"""Run the app that was built, live, in a Modal sandbox, and show it inside the chat.

The sandbox's public address is kept here and never sent to a browser. The page shows an iframe of THIS server, `/api/preview/<id>/<key>/`, and the proxy below
forwards to the sandbox for that key. The key is given only to signed-in members of the project (preview status) and dies with the preview. The frame is sandboxed
without same-origin (iframe attribute, and a CSP sandbox header on every response), so the app's own scripts cannot touch Neuron's session or API; for the same reason
the session cookie does not reach the frame, which is why the key is in the address. A preview stops after PREVIEW_IDLE_SECONDS without a request, when its owner stops it, or at the sandbox's own lifetime."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import secrets
import time
import io
import tarfile
import zipfile

import httpx

from services import build_prompt, chat_rows, runs
from services.build_publish import _find, safe_extract
from services.build_sandbox import PREVIEW_PORT, LiveBuildBox, PreviewBox, SessionBox
from services.first_build import BuildError, EFFORT, TIMEOUT, _need_sandbox, empty_tarball

log = logging.getLogger(__name__)

IDLE_SECONDS = int(os.getenv("PREVIEW_IDLE_SECONDS", "1800"))
READY_SECONDS = 180
MAX_PREVIEWS = int(os.getenv("PREVIEW_MAX", "4"))  # at once on this server
MAX_BODY = 20 * 1024 * 1024

_previews: dict[str, "Preview"] = {}

RECIPE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["install", "start", "notes"],
    "properties": {"install": {"type": "string"}, "start": {"type": "string"}, "notes": {"type": "string"}},
}


class Preview:
    def __init__(self, project_id: str, user_id: str, name: str):
        self.id, self.key, self.project_id, self.user_id, self.name = secrets.token_urlsafe(12), secrets.token_urlsafe(24), project_id, user_id, name
        self.status, self.stage, self.url, self.box, self.task = "starting", "Starting", "", None, None
        self.session, self.agent_id, self.busy, self.version = False, "", False, 0  # a coding session: changes are asked for, and the frame reloads when files change
        self.lock = asyncio.Lock()
        self.based = False  # the app serves itself under this preview's path (Vite --base), so its own URLs already carry it
        self.card_row, self.project_name = 0, ""  # the session's card in the chat, kept in step with its state
        self.logs: collections.deque[str] = collections.deque(maxlen=200)
        self.touched = time.monotonic()
        self.client: httpx.AsyncClient | None = None

    def view(self) -> dict:
        return {"id": self.id, "name": self.name, "src": f"/api/preview/{self.id}/{self.key}/" if self.status == "running" else "", "status": self.status, "stage": self.stage, "session": self.session, "busy": self.busy, "version": self.version, "logs": list(self.logs)[-80:]}


def base_of(preview: Preview) -> str:
    return f"/api/preview/{preview.id}/{preview.key}/"


def recipe_prompt(dev: bool = False) -> str:
    """Muse reads the workspace and says how to install and start it, so the preview does not depend on parsing prose."""
    return (
        f"This workspace is a finished app. Work out how to install it and how to start it, from `{build_prompt.RUN_GUIDE}` and the code, and check the commands work "
        "(you may run them, but do not change any file).\n"
        f"- install: one shell command that installs everything needed (for example `pip install -r requirements.txt` or `npm ci`). Empty if nothing is needed.\n"
        f"- start: one shell command that starts the app and keeps running, listening on host 0.0.0.0 and port {PREVIEW_PORT} (the environment already has PORT={PREVIEW_PORT} and HOST=0.0.0.0; "
        f"pass them explicitly if the app does not read them, for example `flask --app app run --host 0.0.0.0 --port {PREVIEW_PORT}`). For a front end that must be built first, build it in install "
        "and serve the output. No development watchers.\n"
        "- notes: one sentence on what you saw.\n"
        + ("This is a live coding session, so start it in development mode so that it reloads by itself when files change (for example `flask --app app run --debug --host 0.0.0.0 "
           f"--port {PREVIEW_PORT}`, `npm run dev -- --host 0.0.0.0 --port {PREVIEW_PORT}`, or `uvicorn app:app --reload --host 0.0.0.0 --port {PREVIEW_PORT}`).\n" if dev else "")
    )


def guess_recipe(files: dict[str, str]) -> dict | None:
    """How to run a project from its files alone, for when Muse could not say: a package.json dev script, or a Python app with requirements. Looked for at
    the root first, then one folder down (frontend/, web/, app/ ...), where a project that keeps its backend and front end apart puts the part you open."""
    import json as _json

    folders = sorted({n.rsplit("/", 1)[0] if "/" in n else "" for n in files if n.count("/") <= 1}, key=lambda f: (f != "", f))
    for folder in folders:
        at = (lambda n: f"{folder}/{n}" if folder else n)
        cd = f"cd {folder} && " if folder else ""
        if at("package.json") in files:
            try:
                scripts = (_json.loads(files[at("package.json")]).get("scripts") or {})
            except ValueError:
                scripts = {}
            for name, flags in (("dev", f"-- --host 0.0.0.0 --port {PREVIEW_PORT}"), ("start", "")):
                if name in scripts:
                    return {"install": f"{cd}npm install --no-audit --no-fund", "start": f"{cd}npm run {name} {flags}".strip(), "vite": "vite" in scripts[name]}
        if at("requirements.txt") in files and at("app.py") in files:
            return {"install": f"{cd}pip install -r requirements.txt", "start": f"{cd}python app.py"}
    return None


def parse_recipe(text: str) -> dict | None:
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return None
    ok = isinstance(data, dict) and isinstance(data.get("start"), str) and data["start"].strip() and isinstance(data.get("install", ""), str)
    return {"install": (data.get("install") or "").strip(), "start": data["start"].strip()} if ok else None


async def _extract(box: PreviewBox, archive: str, agent_id: str, user_id: str, session: bool = False) -> dict[str, str]:
    """The saved code goes into the sandbox as the archive's own files (read as untrusted first, like a publish)."""
    import tempfile
    from pathlib import Path

    path, _, _ = await asyncio.to_thread(_find, agent_id, user_id, archive)
    with tempfile.TemporaryDirectory(prefix="preview-") as tmp:
        out = Path(tmp) / "code"
        out.mkdir()
        await asyncio.to_thread(safe_extract, path, out)
        files = {}  # the text files, for finding how to run the project; pictures and other binaries go in the tarball, which keeps their bytes
        for p in out.rglob("*"):
            if p.is_file() and p.stat().st_size < 2_000_000:
                try:
                    files[str(p.relative_to(out))] = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    pass
        tarball = io.BytesIO()
        with tarfile.open(fileobj=tarball, mode="w:gz") as tar:
            tar.add(out, arcname="project")  # one top folder: the sandbox unpacks it with --strip-components=1
    extra = {}
    if session:  # Muse will change this code: it works under the same skills and rules as the build did
        extra = {**build_prompt.skill_files(), "AGENTS.md": build_prompt.with_rules(files.get("AGENTS.md"))}
        files.update(extra)
    await box.load(tarball.getvalue(), extra)
    return files


