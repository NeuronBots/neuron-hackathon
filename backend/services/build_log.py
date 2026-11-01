"""What Muse prints while it builds, kept in memory for the build card's terminal. Lost on a restart, like the build itself."""
import time
from collections import deque

KEEP = 500  # lines per build
LINE = 400  # characters per line
_logs: dict[int, dict] = {}


def add(row: int, text: str) -> None:
    box = _logs.setdefault(row, {"lines": deque(maxlen=KEEP), "count": 0, "at": 0.0})
    for line in text.replace("\r", "\n").split("\n"):
        if line.strip():
            box["lines"].append(line[:LINE])
            box["count"] += 1
    box["at"] = time.monotonic()
    for old in [k for k, v in _logs.items() if time.monotonic() - v["at"] > 6 * 3600]:  # a finished build's terminal is kept for hours, not forever
        del _logs[old]


def since(row: int, after: int) -> tuple[list[str], int]:
    """The lines after the first `after` ones that were ever printed (some may have scrolled out), and the new count."""
    box = _logs.get(row)
    if not box:
        return [], after
    lines, count = list(box["lines"]), box["count"]
    return lines[max(0, len(lines) - max(0, count - after)):], count
