"""PR reviews can run Muse in a Modal sandbox. These use a fake Modal: they check what is sent to the sandbox and in what order, not Modal itself."""
import asyncio
import json
import sys
import types

import httpx
import pytest

from services import code_review, review_sandbox

KEY = "meta-secret-key-123"


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.stdin = types.SimpleNamespace(written=[], eof=False)
        self.stdin.write = self.stdin.written.append
        self.stdin.write_eof = lambda: setattr(self.stdin, "eof", True)

        async def drain():
            pass
        self.stdin.drain = types.SimpleNamespace(aio=drain)
        self.stdout = types.SimpleNamespace(read=types.SimpleNamespace(aio=self._read(out)))
        self.stderr = types.SimpleNamespace(read=types.SimpleNamespace(aio=self._read(err)))
        self.wait = types.SimpleNamespace(aio=self._wait)

    @staticmethod
    def _read(value):
        async def read():
            return value
        return read

    async def _wait(self):
        return self.rc


class FakeSandbox:
    def __init__(self, muse_rc=0, muse_out='{"ok": 1}', muse_err="", fail_on=None):
        self.calls, self.files, self.procs, self.terminated = [], {}, [], False
        self.muse_rc, self.muse_out, self.muse_err, self.fail_on = muse_rc, muse_out, muse_err, fail_on

        async def write_bytes(data, path):
            self.calls.append(("write", path))
            self.files[path] = data

        async def write_text(data, path):
            self.calls.append(("write", path))
            self.files[path] = data

        async def exec_(*args, workdir=None, env=None, timeout=None):
            self.calls.append(("exec", args[0]))
            rc = 1 if args[0] == self.fail_on else 0
            proc = Proc(self.muse_rc, self.muse_out, self.muse_err) if args[0] == "muse" else Proc(rc, "", "boom" if rc else "")
            proc.args, proc.env, proc.timeout = args, env, timeout
            self.procs.append(proc)
            return proc

        async def terminate():
            self.terminated = True

        async def set_tags(tags):
            self.tags = tags
        self.object_id = "sb-test"
        self.set_tags = types.SimpleNamespace(aio=set_tags)
        self.filesystem = types.SimpleNamespace(write_bytes=types.SimpleNamespace(aio=write_bytes), write_text=types.SimpleNamespace(aio=write_text))
        self.exec = types.SimpleNamespace(aio=exec_)
        self.terminate = types.SimpleNamespace(aio=terminate)


@pytest.fixture
def fake_modal(monkeypatch):
    """A stand-in `modal` module whose Sandbox.create returns a FakeSandbox, and records how it was created."""
    box = types.SimpleNamespace(sandbox=FakeSandbox(), created=[], fail_create=False)

    async def lookup(name, create_if_missing=False):
        return f"app:{name}"

    async def create(**kwargs):
        if box.fail_create:
            raise RuntimeError("no credentials")
        box.created.append(kwargs)
        return box.sandbox

    mod = types.ModuleType("modal")
    mod.App = types.SimpleNamespace(lookup=types.SimpleNamespace(aio=lookup))
    mod.Sandbox = types.SimpleNamespace(create=types.SimpleNamespace(aio=create))
    mod.Image = types.SimpleNamespace(debian_slim=lambda: types.SimpleNamespace(
        apt_install=lambda *a: types.SimpleNamespace(run_commands=lambda *c: ("image", c))))
    monkeypatch.setitem(sys.modules, "modal", mod)
    monkeypatch.setenv("META_API_KEY", KEY)
    return box


def run(coro):
    return asyncio.run(coro)


def test_the_sandbox_reaches_only_the_model_api_and_holds_no_secrets(fake_modal):
    async def go():
        async with review_sandbox.SandboxBox("low", 60):
            pass
    run(go())
    (kwargs,) = fake_modal.created
    assert kwargs["outbound_domain_allowlist"] == review_sandbox.DOMAINS == ["api.meta.ai"]
    assert "secrets" not in kwargs and KEY not in json.dumps(kwargs, default=str)
    assert fake_modal.sandbox.terminated  # one sandbox per review, destroyed after