async def _pump(preview: Preview, proc) -> None:
    """Keep the app's output, line by line, for the log drawer."""
    try:
        async for line in proc.stdout:
            preview.logs.append(line.rstrip()[:400])
    except Exception:
        pass


async def _ready(preview: Preview) -> bool:
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        try:
            await preview.client.get(preview.url + "/", timeout=5)
            return True
        except httpx.HTTPError:
            await asyncio.sleep(2)
    return False


def _sync_stage(preview: Preview, stage: str) -> None:
    preview.stage, preview.touched = stage, time.monotonic()


async def _sync_card(preview: Preview) -> None:
    """The session's card in the chat says what state it is in (it is written once, when the session starts, and would otherwise say Starting for good)."""
    if not preview.card_row:
        return
    status = {"running": "running", "failed": "failed"}.get(preview.status, "done")
    stage = {"running": "Running. The app is beside this chat.", "failed": preview.stage}.get(preview.status, "The session ended.")
    await asyncio.to_thread(chat_rows.write_row, preview.agent_id, preview.user_id, _card(preview.project_name, preview.id, status, "Coding session", stage), preview.card_row)


async def _ready_again(preview: Preview, seconds: int = 25) -> None:
    """After an edit a development server restarts itself; wait for it to answer so the frame does not reload into an error."""
    await asyncio.sleep(1)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            await preview.client.get(preview.url + "/", timeout=4)
            return
        except httpx.HTTPError:
            await asyncio.sleep(1)


