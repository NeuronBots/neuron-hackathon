"""The image script Muse runs in the sandbox."""
import base64
import io
import json
import urllib.error
import urllib.request

import pytest

from services import imagegen


@pytest.fixture
def script(tmp_path, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "k-123")
    monkeypatch.setattr(imagegen, "MAX", 2)
    source = imagegen.files()[imagegen.SCRIPT_PATH]
    path = tmp_path / ".neuron" / "genimage.py"
    path.parent.mkdir()
    path.write_text(source)
    ns = {"__file__": str(path), "__name__": "genimage"}
    exec(compile(source, str(path), "exec"), ns)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(imagegen.ENV, "k-123")
    return ns["main"], tmp_path


def reply(monkeypatch, data=None, error=None):
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, json.loads(request.data), request.get_header("Authorization")))
        if error:
            raise error
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


PICTURE = {"data": [{"b64_json": base64.b64encode(b"RIFFwebp").decode()}], "output_format": "webp"}


def test_a_picture_is_written_where_asked_with_the_format_the_service_says(script, monkeypatch, capsys):
    main, root = script
    seen = reply(monkeypatch, PICTURE)
    assert main(["a red candy jar", "public/images/hero.png"]) == 0
    assert (root / "public/images/hero.webp").read_bytes() == b"RIFFwebp"  # the extension follows the format returned
    assert seen == [("https://api.meta.ai/v1/images/generations", {"model": "muse-image-1.0", "prompt": "a red candy jar", "n": 1}, "Bearer k-123")]
    assert "1 images left" in capsys.readouterr().out


def test_it_stops_at_the_limit_and_refuses_paths_outside_the_project(script, monkeypatch):
    main, root = script
    reply(monkeypatch, PICTURE)
    assert main(["a", "a.webp"]) == 0 and main(["b", "b.webp"]) == 0
    assert main(["c", "c.webp"]) == 1 and not (root / "c.webp").exists()
    assert main(["d", "../escape.webp"]) == 2 and not (root.parent / "escape.webp").exists()


def test_failures_are_said_plainly_and_write_nothing(script, monkeypatch, capsys):
    main, root = script
    reply(monkeypatch, error=urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"no such model")))
    assert main(["a", "a.webp"]) == 1 and "400" in capsys.readouterr().err and not (root / "a.webp").exists()
    reply(monkeypatch, {"data": []})
    assert main(["a", "a.webp"]) == 1
    monkeypatch.delenv(imagegen.ENV)
    assert main(["a", "a.webp"]) == 1 and "not available" in capsys.readouterr().err


def test_it_is_off_without_a_key_or_with_a_zero_limit(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "k")
    monkeypatch.setattr(imagegen, "MAX", 0)
    assert imagegen.files() == {} and imagegen.env() == {}
    monkeypatch.setattr(imagegen, "MAX", 3)
    monkeypatch.delenv("META_API_KEY")
    assert imagegen.files() == {} and imagegen.env() == {}
