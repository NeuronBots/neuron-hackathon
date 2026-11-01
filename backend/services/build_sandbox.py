"""A Modal sandbox where Muse builds a project's first version.

Unlike a review's sandbox (which only reads the PR's files), this one lets Muse write code and run commands: install packages, run the tests,
start the app and check it answers. What keeps that safe is the sandbox itself. It holds the repository, the plan and the skills and nothing of ours
(no database, no GitHub token, no other users' data); it can reach only the model API and the package registries (BUILD_SANDBOX_DOMAINS); the Muse
key goes in over stdin; and it is destroyed afterwards. The finished code is read out of it, never run on this server.

Needs the same Modal credentials as reviews. See services/first_build.py for what is asked of Muse."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex

from services import imagegen, muse_usage, review_sandbox as rs, starter

log = logging.getLogger(__name__)

DOMAINS = [d.strip() for d in os.getenv("BUILD_SANDBOX_DOMAINS", "api.meta.ai,registry.npmjs.org,pypi.org,files.pythonhosted.org").split(",") if d.strip()]
LIFETIME = int(os.getenv("BUILD_SANDBOX_SECONDS", "10800"))
MEMORY_MB = int(os.getenv("BUILD_SANDBOX_MEMORY_MB", "8192"))
CPU = float(os.getenv("BUILD_SANDBOX_CPU", "2"))
MAX_ARCHIVE = int(os.getenv("BUILD_MAX_ARCHIVE_MB", "100")) * 1024 * 1024
MAX_PATCH = 5 * 1024 * 1024
CATALOG_WAITS = (15, 30, 60, 90, 120, 0)  # seconds to wait after each failed start before the next try (about six minutes in all: it fails in windows of a few minutes); the last try is final
INSPECT_TIMEOUT = int(os.getenv("BUILD_REVIEW_TIMEOUT_SECONDS", "1800"))  # one review pass

# What is left out of the archive and the list of files: dependencies and build output that are rebuilt from the code, caches, and the skills this
# module puts in (the plan's reader does not need them, and a repository does not want them).
EXCLUDE = (
    "node_modules/*", "*/node_modules/*", ".venv/*", "*/.venv/*", "venv/*", "*/venv/*", "__pycache__/*", "*/__pycache__/*", "*.pyc", ".git/*",
    ".pytest_cache/*", "*/.pytest_cache/*", ".next/*", ".cache/*", "*/.cache/*", "dist/*", "*/dist/*", "build/*", "*/build/*", "coverage/*", ".neuron/*",
    ".agents/skills/ponytail/*", ".agents/skills/frontend-design/*", ".agents/skills/single-responsibility/*",
)


PRUNE = ("node_modules", ".venv", "venv", ".git", "__pycache__", "dist", "build", ".next", ".cache", ".pytest_cache", "coverage", ".neuron")  # folders find does not enter


def _image():
    return rs._image().apt_install("git", "zip", "unzip", "build-essential", "python3", "python3-pip", "python3-venv", "gnupg").run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
        "apt-get install -y nodejs",
        f"mkdir -p {starter.DIR} && printf %s {shlex.quote(json.dumps(starter.PACKAGE))} > {starter.DIR}/package.json && cd {starter.DIR} && npm install --no-audit --no-fund",  # the starter's dependencies, installed once here
    )


class BuildBox(rs.SandboxBox):
    """One build's sandbox: `load` the repository and the skills, `build` once, then read the result out."""

    on_line = None  # called with what Muse prints while it works (the build card's terminal)
    owner: str | None = None  # whose plan the tokens of this build are counted toward; None: whoever the current task is acting for

    def _spec(self) -> dict:
        return {"image": _image(), "timeout": LIFETIME, "memory": MEMORY_MB, "cpu": CPU, "domains": DOMAINS}

    async def build(self, prompt: str) -> str:
        """Muse implements the prompt in the workspace, with the workspace's skills and rules loaded and with write and shell tools on. Returns its
        closing message. The workspace is trusted on purpose: it is the project's own code plus the skills this module added. Muse's own shell sandbox
        (bubblewrap) is off, since it cannot start inside this one (every command failed with "bwrap: loopback: Failed RTM_NEWADDR") and this one is the boundary:
        it holds nothing of ours, reaches only the registries and the model, and is destroyed afterwards."""
        await self.sb.filesystem.write_text.aio(prompt, f"{rs.WORK}/build-prompt.txt")
        for attempt, wait in enumerate(CATALOG_WAITS, 1):
            feed = muse_usage.Feed(self.on_line) if self.on_line else None
            try:
                rc, out, err = await self._exec(
                    "muse", "exec", "--json", "--prompt-file", f"{rs.WORK}/build-prompt.txt", "--workspace", rs.REPO, "--trust-workspace", "--disable-web-tools",
                    "--disable-sandbox", "--no-session-log", "--approval-mode", "never", "--reasoning-effort", self.effort, "--api-key-stdin",
                    stdin=os.environ["META_API_KEY"] + "\n", timeout=self.timeout, workdir=rs.REPO, env=imagegen.env(), on_line=feed.feed if feed else None,
                )
            except Exception as exc:
                if "imeout" in type(exc).__name__ or isinstance(exc, TimeoutError):
                    raise RuntimeError("the build took too long and was stopped") from exc
                log.exception("the build sandbox failed")
                raise RuntimeError("the build sandbox failed") from exc
            if feed:
                feed.close()
            if rc != 0 and "model catalog" in err and wait:  # Muse sometimes cannot fetch its model list at start-up, in bursts of about a minute; it stopped before writing anything
                log.warning("muse could not fetch its model catalog (try %s of %s), trying again in %ss", attempt, len(CATALOG_WAITS), wait)
                await asyncio.sleep(wait)
                continue
            break
        if rc != 0:
            log.error("muse exited %s while building: %s", rc, err.replace(os.environ["META_API_KEY"], "<key>")[-1500:])
            raise RuntimeError("Muse's service could not start (its model list was unavailable for several minutes). Try the build again in a few minutes." if "model catalog" in err
                               else "Muse stopped before the build was finished")
        closing, tokens = muse_usage.parse(out)
        await asyncio.to_thread(muse_usage.record, tokens, self.owner)  # counted toward the person's plan, like a bot's own turn
        return closing

    @staticmethod
    def _skip() -> str:
        """The paths the baseline and the patch leave out, as the repository's own exclude file. Not as `:(exclude)` pathspecs: git refuses those when a
        project's .gitignore already ignores the same folder (node_modules, .next), and the build then fails after all its work."""
        return "\n".join(("/.agents", "/.neuron", "/AGENTS.md", *PRUNE)) + "\n"

    async def baseline(self) -> None:
        """Remember the workspace as it is (our own files and dependencies left out), so `patch` can say what changed since. Safe to call again: it moves the mark."""
        for cmd in ("git init -q", f"printf %s {shlex.quote(self._skip())} > .git/info/exclude", "git add -A", "git -c user.name=base -c user.email=base@localhost commit -q --allow-empty -m base"):
            rc, _, err = await self._exec("sh", "-c", cmd, workdir=rs.REPO, timeout=300)
            if rc != 0:
                raise RuntimeError("the sandbox could not take a baseline: " + err[-200:])

    async def patch(self) -> str:
        """What changed since `baseline`, as a git patch. Our own files (skills, rules, the plan) and dependencies are left out. A patch over MAX_PATCH is refused."""
        rc, out, err = await self._exec("sh", "-c", "git add -A && git diff --cached --binary --no-color", workdir=rs.REPO, timeout=300)
        if rc != 0:
            raise RuntimeError("the sandbox could not read what Muse changed: " + err[-200:])
        if len(out) > MAX_PATCH:
            raise RuntimeError("the changes are too large to push")
        return out

    async def inspect(self, prompt: str, schema: dict, what: str = "review") -> str:
        """Muse looks at what was built and answers in the schema. It can run anything (the app, the tests, curl) but cannot write to the workspace, so a
        review cannot change the thing it is judging. Same trust and shell settings as `build`. Returns the JSON text."""
        import json
        import uuid

        tag = uuid.uuid4().hex[:8]
        prompt_path, schema_path = f"{rs.WORK}/inspect-{tag}.txt", f"{rs.WORK}/inspect-{tag}.json"
        await self.sb.filesystem.write_text.aio(prompt, prompt_path)
        await self.sb.filesystem.write_text.aio(json.dumps(schema), schema_path)
        for attempt in (1, 2, 3):  # Muse now and then exits with an error on a run that works when repeated
            try:
                rc, out, err = await self._exec(
                    "muse", "exec", "--prompt-file", prompt_path, "--output-schema", schema_path, "--workspace", rs.REPO, "--trust-workspace", "--disable-web-tools",
                    "--disable-write", "--disable-sandbox", "--no-session-log", "--approval-mode", "never", "--reasoning-effort", self.effort, "--api-key-stdin",
                    stdin=os.environ["META_API_KEY"] + "\n", timeout=INSPECT_TIMEOUT, workdir=rs.REPO,
                )
            except Exception as exc:
                if "imeout" in type(exc).__name__ or isinstance(exc, TimeoutError):
                    raise RuntimeError(f"the {what} took too long and was stopped") from exc
                log.exception("the %s sandbox failed", what)
                raise RuntimeError(f"the {what} sandbox failed") from exc
            if rc == 0:
                return out
            log.error("muse exited %s during the %s (try %s of 3): %s", rc, what, attempt, err.replace(os.environ["META_API_KEY"], "<key>")[-1200:])
        raise RuntimeError(f"Muse stopped before the {what} was finished")

    async def install_starter(self) -> None:
        """Put the starter's installed dependencies in the workspace, so its dev server starts without an install. A failure only costs the install."""
        await self._exec("cp", "-a", f"{starter.DIR}/node_modules", f"{rs.REPO}/node_modules", timeout=120)

    async def read(self, path: str) -> str | None:
        """A text file from the workspace (relative to it), or None when there is none."""
        try:
            return await self.sb.filesystem.read_text.aio(f"{rs.REPO}/{path}")
        except Exception:
            return None

    def _excluded(self) -> list[str]:
        out: list[str] = []
        for pattern in EXCLUDE:
            out += ["-x", pattern]
        return out

    async def files(self) -> list[str]:
        """Every file the archive will hold, as paths relative to the workspace. Dependency and cache folders are skipped inside the sandbox, so a
        project with a hundred thousand installed files is still a short list."""
        prune = " -o ".join(f"-name {d}" for d in PRUNE)
        rc, out, _ = await self._exec("sh", "-c", f"find . \\( {prune} \\) -prune -o -type f -print | sed 's#^\\./##' | sort", workdir=rs.REPO, timeout=120)
        names = [n for n in out.splitlines() if n]
        return [n for n in names if not any(self._match(n, p) for p in EXCLUDE)] if rc == 0 else []

    async def write(self, path: str, text: str | None) -> None:
        """Write a text file of the workspace, or remove it when text is None."""
        if text is None:
            await self._exec("rm", "-f", path, workdir=rs.REPO, timeout=30)
        else:
            if "/" in path:
                await self._exec("mkdir", "-p", f"{rs.REPO}/{path.rsplit('/', 1)[0]}", workdir=rs.REPO, timeout=30)
            await self.sb.filesystem.write_text.aio(text, f"{rs.REPO}/{path}")

    @staticmethod
    def _match(name: str, pattern: str) -> bool:
        from fnmatch import fnmatch
        return fnmatch(name, pattern)

    async def export(self) -> bytes:
        """The workspace as a zip, without what the exclusions leave out."""
        rc, _, err = await self._exec("zip", "-qr", f"{rs.WORK}/out.zip", ".", *self._excluded(), workdir=rs.REPO, timeout=300)
        if rc not in (0, 12):  # 12: zip had nothing to do
            log.error("zip failed in the build sandbox: %s", err[:300])
            raise RuntimeError("could not package the code")
        rc, out, _ = await self._exec("sh", "-c", f"wc -c < {rs.WORK}/out.zip", timeout=60)
        if rc != 0 or int(out.strip() or 0) > MAX_ARCHIVE:
            raise RuntimeError("the code is too large to save")
        return await self.sb.filesystem.read_bytes.aio(f"{rs.WORK}/out.zip")