async def _recipe(preview: Preview, box, files: dict[str, str]) -> dict:
    """How to start the app: read from its files (a project's start-up never changes), and only when they do not say, asked of Muse."""
    recipe = guess_recipe(files)
    for _ in range(2 if recipe is None else 0):  # Muse sometimes answers in a shape that cannot be read, or stops: ask again
        preview.stage = "Working out how to start it"
        try:
            recipe = parse_recipe(await box.inspect(recipe_prompt(preview.session), RECIPE_SCHEMA, "start-up check"))
        except RuntimeError:
            log.exception("Muse could not say how to start %s", preview.name)
        if recipe:
            break
    if recipe is None:
        raise RuntimeError("Muse could not work out how to start this app")
    return recipe


async def _launch(preview: Preview, box, recipe: dict):
    """Install and start the app, and wait until it answers. Returns the process and the task that keeps its log."""
    if recipe["install"]:
        preview.stage = "Installing"
        rc, out = await box.install(recipe["install"])
        preview.logs.extend(out.splitlines()[-30:])
        if rc != 0:
            raise RuntimeError("the install failed; see the log")
    preview.stage = "Starting the app"
    start = recipe["start"]
    preview.based = bool(recipe.get("vite")) and preview.session
    if preview.based:  # Vite writes absolute paths into its pages and modules ("/src/app.tsx"), which would leave this preview's folder: have it serve itself under the folder
        start += (" " if " -- " in start else " -- ") + f"--base {base_of(preview)}"
    proc = await box.start(start, dev=preview.session)
    pump = asyncio.create_task(_pump(preview, proc))
    try:
        preview.url = (await box.address()).rstrip("/")
        preview.client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        if not await _ready(preview):
            raise RuntimeError("the app did not start answering; see the log")
    except BaseException:
        pump.cancel()
        if preview.client is not None:
            await preview.client.aclose()
        preview.client, preview.url = None, ""
        raise
    preview.status, preview.stage = "running", "Muse is building" if preview.busy else "Running"
    preview.touched = time.monotonic()
    await _sync_card(preview)
    return proc, pump


RELOAD_EVERY = 8  # seconds, at most, while Muse is still writing: a frame that reloads on every file would never hold still


async def _refresh(preview: Preview, box, seen: str, last: float) -> tuple[str, float]:
    """A file changed: once the app answers again, the frame reloads."""
    now = await box.fingerprint()
    if not now or now == seen or (preview.busy and time.monotonic() - last < RELOAD_EVERY):
        return seen, last
    await _ready_again(preview)
    preview.version += 1
    preview.touched = time.monotonic()
    return now, time.monotonic()


async def _hold(preview: Preview, box, proc, pump) -> None:
    """Keep the running app up until it is idle, stopped, or dies."""
    try:
        seen, last = await box.fingerprint() if preview.session else "", 0.0
        while preview.status == "running" and time.monotonic() - preview.touched < IDLE_SECONDS:
            await asyncio.sleep(2 if preview.session else 5)
            if preview.session:
                seen, last = await _refresh(preview, box, seen, last)
            if preview.status == "running" and await proc.poll.aio() is not None:
                raise RuntimeError("the app stopped; see the log")
        preview.status, preview.stage = "stopped", "Stopped after being idle" if preview.status == "running" else "Stopped"
    finally:
        pump.cancel()
        await preview.client.aclose()