def test_the_key_goes_in_over_stdin_and_nowhere_else(fake_modal):
    async def go():
        async with review_sandbox.SandboxBox("medium", 99) as box:
            await box.load(b"tar", {".review/pr.diff": "d"})
            return await box.muse("the prompt", {"type": "object"})
    assert run(go()) == '{"ok": 1}'
    sb = fake_modal.sandbox
    muse = next(p for p in sb.procs if p.args[0] == "muse")
    assert muse.stdin.written == [KEY + "\n"] and muse.stdin.eof
    assert KEY not in " ".join(muse.args) and KEY not in json.dumps(muse.env) and all(KEY not in str(v) for v in sb.files.values())
    assert "--api-key-stdin" in muse.args and muse.timeout == 99 and muse.args[muse.args.index("--reasoning-effort") + 1] == "medium"
    assert {"--disable-write", "--disable-shell", "--disable-web-tools", "--no-session-log"} <= set(muse.args)
    assert "--trust-workspace" not in muse.args
    prompt = next(v for k, v in sb.files.items() if k.startswith("/work/prompt-") and k.endswith(".txt"))
    schema = next(v for k, v in sb.files.items() if k.startswith("/work/schema-") and k.endswith(".json"))
    assert prompt == "the prompt" and json.loads(schema) == {"type": "object"}
    assert muse.args[muse.args.index("--prompt-file") + 1].startswith("/work/prompt-")


def test_the_code_is_unpacked_and_stripped_of_symlinks_before_muse_reads_it(fake_modal):
    async def go():
        async with review_sandbox.SandboxBox("low", 60) as box:
            await box.load(b"tarbytes", {".review/pr.diff": "diff", ".review/files.txt": "list"})
            await box.muse("p", {})
    run(go())
    sb = fake_modal.sandbox
    assert sb.files["/work/repo.tgz"] == b"tarbytes"
    execs = [p.args for p in sb.procs]
    assert execs.index(next(a for a in execs if a[0] == "tar")) < execs.index(next(a for a in execs if a[:3] == ("find", "/work/repo", "-type"))) < execs.index(next(a for a in execs if a[0] == "muse"))
    assert next(a for a in execs if a[0] == "find")[-2:] == ("l", "-delete")
    assert sb.files["/work/repo/.review/pr.diff"] == "diff" and sb.files["/work/repo/.review/files.txt"] == "list"
    writes = [path for kind, path in sb.calls if kind == "write"]
    assert writes.index("/work/repo/.review/pr.diff") > 0 and "/work/repo.tgz" in writes[:1]  # the review's files land after the archive is unpacked


def test_a_failed_unpack_or_muse_fails_the_review_and_still_destroys_the_sandbox(fake_modal, caplog, monkeypatch):
    monkeypatch_sleep = lambda secs: monkeypatch.setattr(review_sandbox, "RETRY_WAIT", secs)
    fake_modal.sandbox.fail_on = "tar"

    async def bad_unpack():
        async with review_sandbox.SandboxBox("low", 60) as box:
            await box.load(b"x", {})
    with pytest.raises(RuntimeError, match="unpack"):
        run(bad_unpack())
    assert fake_modal.sandbox.terminated

    monkeypatch_sleep(0)
    fake_modal.sandbox = FakeSandbox(muse_rc=2, muse_err=f"auth failed for {KEY}")

    async def bad_muse():
        async with review_sandbox.SandboxBox("low", 60) as box:
            await box.muse("p", {})
    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="Muse failed to run"):
            run(bad_muse())
    assert KEY not in caplog.text and "<key>" in caplog.text and fake_modal.sandbox.terminated
    assert sum(p.args[0] == "muse" for p in fake_modal.sandbox.procs) == review_sandbox.MUSE_TRIES  # it was tried again before giving up


def test_no_key_or_no_sandbox_fails_the_review_and_never_falls_back_to_this_server(fake_modal, monkeypatch):
    async def enter():
        async with review_sandbox.SandboxBox("low", 60):
            pass
    fake_modal.fail_create = True
    with pytest.raises(RuntimeError, match="could not start the review sandbox"):
        run(enter())
    fake_modal.fail_create = False
    monkeypatch.delenv("META_API_KEY")
    with pytest.raises(RuntimeError, match="META_API_KEY"):
        run(enter())
    assert fake_modal.created == []


