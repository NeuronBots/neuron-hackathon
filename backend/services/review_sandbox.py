"""Muse for a PR review, run in a Modal sandbox instead of on this server.

A PR's files are written by its author, and Muse reads them. Here the checkout and Muse live in a container that holds nothing else of ours:
no database, no environment, no other users' data. It can reach only the model API (REVIEW_SANDBOX_DOMAINS), it never sees a GitHub token (this
server downloads the PR's tarball and uploads it), and the Muse key goes in over stdin, not the environment. One sandbox per review, destroyed after.

Needs MODAL_TOKEN_ID and MODAL_TOKEN_SECRET (or a `modal token set` profile) and REVIEW_RUNNER=modal. See services/code_review.py."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid

log = logging.getLogger(__name__)

APP_NAME = "neuron-pr-review"
LIFETIME = int(os.getenv("REVIEW_SANDBOX_SECONDS", "7200"))  # one sandbox serves a whole review: the walkthrough and every findings batch
DOMAINS = [d.strip() for d in os.getenv("REVIEW_SANDBOX_DOMAINS", "api.meta.ai").split(",") if d.strip()]  # all the sandbox may reach; empty = anywhere
MEMORY_MB = int(os.getenv("REVIEW_SANDBOX_MEMORY_MB", "2048"))
MUSE_TRIES = 3  # a Muse call that exits with an error is run again (twice, after a short wait): the model API drops a call now and then
RETRY_WAIT = 4.0

# The Linux build of Muse, pinned by checksum so the image is the same every time and cannot be swapped under us. To update, read the
# channel's manifest (https://api.meta.ai/muse-code/channels/muse-stable -> manifest_url) and copy the x86_linux url and checksum.
MUSE_VERSION = "1.4.14-R7309.1"
MUSE_URL = "https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=1.4.14-R7309.1&file=muse-x86-linux"
MUSE_SHA256 = "0809aac7cb58d18f5022b6d05e02aa1974683c0ec430e5f04d25b60ed17de2da"

WORK, REPO = "/work", "/work/repo"

# A sandbox is destroyed when its box closes, but a restart or a crash of this server skips that, and the sandbox then runs (and is paid for) until its own
# lifetime ends. Every sandbox is tagged with this server's name, and the ones tagged with it that this process is not holding are orphans: reaped below.
OWNER = os.getenv("SANDBOX_OWNER") or socket.gethostname()  # one name per deployment, so a second environment sharing the Modal app keeps its own
REAP_SECONDS = int(os.getenv("SANDBOX_REAP_SECONDS", "600"))  # 0 turns the reaper off
REAP_GRACE = 180  # a sandbox this young may still be being set up
_held: set[str] = set()  # the sandboxes this process has open


def _image():
    import modal

    return (
        modal.Image.debian_slim()
        .apt_install("ca-certificates", "curl")
        .run_commands(
            f"curl -fsSL -o /usr/local/bin/muse '{MUSE_URL}'",
            f"echo '{MUSE_SHA256}  /usr/local/bin/muse' | sha256sum -c -",
            "chmod 755 /usr/local/bin/muse",
        )
    )


def orphans(found: list[tuple[str, dict]], held: set[str], now: float) -> list[str]:
    """The ids, among (id, tags) of the sandboxes tagged with this server's name, that nothing here is holding and that are past the grace period."""
    return [i for i, tags in found if i not in held and now - float(tags.get("started") or 0) >= REAP_GRACE]


async def reap_orphans() -> list[str]:
    """Terminate the orphaned sandboxes of this server. Returns their ids."""
    import modal

    app = await modal.App.lookup.aio(APP_NAME, create_if_missing=False)
    sandboxes = {sb.object_id: sb async for sb in modal.Sandbox.list.aio(app_id=app.app_id, tags={"owner": OWNER})}
    found = [(i, await sb.get_tags.aio()) for i, sb in sandboxes.items()]
    gone = orphans(found, _held, time.time())
    for i in gone:
        await sandboxes[i].terminate.aio()
        log.warning("terminated the orphaned sandbox %s", i)
    return gone


async def reaper() -> None:
    """Look for orphaned sandboxes at start-up and then every REAP_SECONDS. Quiet when Modal is not set up here."""
    while True:
        try:
            import modal

            if os.environ.get("MODAL_TOKEN_ID") or modal.config.config.get("token_id"):
                await reap_orphans()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the sandbox reaper failed; it tries again later")
        await asyncio.sleep(REAP_SECONDS)


