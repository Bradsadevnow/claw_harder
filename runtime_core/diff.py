from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .replay import _load_events


# ---------------------------------------------------------------------------
# Per-cycle snapshot — one side of the comparison
# ---------------------------------------------------------------------------

@dataclass
class CycleSnapshot:
    cycle: int
    user_message: str | None = None
    llm_response: str | None = None
    turn_complete: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    governance_mode: str | None = None
    execution_mode: str | None = None


# ---------------------------------------------------------------------------
# Diff records
# ---------------------------------------------------------------------------

@dataclass
class FieldDelta:
    field: str
    value_a: Any
    value_b: Any


@dataclass
class CycleDiff:
    cycle: int
    deltas: list[FieldDelta] = field(default_factory=list)
    only_in_a: bool = False   # cycle exists in A but not B
    only_in_b: bool = False   # cycle exists in B but not A

    @property
    def diverged(self) -> bool:
        return bool(self.deltas) or self.only_in_a or self.only_in_b


@dataclass
class RunDiff:
    run_id_a: str
    run_id_b: str

    # Fork provenance — populated when B is a fork of A
    common_ancestor_run_id: str | None = None
    fork_cycle: int | None = None  # cycles <= fork_cycle are shared, skip in diff

    cycles: list[CycleDiff] = field(default_factory=list)

    # Summary counts
    cycles_compared: int = 0
    cycles_diverged: int = 0
    cycles_only_in_a: int = 0
    cycles_only_in_b: int = 0

    @property
    def identical(self) -> bool:
        return self.cycles_diverged == 0 and self.cycles_only_in_a == 0 and self.cycles_only_in_b == 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _group_by_cycle(events: list[dict]) -> dict[int, list[dict]]:
    groups: dict[int, list[dict]] = {}
    for event in events:
        c = event.get("cycle", 0)
        groups.setdefault(c, []).append(event)
    return groups


def _extract_run_id(events: list[dict]) -> str:
    for event in events:
        rid = event.get("run_id", "")
        if rid:
            return rid
    return ""


def _extract_fork_provenance(events: list[dict]) -> tuple[str | None, int | None]:
    """Return (parent_run_id, fork_cycle) if a fork.created event is present."""
    for event in events:
        if event.get("kind") == "fork.created":
            details = event.get("details", {})
            parent = details.get("parent_run_id")
            cycle  = details.get("parent_cycle")
            if parent:
                return parent, (int(cycle) if cycle is not None else None)
    return None, None


def _build_snapshot(cycle_events: list[dict]) -> CycleSnapshot:
    snap = CycleSnapshot(cycle=cycle_events[0].get("cycle", 0) if cycle_events else 0)
    for event in cycle_events:
        kind    = event.get("kind", "")
        details = event.get("details", {})
        if kind == "user_message":
            snap.user_message = details.get("content")
        elif kind == "llm_response":
            snap.llm_response = details.get("output_text")
            if snap.tool_calls is not None:
                snap.tool_calls = details.get("tool_calls", [])
        elif kind == "turn_complete":
            snap.turn_complete = details.get("content")
        elif kind == "policy_engine.mode":
            snap.governance_mode = details.get("mode")
        elif kind == "runtime.execution_mode":
            if "execution_mode" in details:
                snap.execution_mode = str(details.get("execution_mode", "")).strip().lower() or None
            else:
                enabled = details.get("tool_use_enabled")
                if enabled is not None:
                    snap.execution_mode = "execute" if bool(enabled) else "plan"
    return snap