async def _guarded(preview: Preview, work) -> None:
    """The ending every preview shares: stopped, failed, or run to its end, and the session's card says which."""
    try:
        await work
    except asyncio.CancelledError:
        preview.status, preview.stage = "stopped", "Stopped"
        raise
    except Exception as exc:
        log.exception("Preview of %s failed", preview.name)
        preview.status, preview.stage = "failed", str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "The preview could not start"
    finally:
        preview.url = ""  # the address is useless from here on
        try:
            await asyncio.shield(_sync_card(preview))
        except Exception:
            log.exception("Could not update the session card")


async def _run(preview: Preview, archive: str, agent_id: str) -> None:
    async def work() -> None:
        async with (SessionBox if preview.session else PreviewBox)(EFFORT if EFFORT in ("low", "medium") else "medium", 900 if not preview.session else 7200) as box:
            preview.box = box
            preview.stage = "Putting the code in the sandbox"
            files = await _extract(box, archive, agent_id, preview.user_id, preview.session)
            if preview.session:  # the code as it is now: what the session changes later (a review's fixes among it) is measured from here
                await box.baseline()
            recipe = await _recipe(preview, box, files)
            await _hold(preview, box, *await _launch(preview, box, recipe))

    await _guarded(preview, work())


# ── a session that is there while the first build is still being written ──

BRING_UP_EVERY = 5  # seconds between looks at what Muse has written so far
BRING_UP_TRIES = 8   # the app is half written for a while: a start that fails is tried again, a few times, a little later each time


async def _start_files(box) -> dict[str, str]:
    """The files that say how to start the app, at the root and one folder down."""
    wanted = ("package.json", "requirements.txt", "app.py")
    found = [n for n in await box.files() if n.count("/") <= 1 and n.rsplit("/", 1)[-1] in wanted]
    return {n: t for n in found if (t := await box.read(n)) is not None}


async def _bring_up(preview: Preview, box, builder: asyncio.Future) -> tuple | None:
    """While Muse builds: as soon as the files say how to start the app, install and start it, so it is on screen and reloads as the code is written.
    Returns the running app (process, log task) or None when the build ended first. A failed start never fails the build."""
    tries, seen, last, not_before = 0, "", 0.0, 0.0
    launched = None
    while not builder.done():
        await asyncio.sleep(min(1, BRING_UP_EVERY) if launched is None and tries == 0 else BRING_UP_EVERY)  # the first look is at once: the starter is already there
        preview.touched = time.monotonic()  # a build that nobody is watching must not be taken for an idle preview
        try:
            if launched is not None:
                seen, last = await _refresh(preview, box, seen, last)
                if await launched[0].poll.aio() is not None:  # the dev server fell over (a half-written file): start it again
                    launched[1].cancel()
                    await preview.client.aclose()
                    launched, preview.status, seen = None, "starting", ""
                continue
            if tries >= BRING_UP_TRIES or time.monotonic() < not_before:
                continue
            files = await _start_files(box)
            recipe = guess_recipe(files)
            if recipe is None:
                continue
            tries += 1
            launched = await _launch(preview, box, recipe)
            seen, last = await box.fingerprint(), time.monotonic()
        except Exception:
            log.exception("Could not bring up the app of %s while it is being built (try %s)", preview.name, tries)
            not_before = time.monotonic() + 30 * tries  # an install on files that are still being written fails fast: give Muse time before the next try
            preview.status, preview.stage = "starting", "Muse is building"
    return launched