class SandboxBox:
    """One review's sandbox: `load` the PR's code, then `muse` as many times as the passes need."""

    def __init__(self, effort: str, timeout: int):
        self.effort, self.timeout = effort, timeout
        self.sb = None

    def _spec(self) -> dict:
        """What the sandbox is made of; a subclass that builds code instead of reading it changes this."""
        return {"image": _image(), "timeout": LIFETIME, "memory": MEMORY_MB, "cpu": 1.0, "domains": DOMAINS}

    async def __aenter__(self) -> SandboxBox:
        if not os.environ.get("META_API_KEY"):
            raise RuntimeError("META_API_KEY is not set")
        import modal

        try:
            spec = self._spec()
            app = await modal.App.lookup.aio(APP_NAME, create_if_missing=True)
            self.sb = await modal.Sandbox.create.aio(
                app=app, image=spec["image"], timeout=spec["timeout"], memory=spec["memory"], cpu=spec["cpu"], workdir=WORK,
                **({"outbound_domain_allowlist": spec["domains"]} if spec["domains"] else {}),
                **({"encrypted_ports": spec["ports"]} if spec.get("ports") else {}),
            )
        except Exception as exc:  # no credentials, a quota, an outage: never fall back to running the PR's code on this server
            log.exception("could not start the review sandbox")
            raise RuntimeError("could not start the review sandbox") from exc
        _held.add(self.sb.object_id)
        try:
            await self.sb.set_tags.aio({"owner": OWNER, "started": str(int(time.time()))})
        except Exception:
            log.warning("could not tag the sandbox %s: it will not be reaped if it is orphaned", self.sb.object_id)
        return self

    async def __aexit__(self, *exc) -> None:
        if self.sb is not None:
            _held.discard(self.sb.object_id)
            try:
                await self.sb.terminate.aio()
            except Exception:
                log.exception("could not stop the review sandbox")  # it still ends at its own timeout

    async def _exec(self, *args: str, stdin: str | None = None, timeout: int | None = None, workdir: str = WORK, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        p = await self.sb.exec.aio(*args, workdir=workdir, env={"HOME": WORK, **(env or {})}, timeout=timeout)
        if stdin is not None:
            p.stdin.write(stdin)
            p.stdin.write_eof()
            await p.stdin.drain.aio()
        out, err = await p.stdout.read.aio(), await p.stderr.read.aio()
        return await p.wait.aio(), out, err

    async def load(self, tarball: bytes, files: dict[str, str], _retry: int = 2) -> None:
        """Unpack the PR's code and add the review's own files (paths relative to the checkout). Symlinks are removed: a link to /proc/self/environ
        or a system file is how a hostile repo would make Muse read its own key or the container into the review it writes."""
        try:
            await self.sb.filesystem.write_bytes.aio(tarball, f"{WORK}/repo.tgz")
            for cmd in (
                ["mkdir", "-p", REPO],
                ["tar", "xzf", f"{WORK}/repo.tgz", "-C", REPO, "--strip-components=1", "--no-same-owner", "--no-same-permissions"],
                ["rm", "-f", f"{WORK}/repo.tgz"],
                ["find", REPO, "-type", "l", "-delete"],
            ):
                rc, _, err = await self._exec(*cmd, timeout=300)
                if rc != 0:
                    log.error("%s failed in the sandbox: %s", cmd[0], err[:300])
                    raise RuntimeError("could not unpack the PR in the sandbox")
            for path, text in files.items():
                await self.sb.filesystem.write_text.aio(text, f"{REPO}/{path}")
        except RuntimeError:
            raise
        except Exception as exc:
            if _retry and isinstance(exc, ConnectionError):  # a lookup or connection that dropped for a moment on this machine: the load is safe to repeat
                log.warning("loading the sandbox hit a network error, trying again: %s", exc)
                await asyncio.sleep(3)
                return await self.load(tarball, files, _retry - 1)
            log.exception("could not load the PR into the sandbox")
            raise RuntimeError("could not load the PR into the sandbox") from exc

    async def muse(self, text: str, schema: dict) -> str:
        """One `muse exec` over the checkout; its stdout, which the caller checks against the schema."""
        try:
            tag = uuid.uuid4().hex[:8]  # a name of its own, so calls can run side by side
            prompt_path, schema_path = f"{WORK}/prompt-{tag}.txt", f"{WORK}/schema-{tag}.json"
            await self.sb.filesystem.write_text.aio(text, prompt_path)
            await self.sb.filesystem.write_text.aio(json.dumps(schema), schema_path)
            # Muse reads the PR author's files with writes, shell and web tools off, and runs from outside the checkout so its workspace config
            # is never loaded (the workspace is untrusted; --trust-workspace is never passed). The key arrives on stdin, so it is in no file or environment.
            for attempt in range(1, MUSE_TRIES + 1):
                rc, out, err = await self._exec(
                    "muse", "exec", "--prompt-file", prompt_path, "--output-schema", schema_path, "--workspace", REPO,
                    "--disable-write", "--disable-shell", "--disable-web-tools", "--no-session-log", "--approval-mode", "never",
                    "--reasoning-effort", self.effort, "--api-key-stdin", stdin=os.environ["META_API_KEY"] + "\n", timeout=self.timeout,
                )
                if rc == 0:
                    break
                log.error("muse exec exited %s in the sandbox (try %s of %s): %s", rc, attempt, MUSE_TRIES, err.replace(os.environ["META_API_KEY"], "<key>")[-1500:])
                if attempt < MUSE_TRIES:
                    await asyncio.sleep(RETRY_WAIT * attempt)
        except Exception as exc:
            if "imeout" in type(exc).__name__ or isinstance(exc, TimeoutError):
                raise RuntimeError("a review pass timed out") from exc
            log.exception("the review sandbox failed")
            raise RuntimeError("the review sandbox failed") from exc
        if rc != 0:
            raise RuntimeError("Muse failed to run")
        return out
