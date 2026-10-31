"""The sandbox where Muse builds: what it can reach, how Muse is started in it, and what is read back out."""
import asyncio
import json
import types

import pytest

from services import build_sandbox as bsb, review_sandbox as rs
from test_review_sandbox import FakeSandbox, Proc  # the stand-in for Modal's sandbox that the review tests use

KEY = "meta-secret-key-123"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_modal(monkeypatch):
    box = types.SimpleNamespace(sandbox=FakeSandbox(), created=[])

    async def lookup(name, create_if_missing=False):
        return f"app:{name}"

    async def create(**kwargs):
        box.created.append(kwargs)
        return box.sandbox

    mod = types.ModuleType("modal")
    mod.App = types.SimpleNamespace(lookup=types.SimpleNamespace(aio=lookup))
    mod.Sandbox = types.SimpleNamespace(create=types.SimpleNamespace(aio=create))
    chain = types.SimpleNamespace()
    chain.apt_install = lambda *a: chain
    chain.run_commands = lambda *c: chain
    mod.Image = types.SimpleNamespace(debian_slim=lambda: chain)
    monkeypatch.setitem(__import__("sys").modules, "modal", mod)
    monkeypatch.setenv("META_API_KEY", KEY)
    return box


def test_the_build_sandbox_is_bigger_and_reaches_the_registries_but_nothing_else(fake_modal):
    async def go():
        async with bsb.BuildBox("high", 60):
            pass
    run(go())
    (kw,) = fake_modal.created
    assert set(kw["outbound_domain_allowlist"]) == {"api.meta.ai", "registry.npmjs.org", "pypi.org", "files.pythonhosted.org"}
    assert (kw["memory"], kw["cpu"], kw["timeout"]) == (bsb.MEMORY_MB, bsb.CPU, bsb.LIFETIME) and kw["memory"] > rs.MEMORY_MB
    assert KEY not in json.dumps(kw, default=str) and fake_modal.sandbox.terminated


def test_a_review_sandbox_is_still_small_and_reaches_only_the_model(fake_modal):
    async def go():
        async with rs.SandboxBox("low", 60):
            pass
    run(go())
    (kw,) = fake_modal.created
    assert kw["outbound_domain_allowlist"] == ["api.meta.ai"] and kw["memory"] == rs.MEMORY_MB and kw["cpu"] == 1.0


def build_command(fake_modal, **kw):
    async def go():
        async with bsb.BuildBox(kw.get("effort", "high"), 123) as box:
            return await box.build("the prompt")
    out = run(go())
    muse = next(p for p in fake_modal.sandbox.procs if p.args[0] == "muse")
    return out, muse


def test_muse_is_started_to_build_with_its_skills_loaded_and_a_working_shell(fake_modal):
    out, muse = build_command(fake_modal)
    args = muse.args
    assert out == '{"ok": 1}'
    assert "--trust-workspace" in args  # without it Muse silently skips the project's skills and rules
    assert "--disable-sandbox" in args  # its own shell sandbox cannot start inside this one: every command failed with bwrap: loopback
    assert "--disable-write" not in args and "--disable-shell" not in args  # it writes code and runs it
    assert "--disable-web-tools" in args and args[args.index("--workspace") + 1] == rs.REPO
    assert args[args.index("--reasoning-effort") + 1] == "high" and muse.timeout == 123
    assert muse.stdin.written == [KEY + "\n"] and KEY not in " ".join(args) and muse.env == {"HOME": rs.WORK, "NEURON_IMAGE_KEY": KEY}  # the key goes in over stdin, and in the Muse process's environment for the image script: never in the command line
    assert fake_modal.sandbox.files[f"{rs.WORK}/build-prompt.txt"] == "the prompt"


def test_a_failed_muse_run_stops_the_build_and_never_shows_the_key(fake_modal, caplog):
    fake_modal.sandbox = FakeSandbox(muse_rc=2, muse_err=f"auth failed for {KEY}")

    async def go():
        async with bsb.BuildBox("high", 60) as box:
            await box.build("p")
    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="stopped before the build was finished"):
            run(go())
    assert KEY not in caplog.text and fake_modal.sandbox.terminated


def test_a_timeout_is_reported_as_one(fake_modal):
    class Timeout(Exception):
        pass
    Timeout.__name__ = "ExecTimeoutError"

    async def slow(*a, **k):
        raise Timeout("too long")
    fake_modal.sandbox.exec = types.SimpleNamespace(aio=slow)

    async def go():
        async with bsb.BuildBox("high", 1) as box:
            await box.build("p")
    with pytest.raises(RuntimeError, match="took too long"):
        run(go())