async def _run_live(preview: Preview, steps, built: asyncio.Future) -> None:
    """The first build, in a session's own sandbox: the dev server is started as soon as there is something to start, and when the build ends the same
    sandbox goes on as the coding session. `steps(box)` is the build itself; its result goes to `built`."""
    async def work() -> None:
        async with LiveBuildBox(EFFORT, TIMEOUT) as box:
            preview.box = box
            builder = asyncio.ensure_future(steps(box))
            try:
                launched = await _bring_up(preview, box, builder)
                result = await builder
            except BaseException as exc:
                builder.cancel()
                if not built.done():
                    built.cancel() if isinstance(exc, asyncio.CancelledError) else built.set_exception(exc)
                raise
            preview.busy = False
            preview.stage = "Running" if launched else "Working out how to start it"
            await box.write("AGENTS.md", build_prompt.with_rules(await box.read("AGENTS.md")))  # Muse changes this code from here on, under the build rules again
            await box.baseline()  # what the session changes later is measured from the finished build
            built.set_result(result)
            if launched is None:  # nothing could be started while it was written: the usual way, from the finished files
                files = await _start_files(box)
                launched = await _launch(preview, box, await _recipe(preview, box, files))
            preview.touched = time.monotonic()
            await _hold(preview, box, *launched)

    try:
        await _guarded(preview, work())
    finally:
        if not built.done():
            built.set_exception(RuntimeError(preview.stage or "the build could not start"))


async def begin_live(project_id: str, user_id: str, agent_id: str, name: str) -> Preview:
    """The coding session of a project whose first build is about to start: it exists (and its card is in the chat) before there is any code."""
    _need_sandbox()
    live = [p for p in _previews.values() if p.status in ("starting", "running")]
    for old in [p for p in live if p.project_id == project_id]:
        await stop(old.id)
    if len(live) - sum(p.project_id == project_id for p in live) >= MAX_PREVIEWS:
        raise BuildError("Too many previews are running right now. Try again in a few minutes.", 429)
    preview = Preview(project_id, user_id, name)
    preview.session, preview.agent_id, preview.busy, preview.project_name = True, agent_id, True, name
    preview.stage = "Muse is building"
    preview.card_row = await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, _card(name, preview.id, "running", "Coding session", "Muse is building the first version"))
    _previews[preview.id] = preview  # only once its card is written: a session that could not be announced is not left behind
    return preview


async def abandon(preview: Preview, reason: str) -> None:
    """The build failed before it reached the session's sandbox (nothing was written to it): the session ends with it."""
    if preview.task is None:
        preview.status, preview.stage = "failed", reason[:300]
        await _sync_card(preview)


async def run_live(preview: Preview, steps) -> dict:
    """Run the build (`steps(box)`) in the session's sandbox and return its result. The session goes on after it, in the same sandbox."""
    built = asyncio.get_running_loop().create_future()
    preview.task = runs.spawn(_run_live(preview, steps, built))
    try:
        return await built
    except asyncio.CancelledError:
        await asyncio.shield(stop(preview.id))
        raise


async def start(project_id: str, user_id: str, agent_id: str, archive: str, session: bool = False) -> dict:
    """Start a live preview of a saved build. One per project: starting again replaces it."""
    _need_sandbox()
    live = [p for p in _previews.values() if p.status in ("starting", "running")]
    for old in [p for p in live if p.project_id == project_id]:
        await stop(old.id)
    if len(live) - sum(p.project_id == project_id for p in live) >= MAX_PREVIEWS:
        raise BuildError("Too many previews are running right now. Try again in a few minutes.", 429)
    await asyncio.to_thread(_find, agent_id, user_id, archive)  # the person may read it and it is this project's: refused before a sandbox is paid for
    preview = Preview(project_id, user_id, archive)
    preview.session, preview.agent_id = session, agent_id
    _previews[preview.id] = preview
    preview.task = runs.spawn(_run(preview, archive, agent_id))
    return preview.view()


async def stop(preview_id: str) -> None:
    preview = _previews.get(preview_id)
    if preview is not None and preview.task is not None and not preview.task.done():
        preview.status = "stopping"
        preview.task.cancel()
        await asyncio.gather(preview.task, return_exceptions=True)
        preview.status, preview.stage = "stopped", "Stopped"


