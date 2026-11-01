"""What `muse exec --json` says while it works: the words it prints for the build card's terminal, the closing message, and the tokens it used.

Muse runs in a sandbox with its own key, so its calls never pass through this server's model client, and the plan's token limits did not see them.
Its `run.usage` event reports every model call's tokens; they are recorded here against the person whose build it is, like any other turn."""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

SKIP_TASKS = ("model.", "reminder.")  # bookkeeping, not something the terminal should show


def _events(stdout: str):
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("payload"), dict):
            yield event["payload_type"] if "payload_type" in event else "", event["payload"]


def parse(stdout: str) -> tuple[str, dict[str, int]]:
    """(Muse's closing message, the tokens of the run). Both come from the JSON events; when there are none the output is returned as it is."""
    closing, spoken, totals, seen = "", [], {"input": 0, "cached": 0, "output": 0}, False
    for kind, payload in _events(stdout):
        seen = True
        if kind == "run.terminal.completed" and isinstance(payload.get("text"), str):
            closing = payload["text"]
        elif kind == "run.output.delta" and isinstance(payload.get("text"), str):
            spoken.append(payload["text"])
        elif kind == "run.usage":
            for m in payload.get("models") or []:
                if not isinstance(m, dict):
                    continue
                totals["input"] += int(m.get("input_tokens") or 0)
                totals["cached"] += int(m.get("cached_tokens") or 0)
                totals["output"] += int(m.get("output_tokens") or 0) + int(m.get("reasoning_tokens") or 0)
    return (closing or "".join(spoken) if seen else stdout), totals


def record(totals: dict[str, int], user_id: str | None = None, agent_id: str | None = None) -> None:
    """Count the run toward the person's plan: the same ledger the bots' own turns write to. Never raises."""
    if not any(totals.values()):
        return
    try:
        from core.usage import record_usage

        record_usage("muse", {"input_tokens": totals["input"], "output_tokens": totals["output"], "cache_read_input_tokens": totals["cached"]},
                     user_id=user_id, agent_id=agent_id, kind="muse")
    except Exception:
        log.exception("Could not record Muse's token usage")


class Feed:
    """Turns Muse's JSON events into the lines of a terminal: what it says, one line at a time, and the tools it starts. Anything that is not an event passes through."""

    def __init__(self, sink):
        self.sink, self.buffer = sink, ""

    def _say(self, text: str) -> None:
        if text.strip():
            self.sink(text)

    def feed(self, chunk: str) -> None:
        for line in chunk.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                self._say(line)  # stderr, or a line of plain text
                continue
            if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                continue
            kind, payload = event.get("payload_type", ""), event["payload"]
            if kind == "run.output.delta" and isinstance(payload.get("text"), str):
                self.buffer += payload["text"]
                *done, self.buffer = self.buffer.split("\n")
                for text in done:
                    self._say(text)
            elif kind == "task.lifecycle.proposed":
                task = str((payload.get("event") or {}).get("task_kind") or "")
                if task and not task.startswith(SKIP_TASKS):
                    self._flush()
                    self._say(f"→ {task}")

    def _flush(self) -> None:
        if self.buffer:
            self._say(self.buffer)
            self.buffer = ""

    def close(self) -> None:
        self._flush()