def test_a_review_with_the_modal_runner_never_checks_the_code_out_here(monkeypatch, fake_modal):
    """The sandbox gets the tarball and the review's files; the passes run against it; the GitHub token never leaves this server."""
    seen = {}
    pr = {"number": 7, "title": "t", "head": {"sha": "abc123"}, "base": {}}
    files = [{"path": "a.py", "status": "modified", "patch": "@@", "add": 1, "del": 0}]

    async def token(repo, user):
        return "ghs_TOKEN"

    async def changed(repo, number, tok):
        return files

    async def tarball(repo, sha, tok):
        seen["tarball"] = (repo, sha, tok)
        return b"archive"

    async def checkout(*a, **k):
        raise AssertionError("the PR's code must not be checked out on this server")

    async def muse(text, schema, cleaner, box):
        seen.setdefault("boxes", []).append(box)
        return cleaner(json.dumps({"summary": "s", "reviewed_files": ["a.py"], "findings": [], "overall": "fine", "verdict": "approve"})) if "findings" in schema.get("properties", {}) else {}

    monkeypatch.setenv("REVIEW_RUNNER", "modal")
    monkeypatch.setattr(code_review, "_token", token)
    monkeypatch.setattr(code_review, "changed_files", changed)
    monkeypatch.setattr(code_review, "fetch_tarball", tarball)
    monkeypatch.setattr(code_review, "checkout", checkout)
    monkeypatch.setattr(code_review, "run_muse", muse)

    result = run(code_review.review("o/r", pr, "u", "ctx"))

    assert seen["tarball"] == ("o/r", "abc123", "ghs_TOKEN")
    assert all(isinstance(b, review_sandbox.SandboxBox) for b in seen["boxes"]) and len(seen["boxes"]) >= 2  # the walkthrough and the findings
    sb = fake_modal.sandbox
    assert sb.files["/work/repo.tgz"] == b"archive" and "a.py (modified, +1 -0)" in sb.files["/work/repo/.review/files.txt"]
    assert "ghs_TOKEN" not in json.dumps(list(sb.files.values()), default=str) and "ghs_TOKEN" not in json.dumps([p.args for p in sb.procs])
    assert result["verdict"] == "approve" and sb.terminated


def test_the_default_runner_is_still_this_server(monkeypatch):
    monkeypatch.delenv("REVIEW_RUNNER", raising=False)
    ran = {}

    async def token(repo, user):
        return "t"

    async def changed(*a):
        return [{"path": "a.py", "status": "added", "patch": "", "add": 1, "del": 0}]

    async def checkout(repo, sha, tok, dest):
        ran["checkout"] = (repo, sha)

    async def muse(text, schema, cleaner, box):
        ran.setdefault("boxes", []).append(type(box).__name__)
        return cleaner(json.dumps({"summary": "s", "reviewed_files": ["a.py"], "findings": []})) if "findings" in schema.get("properties", {}) else {}

    monkeypatch.setattr(code_review, "_token", token)
    monkeypatch.setattr(code_review, "changed_files", changed)
    monkeypatch.setattr(code_review, "checkout", checkout)
    monkeypatch.setattr(code_review, "run_muse", muse)
    run(code_review.review("o/r", {"number": 1, "head": {"sha": "s1"}}, "u"))
    assert ran["checkout"] == ("o/r", "s1") and set(ran["boxes"]) == {"LocalBox"}


_REAL_CLIENT = httpx.AsyncClient  # kept before any patch: a second patch must wrap this, not the first one