def by_key(preview_id: str, key: str) -> Preview | None:
    """The preview for a frame's address."""
    preview = _previews.get(preview_id)
    return preview if preview is not None and secrets.compare_digest(preview.key, key) else None


def get(preview_id: str, project_ids: set[str]) -> Preview | None:
    """The preview, if the caller is a member of its project (project_ids: the caller's projects)."""
    preview = _previews.get(preview_id)
    return preview if preview is not None and preview.project_id in project_ids else None


# ── the proxy ────────────────────────────────────────────────────────────

_DROP_REQUEST = {"host", "cookie", "authorization", "content-length", "connection", "accept-encoding", "x-forwarded-for", "x-real-ip"}
_DROP_RESPONSE = {"content-length", "content-encoding", "transfer-encoding", "connection", "set-cookie", "x-frame-options", "content-security-policy", "strict-transport-security"}

# The frame has no origin of its own (sandboxed without same-origin), so the browser refuses it localStorage and sessionStorage, and an app that reads them while
# it renders (a theme, a saved plan) crashes to a blank page: it gets in-memory ones instead, which last until the frame reloads.
# Root-relative URLs in the app ("/api/x", fetch("/x"), href="/x") would leave the preview's folder: they are pointed back into it. This is the usual
# limit of serving an app under a path; an app that builds its URLs some other way may not work in the preview.
_SHIM = """<script>(function(){var B=%s;
try{window.localStorage.getItem('x')}catch(e){['localStorage','sessionStorage'].forEach(function(n){var d={};try{Object.defineProperty(window,n,{configurable:true,value:{getItem:function(k){return Object.prototype.hasOwnProperty.call(d,k)?d[k]:null},setItem:function(k,v){d[k]=String(v)},removeItem:function(k){delete d[k]},clear:function(){d={}},key:function(i){return Object.keys(d)[i]||null},get length(){return Object.keys(d).length}}})}catch(e2){}})}
function f(u){return typeof u==='string'&&u.charAt(0)==='/'&&u.charAt(1)!=='/'&&u.indexOf(B)!==0?B.slice(0,-1)+u:u}
var F=window.fetch;window.fetch=function(i,o){return F.call(this,typeof i==='string'?f(i):i,o)};
var O=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){arguments[1]=f(u);return O.apply(this,arguments)};
['pushState','replaceState'].forEach(function(n){var H=history[n];history[n]=function(s,t,u){return H.call(this,s,t,f(u))}});
document.addEventListener('click',function(e){var a=e.target&&e.target.closest&&e.target.closest('a[href]');if(a){var h=a.getAttribute('href');if(h&&f(h)!==h)a.setAttribute('href',f(h))}},true);
document.addEventListener('submit',function(e){var a=e.target.getAttribute('action');if(a)e.target.setAttribute('action',f(a))},true);})();</script>"""
_ATTR = re.compile(r'(\s(?:src|href|action|poster)=)(["\'])/(?!/)([^"\']*)\2', re.I)


def rewrite_html(html: str, base: str, relink: bool = True) -> str:
    if relink:  # an app that serves itself under the base has already done this
        html = _ATTR.sub(lambda m: f"{m.group(1)}{m.group(2)}{base}{m.group(3)}{m.group(2)}", html)
    shim = _SHIM % json.dumps(base)
    return re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + shim, html, count=1, flags=re.I) if re.search(r"<head", html, re.I) else shim + html