def _diff_snapshots(snap_a: CycleSnapshot, snap_b: CycleSnapshot) -> list[FieldDelta]:
    deltas: list[FieldDelta] = []
    for field_name in ("user_message", "llm_response", "turn_complete", "governance_mode", "execution_mode"):
        va = getattr(snap_a, field_name)
        vb = getattr(snap_b, field_name)
        if va != vb:
            deltas.append(FieldDelta(field=field_name, value_a=va, value_b=vb))

    # Tool calls compared as sorted lists of (name, arguments) pairs for stability
    def _normalise_calls(calls: list[dict]) -> list[tuple[str, str]]:
        import json as _json
        return sorted((c.get("name", ""), _json.dumps(c.get("arguments", {}), sort_keys=True)) for c in calls)

    calls_a = _normalise_calls(snap_a.tool_calls)
    calls_b = _normalise_calls(snap_b.tool_calls)
    if calls_a != calls_b:
        deltas.append(FieldDelta(field="tool_calls", value_a=snap_a.tool_calls, value_b=snap_b.tool_calls))

    return deltas


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def diff_runs(log_a: Path, log_b: Path) -> RunDiff:
    """Compare two event logs cycle-by-cycle and return a structured diff.

    If log_b is a fork of log_a, the fork.created event is used to skip the
    shared prefix and focus the diff on the divergent portion only.
    """
    events_a = _load_events(log_a)
    events_b = _load_events(log_b)

    run_id_a = _extract_run_id(events_a)
    run_id_b = _extract_run_id(events_b)

    # Detect fork relationship — B might be a fork of A
    parent_run_id, fork_cycle = _extract_fork_provenance(events_b)
    if parent_run_id != run_id_a:
        # Also check if A is a fork of B
        parent_run_id_alt, fork_cycle_alt = _extract_fork_provenance(events_a)
        if parent_run_id_alt == run_id_b:
            parent_run_id, fork_cycle = parent_run_id_alt, fork_cycle_alt
        else:
            parent_run_id, fork_cycle = None, None

    result = RunDiff(
        run_id_a=run_id_a,
        run_id_b=run_id_b,
        common_ancestor_run_id=parent_run_id,
        fork_cycle=fork_cycle,
    )

    groups_a = _group_by_cycle(events_a)
    groups_b = _group_by_cycle(events_b)

    # When comparing a fork, skip cycles that were shared (cycle <= fork_cycle)
    skip_up_to = fork_cycle if fork_cycle is not None else -1

    all_cycles = sorted(set(groups_a) | set(groups_b))
    for cycle in all_cycles:
        if cycle <= skip_up_to:
            continue
        if cycle not in groups_a and cycle not in groups_b:
            continue

        in_a = cycle in groups_a
        in_b = cycle in groups_b

        if not in_a:
            cd = CycleDiff(cycle=cycle, only_in_b=True)
            result.cycles.append(cd)
            result.cycles_only_in_b += 1
            continue
        if not in_b:
            cd = CycleDiff(cycle=cycle, only_in_a=True)
            result.cycles.append(cd)
            result.cycles_only_in_a += 1
            continue

        snap_a = _build_snapshot(groups_a[cycle])
        snap_b = _build_snapshot(groups_b[cycle])
        deltas = _diff_snapshots(snap_a, snap_b)
        cd = CycleDiff(cycle=cycle, deltas=deltas)
        result.cycles.append(cd)
        result.cycles_compared += 1
        if cd.diverged:
            result.cycles_diverged += 1

    return result


def render_diff(result: RunDiff) -> str:
    """Render a RunDiff as a human-readable text report."""
    lines: list[str] = []

    lines.append(f"run A: {result.run_id_a or '(unknown)'}")
    lines.append(f"run B: {result.run_id_b or '(unknown)'}")

    if result.common_ancestor_run_id:
        lines.append(f"fork:  {result.common_ancestor_run_id} @ cycle {result.fork_cycle}")
        lines.append(f"       (cycles 0–{result.fork_cycle} shared, comparing from cycle {result.fork_cycle + 1})")
    lines.append("")

    if result.identical:
        lines.append("identical — no differences found")
        return "\n".join(lines)

    lines.append(
        f"summary: {result.cycles_diverged} diverged, "
        f"{result.cycles_only_in_a} only-in-A, "
        f"{result.cycles_only_in_b} only-in-B"
        f" (of {result.cycles_compared} compared)"
    )
    lines.append("")

    for cd in result.cycles:
        if not cd.diverged:
            continue
        lines.append(f"cycle {cd.cycle}:")
        if cd.only_in_a:
            lines.append("  [only in A]")
        elif cd.only_in_b:
            lines.append("  [only in B]")
        else:
            for delta in cd.deltas:
                lines.append(f"  {delta.field}:")
                lines.append(f"    A: {_truncate(delta.value_a)}")
                lines.append(f"    B: {_truncate(delta.value_b)}")
        lines.append("")

    return "\n".join(lines)


def _truncate(value: Any, limit: int = 120) -> str:
    s = repr(value) if not isinstance(value, str) else value
    if len(s) > limit:
        return s[:limit] + "…"
    return s