def _client(monkeypatch, handler):
    monkeypatch.setattr(code_review.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_the_tarball_is_downloaded_with_the_token_and_capped(monkeypatch):
    asked = []

    def handler(request):
        asked.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, content=b"x" * 50)
    _client(monkeypatch, handler)
    assert run(code_review.fetch_tarball("o/r", "abc", "tok")) == b"x" * 50
    assert asked == [("https://api.github.com/repos/o/r/tarball/abc", "Bearer tok")]

    monkeypatch.setattr(code_review, "MAX_TARBALL", 10)
    with pytest.raises(RuntimeError, match="too large"):
        run(code_review.fetch_tarball("o/r", "abc", "tok"))

    _client(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(RuntimeError, match="404"):
        run(code_review.fetch_tarball("o/r", "abc", "tok"))


# ── Follow-up reviews use the same box ──

def _followup_world(monkeypatch):
    from services import followup
    files_since = [{"path": "new.py", "status": "added", "patch": "+NEW", "add": 1, "del": 0}]
    whole_pr = files_since + [{"path": "old.py", "status": "modified", "patch": "+OLD", "add": 1, "del": 0}]
    previous = [{"id": "f1", "severity": "major", "title": "T", "file": "old.py", "line": 1, "why": "w", "thread": "th1"}]
    seen = {"boxes": []}

    async def token(repo, owner):
        return "ghs_TOKEN"

    async def compare(repo, since, sha, tok):
        return files_since

    async def prev(repo, number):
        return previous

    async def changed(repo, number, tok):
        return whole_pr

    async def muse(text, schema, cleaner, box):
        seen["boxes"].append(box)
        return {"f1": {"status": "fixed", "note": "n"}} if "previous" in schema.get("properties", {}) else {
            "summary": "s", "overall": "", "verdict": "approve", "findings": [], "reviewed_files": ["new.py"]}

    monkeypatch.setattr(code_review, "_token", token)
    monkeypatch.setattr(followup.github_app, "compare_files", compare)
    monkeypatch.setattr(followup, "previous_findings", prev)
    monkeypatch.setattr(code_review, "changed_files", changed)
    monkeypatch.setattr(code_review, "run_muse", muse)
    return followup, seen


def test_a_follow_up_in_the_sandbox_gets_only_what_changed_since_as_its_diff(monkeypatch, fake_modal):
    followup, seen = _followup_world(monkeypatch)

    async def tarball(repo, sha, tok):
        return b"head"
    monkeypatch.setattr(code_review, "fetch_tarball", tarball)
    monkeypatch.setenv("REVIEW_RUNNER", "modal")

    result = run(followup.review("o/r", {"number": 3, "head": {"sha": "newsha"}}, "owner", "ctx", "oldsha"))

    sb = fake_modal.sandbox
    assert result["followup"] and result["fixed"] and sb.terminated
    assert len(seen["boxes"]) == 2 and all(isinstance(b, review_sandbox.SandboxBox) for b in seen["boxes"])  # the re-check, then the new changes
    assert "+NEW" in sb.files["/work/repo/.review/pr.diff"] and "+OLD" not in sb.files["/work/repo/.review/pr.diff"]
    assert "ghs_TOKEN" not in json.dumps(list(sb.files.values()), default=str)


def test_a_follow_up_on_this_server_still_checks_out_and_cleans_up(monkeypatch, tmp_path):
    followup, seen = _followup_world(monkeypatch)
    monkeypatch.delenv("REVIEW_RUNNER", raising=False)
    made = []

    async def checkout(repo, sha, tok, dest):
        made.append(dest)
    monkeypatch.setattr(code_review, "checkout", checkout)

    run(followup.review("o/r", {"number": 3, "head": {"sha": "newsha"}}, "owner", "ctx", "oldsha"))

    assert len(made) == 1 and {type(b).__name__ for b in seen["boxes"]} == {"LocalBox"}
    import os
    assert not os.path.exists(made[0])  # the temp checkout is gone


def test_a_muse_call_that_drops_once_is_run_again_and_succeeds(fake_modal, monkeypatch):
    monkeypatch.setattr(review_sandbox, "RETRY_WAIT", 0)
    sb = fake_modal.sandbox
    results = iter([(1, "", "api dropped"), (0, '{"ok": 2}', "")])
    original = sb.exec.aio

    async def flaky(*args, **kw):
        proc = await original(*args, **kw)
        if args[0] == "muse":
            proc.rc, proc.out = next(results)[:2]
            proc.stdout = types.SimpleNamespace(read=types.SimpleNamespace(aio=Proc._read(proc.out)))
            proc.wait = types.SimpleNamespace(aio=proc._wait)
        return proc
    sb.exec = types.SimpleNamespace(aio=flaky)

    async def go():
        async with review_sandbox.SandboxBox("low", 60) as box:
            return await box.muse("p", {})
    assert run(go()) == '{"ok": 2}'
    assert sum(p.args[0] == "muse" for p in sb.procs) == 2


def test_only_unheld_sandboxes_past_the_grace_period_are_orphans():
    from services import review_sandbox as rs

    now = 10_000.0
    found = [("held", {"started": "1"}), ("old", {"started": "1"}), ("new", {"started": str(now - 10)}), ("untimed", {})]
    assert rs.orphans(found, {"held"}, now) == ["old", "untimed"]  # held ones and ones still being set up are left alone


def test_a_sandbox_is_tagged_with_this_server_while_it_is_open_and_forgotten_when_it_closes(fake_modal):
    async def go():
        async with review_sandbox.SandboxBox("low", 60):
            assert "sb-test" in review_sandbox._held  # the reaper must leave an open one alone
    run(go())
    assert fake_modal.sandbox.tags["owner"] == review_sandbox.OWNER and "sb-test" not in review_sandbox._held
