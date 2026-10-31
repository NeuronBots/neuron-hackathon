"""A starter app that is in the workspace of a new project's first build before Muse writes a line.

The live preview (services/preview.py) starts the app as soon as the files say how to. Without a starter that waits for Muse to read the plan and write a
skeleton, minutes in which the person sees nothing. The starter is a Vite page ("Muse is building <name>") whose dependencies are installed in the sandbox
image (build_sandbox._image), so the dev server is up within seconds and hot-reloads as Muse replaces it. Muse builds on it or replaces it, whatever the plan needs."""
from __future__ import annotations

import html
import json

DIR = "/opt/starter"  # in the sandbox image: package.json and its installed node_modules
PACKAGE = {"name": "app", "private": True, "version": "0.0.0", "type": "module", "scripts": {"dev": "vite", "build": "vite build"}, "devDependencies": {"vite": "^6.0.0"}}


def package_json() -> str:
    return json.dumps(PACKAGE, indent=2) + "\n"


def files(name: str) -> dict[str, str]:
    """The starter's files, by path relative to the workspace."""
    title = html.escape(name)
    return {
        "package.json": package_json(),
        "index.html": (
            '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="UTF-8" />\n<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            f"<title>{title}</title>\n</head>\n<body>\n<main>\n<h1>{title}</h1>\n<p>Muse is building this now. The page updates as the code is written.</p>\n"
            '<div class="bar" aria-hidden="true"></div>\n</main>\n<script type="module" src="/src/main.js"></script>\n</body>\n</html>\n'
        ),
        "src/main.js": "import './style.css'\n",
        "src/style.css": (
            "body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e0f12;color:#e8e8ea;font:16px/1.5 system-ui,sans-serif}"
            "main{padding:2rem;text-align:center;max-width:32rem}h1{font-size:clamp(1.5rem,6vw,2.25rem);margin:0 0 .5rem}p{margin:0 0 1.5rem;opacity:.7}"
            ".bar{height:4px;border-radius:2px;background:linear-gradient(90deg,transparent,#7c8cff,transparent);background-size:200% 100%;animation:s 1.4s linear infinite}"
            "@keyframes s{to{background-position:-200% 0}}\n"
        ),
    }