PREVIEW_PORT = 8080
PREVIEW_SECONDS = int(os.getenv("PREVIEW_SANDBOX_SECONDS", "3600"))
SESSION_SECONDS = int(os.getenv("SESSION_SANDBOX_SECONDS", "7200"))


class PreviewBox(BuildBox):
    """A sandbox that runs the finished app and opens one port. The address Modal gives it is kept on this server (services/preview.py proxies it for
    signed-in project members); it never goes to a browser. Same allowlist as a build, so the app can reach only the package registries."""

    lifetime = PREVIEW_SECONDS

    def _spec(self) -> dict:
        return {**super()._spec(), "timeout": self.lifetime, "memory": 4096, "cpu": 1.0, "ports": [PREVIEW_PORT]}

    async def fingerprint(self) -> str:
        """A short value that changes when any source file does (names and modification times; dependencies and caches left out), for the session to notice edits."""
        prune = " -o ".join(f"-name {d}" for d in PRUNE if d != ".git")
        rc, out, _ = await self._exec("sh", "-c", f"find . \\( {prune} \\) -prune -o -type f -printf '%T@ %p\\n' | sort | md5sum", workdir=rs.REPO, timeout=60)
        return out.strip() if rc == 0 else ""

    async def address(self) -> str:
        return (await self.sb.tunnels.aio())[PREVIEW_PORT].url

    async def install(self, command: str) -> tuple[int, str]:
        rc, out, err = await self._exec("sh", "-c", command, workdir=rs.REPO, timeout=900)
        return rc, (out + err)[-4000:]

    async def start(self, command: str, dev: bool = False):
        """Start the app in the background, listening on 0.0.0.0:PREVIEW_PORT. Returns the process, whose output is the app's log. In a coding session the
        development servers that check the Host header are told to accept Modal's, and Flask is put in debug mode so that it reloads."""
        env = {"HOME": rs.WORK, "PORT": str(PREVIEW_PORT), "HOST": "0.0.0.0"}
        if dev:
            env.update({"FLASK_DEBUG": "1", "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": ".modal.host", "CHOKIDAR_USEPOLLING": "1"})
        return await self.sb.exec.aio("sh", "-c", command, workdir=rs.REPO, env=env)


class SessionBox(PreviewBox):
    """The same, for a coding session: it lives longer, because a person keeps asking for changes."""
    lifetime = SESSION_SECONDS

    def _spec(self) -> dict:
        return {**super()._spec(), "memory": MEMORY_MB, "cpu": CPU}


class LiveBuildBox(SessionBox):
    """The sandbox of a first build that is also its coding session: it has the port open from the start and lives as long as a build may take."""
    lifetime = LIFETIME