async def forward(preview: Preview, method: str, path: str, query: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
    """One request to the app in the sandbox. Returns (status, headers, body) with the app's cookies, framing headers and absolute links taken care of."""
    if preview.status != "running" or not preview.url or preview.client is None:
        return 503, {"content-type": "text/plain"}, b"The preview is not running."
    preview.touched = time.monotonic()
    base = base_of(preview)
    try:
        r = await preview.client.request(method, f"{preview.url}/{base.lstrip('/') if preview.based else ''}{path}" + (f"?{query}" if query else ""), content=body or None,
                                         headers={**{k: v for k, v in headers.items() if k.lower() not in _DROP_REQUEST}, "host": f"localhost:{PREVIEW_PORT}"})  # dev servers refuse a host they do not know (Vite 5, webpack): the tunnel routes by its own name, so the app is told it is being opened locally
    except httpx.HTTPError:
        return 502, {"content-type": "text/plain"}, b"The app did not answer."
    out = {k: v for k, v in r.headers.items() if k.lower() not in _DROP_RESPONSE}
    if (loc := out.get("location")) and loc.startswith("/") and not loc.startswith("//") and not loc.startswith(base):
        out["location"] = base + loc[1:]
    content = r.content[:MAX_BODY]
    if "text/html" in r.headers.get("content-type", ""):
        content = rewrite_html(content.decode(r.encoding or "utf-8", errors="replace"), base, relink=not preview.based).encode()
    return r.status_code, out, content


# ── a coding session, asked for through a bot ────────────────────────────

def _card(name: str, preview_id: str, status: str, title: str, stage: str = "") -> str:
    return chat_rows.card("session", project=name, preview_id=preview_id, status=status, title=title[:200], stage=stage)


def _context(agent_id: str) -> tuple[str, str, str, str]:
    """(project id, project name, repo, the newest saved build of it), for the bot's project."""
    from core.database import SessionLocal
    from core.models import Agent, DocumentRecord, Project

    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        project = db.get(Project, agent.project_id) if agent else None
        if project is None:
            return "", "", "", ""
        zips = db.query(DocumentRecord).filter(DocumentRecord.project_id == project.id, DocumentRecord.filename.like("%.zip")).order_by(DocumentRecord.created_at.desc()).all()
        return project.id, project.name, project.repo or "", next((z.filename for z in zips if "-first-build-" in z.filename or "-session-" in z.filename), "")
    finally:
        db.close()


def session_of(project_id: str, user_id: str) -> Preview | None:
    return next((p for p in _previews.values() if p.session and p.project_id == project_id and p.user_id == user_id and p.status in ("starting", "running")), None)


async def start_session(project_id: str, name: str, user_id: str, agent_id: str, archive: str) -> dict:
    """Start a coding session on a saved build and put its card in the bot's chat (the card opens the panel and follows the session's state)."""
    info = await start(project_id, user_id, agent_id, archive, session=True)
    row = await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, _card(name, info["id"], "running", "Coding session", "Starting"))
    if (live := _previews.get(info["id"])) is not None:
        live.card_row, live.project_name = row, name
        if live.status != "starting":  # it already finished while the card was being written
            await _sync_card(live)
    return info


async def start_for_bot(agent_id: str) -> str:
    """The start_coding_session tool. Returns what the bot is told."""
    from core.usage import current_user

    user = current_user()
    project_id, name, _, archive = await asyncio.to_thread(_context, agent_id)
    if not user or not project_id:
        return "Error: a coding session needs a signed-in person on a project."
    if not archive:
        return "Error: there is no saved build of this project to code on yet. Build the first version first (build_project), then start the session."
    try:
        await start_session(project_id, name, user, agent_id, archive)
    except (BuildError, ValueError) as exc:
        return f"Error: {exc}"
    return ("Started: a live coding session. The app is starting in a sandbox (about two minutes) and appears in a panel beside this chat, where it reloads by itself after every "
            "change. Tell the person it is starting and that they can now tell you what to change; each change is made with change_code. Do not call start_coding_session again.")


