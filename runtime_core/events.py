from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time
from typing import Any


@dataclass
class Event:
    # Identity — required, set by runtime._emit
    run_id: str
    cycle: int
    seq: int

    # Classification
    kind: str
    module: str
    level: str = "info"

    # Content
    msg: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    # Time (non-authoritative — for observability only, never used for ordering)
    timestamp: float = field(default_factory=time)

    # Causal linkage (seq of the event that triggered this one)
    parent_seq: int | None = None

    # Turn-scoped correlation ID — same value on every event within a single turn.
    # None for events emitted outside of a turn (init, idle heartbeats, etc.)
    correlation_id: str | None = None

    # Stable identity for deduplication and cross-run comparison
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class JSONLLogSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: Event) -> None:
        from .jsonutil import dumps_json
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(dumps_json(asdict(event)) + "\n")


class ConsoleSink:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def write(self, event: Event) -> None:
        if not self.enabled:
            return
        if (
            event.kind.startswith("loop.")
            or event.kind.startswith("tool.")
            or event.kind.startswith("policy_engine.")
            or event.kind.startswith("operator.")
        ):
            print(
                f"[{event.module}:{event.kind}] {event.msg or ''}".rstrip(),
                flush=True,
            )


def trim_event_log(path: Path, max_lines: int = 5000) -> int:
    """Trim the JSONL event log to the most recent max_lines entries.
    Returns the number of lines removed. No-ops if the file doesn't exist or is within bounds.
    """
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) <= max_lines:
        return 0
    trimmed = lines[-max_lines:]
    path.write_text("".join(trimmed), encoding="utf-8")
    return len(lines) - max_lines


class Router:
    def __init__(self, sinks: list[object]) -> None:
        self.sinks = sinks

    def emit(self, event: Event) -> None:
        for sink in self.sinks:
            sink.write(event)