def test_the_files_are_listed_without_dependencies_or_the_skills_we_added(fake_modal):
    fake_modal.sandbox.exec_out = None
    original = fake_modal.sandbox.exec.aio

    async def exec_(*args, **kw):
        proc = await original(*args, **kw)
        if args[0] == "sh" and "find" in args[2]:
            listing = "\n".join(["app.py", "tests/test_app.py", "RUN.md", ".agents/skills/ponytail/SKILL.md", ".agents/skills/frontend-design/LICENSE.txt",
                                 ".agents/skills/mine/SKILL.md", "node_modules/x/index.js", "a/__pycache__/m.pyc", "web/dist/app.js", ".neuron/PLAN.md", "x.pyc"])
            proc.stdout = types.SimpleNamespace(read=types.SimpleNamespace(aio=Proc._read(listing)))
        return proc
    fake_modal.sandbox.exec = types.SimpleNamespace(aio=exec_)

    async def go():
        async with bsb.BuildBox("high", 60) as box:
            return await box.files()
    # the find command also prunes the heavy folders inside the sandbox; this filter is the second line, and it drops our own skills
    assert run(go()) == ["app.py", "tests/test_app.py", "RUN.md", ".agents/skills/mine/SKILL.md"]
    find = next(p for p in fake_modal.sandbox.procs if p.args[0] == "sh").args[2]
    assert "-prune" in find and all(f"-name {d}" in find for d in ("node_modules", ".venv", ".git", "__pycache__", "dist"))


def test_the_archive_leaves_out_what_is_rebuilt_and_what_we_added_and_is_size_capped(fake_modal, monkeypatch):
    fake_modal.sandbox.files[f"{rs.WORK}/out.zip"] = b"PKzip"
    original = fake_modal.sandbox.exec.aio

    async def exec_(*args, **kw):
        proc = await original(*args, **kw)
        if args[0] == "sh" and "wc -c" in args[2]:
            proc.stdout = types.SimpleNamespace(read=types.SimpleNamespace(aio=Proc._read("5\n")))
        return proc
    fake_modal.sandbox.exec = types.SimpleNamespace(aio=exec_)

    async def read_bytes(path):
        return fake_modal.sandbox.files[path]
    fake_modal.sandbox.filesystem.read_bytes = types.SimpleNamespace(aio=read_bytes)

    async def go():
        async with bsb.BuildBox("high", 60) as box:
            return await box.export()
    assert run(go()) == b"PKzip"
    zip_cmd = next(p for p in fake_modal.sandbox.procs if p.args[0] == "zip").args
    excluded = [zip_cmd[i + 1] for i, a in enumerate(zip_cmd) if a == "-x"]
    for must in ("node_modules/*", "*/node_modules/*", ".git/*", ".neuron/*", ".agents/skills/ponytail/*", ".agents/skills/frontend-design/*", ".agents/skills/single-responsibility/*"):
        assert must in excluded

    monkeypatch.setattr(bsb, "MAX_ARCHIVE", 3)
    with pytest.raises(RuntimeError, match="too large to save"):
        run(go())


def test_a_missing_file_reads_as_none_and_the_rules_file_can_be_written_or_removed(fake_modal):
    async def go():
        async with bsb.BuildBox("high", 60) as box:
            missing = await box.read("RUN.md")
            await box.write("AGENTS.md", "rules")
            await box.write("AGENTS.md", None)
            return missing
    fake_modal.sandbox.filesystem.read_text = types.SimpleNamespace(aio=lambda path: (_ for _ in ()).throw(FileNotFoundError(path)))
    assert run(go()) is None
    assert fake_modal.sandbox.files[f"{rs.REPO}/AGENTS.md"] == "rules"
    assert any(p.args[:2] == ("rm", "-f") for p in fake_modal.sandbox.procs)


def test_a_review_run_cannot_write_but_can_run_things_and_answers_in_the_schema(fake_modal):
    async def go():
        async with bsb.BuildBox("high", 60) as box:
            return await box.inspect("review it", {"type": "object"})
    assert run(go()) == '{"ok": 1}'
    muse = next(p for p in fake_modal.sandbox.procs if p.args[0] == "muse")
    args = muse.args
    assert "--disable-write" in args and "--disable-shell" not in args and "--disable-sandbox" in args and "--trust-workspace" in args
    schema = args[args.index("--output-schema") + 1]
    assert json.loads(fake_modal.sandbox.files[schema]) == {"type": "object"} and muse.stdin.written == [KEY + "\n"]
    assert any(v == "review it" for v in fake_modal.sandbox.files.values())


def test_a_build_start_that_cannot_fetch_muses_model_list_is_tried_again(monkeypatch):
    import asyncio
    from services import build_sandbox as bs

    monkeypatch.setenv("META_API_KEY", "k")
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bs.asyncio, "sleep", lambda s: real_sleep(0))
    box = bs.BuildBox("low", 60)

    class Files:
        class write_text:
            @staticmethod
            async def aio(*a):
                return None

    class Sb:
        filesystem = Files
    box.sb = Sb
    answers = [(1, "", "failed to fetch model catalog: model catalog has no visible models"), (0, "built", "")]

    async def fake_exec(*a, **k):
        return answers.pop(0)
    box._exec = fake_exec
    assert asyncio.run(box.build("p")) == "built" and not answers