async def change_for_bot(agent_id: str, request: str) -> str:
    """The change_code tool: Muse makes one change in the running session, in the background."""
    from core.usage import current_user

    user = current_user()
    project_id, name, _, _ = await asyncio.to_thread(_context, agent_id)
    session = session_of(project_id, user or "")
    if session is None:
        return "Error: there is no coding session running for this person. Start one first with start_coding_session."
    if not request.strip():
        return "Error: say what to change."
    if session.status != "running":
        return "Error: the session is still starting. Wait for the app to appear, then ask again."
    if session.busy:
        return "Error: Muse is still making the previous change. Wait for it to finish."
    session.busy, session.stage, session.touched = True, "Muse is changing the code…", time.monotonic()
    row = await asyncio.to_thread(chat_rows.write_row, agent_id, user, _card(name, session.id, "running", f"Changing: {request.strip()[:120]}", "Muse is changing the code"))
    runs.spawn(_change_and_tell(session, agent_id, name, request.strip(), row))
    return ("Started: Muse is making that change in the running app. The frame updates when it is done, and you are told what changed. "
            "Tell the person it is in progress. Do not call change_code again until you are told it finished.")


async def _change_and_tell(session: Preview, agent_id: str, name: str, request: str, row: int) -> None:
    from routes.chat import _start_kickoff
    from services.review_render import untrusted_block

    from core.usage import set_usage_user

    set_usage_user(session.user_id, session.project_id)  # Muse's tokens for this change are counted toward their plan
    summary, failure = "", ""
    try:
        async with session.lock:
            session.box.owner = session.user_id
            summary = (await session.box.build(build_prompt.change_prompt(name, request))).strip()[-1500:]
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("A change in the coding session of %s failed", name)
        failure = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "the change could not be made"
    finally:
        session.busy, session.stage, session.touched = False, "Running", time.monotonic()
    title = f"Changing: {request[:120]}"
    await asyncio.to_thread(chat_rows.write_row, agent_id, session.user_id, _card(name, session.id, "failed" if failure else "done", title, failure[:200]), row)
    text = (f"You asked Muse to make this change in the live coding session of {name}: {request[:300]!r}.\n"
            + (f"It did not finish: {failure}. Tell the person.\n" if failure else "It is done and the frame has reloaded. Say what changed from its report, in a sentence or two:\n" + untrusted_block("Muse's report", summary) + "\n")
            + "No headings. You have no tools for this message.")
    _start_kickoff(agent_id, "Neuron", session.project_id, text, session.user_id, no_tools=True)


async def save(session: Preview) -> dict:
    """Save the session's code: a zip in Docs, and the pull request behind its Allow card when the project has a repository."""
    from services import first_build as fb

    async with session.lock:
        box = session.box
        if box is None or session.status != "running":
            raise BuildError("The session is not running.", 409)
        own = await box.read("AGENTS.md")
        await box.write("AGENTS.md", build_prompt.without_rules(own, False))  # the rules were ours: the saved code is without them
        try:
            guide, files, archive = (await box.read(build_prompt.RUN_GUIDE) or "").strip(), await box.files(), await box.export()
        finally:
            if own is not None:
                await box.write("AGENTS.md", own)
    project_id, name, repo, _ = await asyncio.to_thread(_context, session.agent_id)
    saved = await asyncio.to_thread(fb._save, project_id, session.user_id, session.agent_id, name, archive, guide, "")
    if repo:
        await asyncio.to_thread(fb._offer_pull_request, project_id, session.user_id, session.agent_id, repo, name, saved, len(files))
    await asyncio.to_thread(chat_rows.write_row, session.agent_id, session.user_id, _card(name, session.id, "done", "Session saved", f"{len(files)} files saved in Docs" + (" · pull request waiting for your Allow" if repo else "")))
    return {"files": len(files), "zip": saved["zip"]}


async def save_for_bot(agent_id: str) -> str:
    from core.usage import current_user

    user = current_user()
    project_id, _, _, _ = await asyncio.to_thread(_context, agent_id)
    session = session_of(project_id, user or "")
    if session is None or session.status != "running":
        return "Error: there is no running coding session to save."
    try:
        out = await save(session)
    except BuildError as exc:
        return f"Error: {exc}"
    return f"Saved: {out['files']} files are in Docs as a zip. If the project has a repository, a card in this chat asks the person to allow the pull request. Tell them."
