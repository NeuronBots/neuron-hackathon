"""Image generation for the build: Muse has no image tool of its own, so the sandbox gets a small script it can run from its shell.

`.neuron/genimage.py "<prompt>" public/images/hero.webp` calls the Meta API's image model (muse-image-1.0) and writes the picture into the workspace.
The key is given to the Muse process only (the environment of `muse exec`, see BuildBox.build), never written to a file; the sandbox can reach api.meta.ai
anyway, which is where Muse itself talks. `.neuron/` stays out of the archive and the diff, so the script is not part of the project; the images are.
BUILD_IMAGES_MAX caps the pictures of one sandbox (they cost money); 0 turns the whole thing off."""
from __future__ import annotations

import os

SCRIPT_PATH = ".neuron/genimage.py"
ENV = "NEURON_IMAGE_KEY"
MODEL = os.getenv("BUILD_IMAGE_MODEL", "muse-image-1.0")
BASE = os.getenv("META_BASE_URL", "https://api.meta.ai/v1").rstrip("/")
MAX = int(os.getenv("BUILD_IMAGES_MAX", "12"))
TOKENS_PER_IMAGE = int(os.getenv("BUILD_IMAGE_TOKENS", "2000"))  # the image call reports no tokens we can read, so each picture counts toward the plan as this many output tokens

SCRIPT = '''"""Make a picture for this project: python3 .neuron/genimage.py "<prompt>" <path inside the project, e.g. public/images/hero.webp>"""
import base64, json, os, pathlib, sys, urllib.error, urllib.request

URL, MODEL, LIMIT = "%(url)s", "%(model)s", %(limit)d
COUNT = pathlib.Path(__file__).with_name("images.count")


def main(argv):
    if len(argv) != 2 or not argv[0].strip():
        print('usage: python3 .neuron/genimage.py "<prompt>" <output path>', file=sys.stderr)
        return 2
    prompt, out = argv
    root = pathlib.Path.cwd().resolve()
    target = (root / out).resolve()
    if root not in target.parents:
        print("the output path must be inside the project", file=sys.stderr)
        return 2
    key = os.environ.get("%(env)s")
    if not key:
        print("image generation is not available in this run", file=sys.stderr)
        return 1
    made = int(COUNT.read_text() or 0) if COUNT.exists() else 0
    if made >= LIMIT:
        print(f"image limit reached ({LIMIT}): reuse the images already made", file=sys.stderr)
        return 1
    body = json.dumps({"model": MODEL, "prompt": prompt, "n": 1}).encode()
    request = urllib.request.Request(URL, body, {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=180) as reply:
                data = json.load(reply)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if attempt == 2 or exc.code < 500:
                print(f"the image service refused the request ({exc.code}): {detail}", file=sys.stderr)
                return 1
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2:
                print(f"the image service could not be reached: {exc}", file=sys.stderr)
                return 1
    pictures = data.get("data") or []
    if not pictures or not pictures[0].get("b64_json"):
        print("the image service returned no picture", file=sys.stderr)
        return 1
    target = target.with_suffix("." + (data.get("output_format") or "webp"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(pictures[0]["b64_json"]))
    COUNT.write_text(str(made + 1))
    print(f"wrote {target.relative_to(root)} ({target.stat().st_size} bytes); {LIMIT - made - 1} images left")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def pictures_made(count_file: str | None) -> int:
    """How many pictures the sandbox made, from the script's own counter file."""
    try:
        return max(0, int((count_file or "0").strip() or 0))
    except ValueError:
        return 0


def enabled() -> bool:
    return MAX > 0 and bool(os.environ.get("META_API_KEY"))


def files() -> dict[str, str]:
    """The script, as a file of the workspace (none when image generation is off)."""
    if not enabled():
        return {}
    return {SCRIPT_PATH: SCRIPT % {"url": f"{BASE}/images/generations", "model": MODEL, "limit": MAX, "env": ENV}}


def env() -> dict[str, str]:
    """What the Muse process is given so the script can call the model."""
    return {ENV: os.environ["META_API_KEY"]} if enabled() else {}
