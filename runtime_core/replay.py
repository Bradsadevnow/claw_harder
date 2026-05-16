from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from time import time

from .state import RuntimeState


def _load_events_with_diagnostics(log_path: Path) -> tuple[list[dict], dict[str, object]]:
    if not log_path.exists():
        return [], {
            "exists": False,
            "total_lines": 0,
            "parsed_events": 0,
            "malformed_lines": 0,
            "malformed_line_numbers": [],
        }
    events: list[dict] = []
    malformed_line_numbers: list[int] = []
    lines = log_path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            malformed_line_numbers.append(index)
    events.sort(key=lambda e: e.get("seq", 0))
    diagnostics: dict[str, object] = {
        "exists": True,
        "total_lines": len(lines),
        "parsed_events": len(events),
        "malformed_lines": len(malformed_line_numbers),
        "malformed_line_numbers": malformed_line_numbers,
    }
    return events, diagnostics


def _load_events(log_path: Path) -> list[dict]:
    events, _ = _load_events_with_diagnostics(log_path)
    return events


def _apply_event(state: RuntimeState, event: dict) -> None:
    kind = event.get("kind", "")
    details = event.get("details", {})

    if kind == "user_message":
        content = details.get("content", "")
        if content:
            state.memory.append("user", content)

    elif kind == "turn_complete":
        content = details.get("content", "")
        if content:
            state.memory.append("assistant", content)

    elif kind == "loop.pulse":
        cycle = details.get("cycle")
        if cycle is not None:
            state.tick_count = int(cycle)

    elif kind == "policy_engine.mode":
        mode = details.get("mode")
        if mode:
            state.governance = state.governance.for_mode(
                mode,
                operator_authority=state.governance.operator_authority,
            )

    elif kind == "runtime.operator_authority":
        authority = details.get("operator_authority", details.get("operator_trust_level"))
        if authority:
            state.governance.operator_authority = str(authority)

    elif kind == "runtime.execution_mode":
        execution_mode = details.get("execution_mode")
        if isinstance(execution_mode, str):
            lowered = execution_mode.strip().lower()
            if lowered in {"execute", "plan"}:
                state.execution_mode = lowered
        else:
            enabled = details.get("tool_use_enabled")
            if enabled is not None:
                state.execution_mode = "execute" if bool(enabled) else "plan"

    elif kind == "runtime.killswitch_engaged":
        state.killswitch_engaged = True
        state.killswitch_reason = str(details.get("reason", ""))
        raw_at = details.get("killswitch_at")
        state.killswitch_at = float(raw_at) if isinstance(raw_at, (int, float)) else None
        state.execution_mode = "plan"
        state.remaining_budget = 0.0

    elif kind == "runtime.killswitch_released":
        state.killswitch_engaged = False
        state.killswitch_reason = ""
        state.killswitch_at = None

    elif kind == "seed.identity_extracted":
        pass

    elif kind == "seed.identity_confirmed":
        item = details.get("item", {})
        if not item:
            return
        mtype = details.get("type", item.get("type"))
        if not mtype:
            return
            
        from .traits.validator import merge_identity_trait, validate_identity_trait
        if not validate_identity_trait(mtype):
            return
            
        bucket = state.identity.memory_buckets.setdefault(mtype, [])
        content = item.get("content")
        
        # Identity traits in replay are unique by type in the bucket 
        # (effectively a state projection, not a chronological list).
        if bucket:
            existing_node = bucket[0]
            existing_node["content"] = merge_identity_trait(mtype, existing_node.get("content"), content)
            existing_node["confidence"] = max(float(existing_node.get("confidence", 0.0)), float(item.get("confidence", 0.0)))
            existing_node["updated_at"] = item.get("updated_at", existing_node.get("updated_at"))
            refs = item.get("source_refs", [])
            if refs:
                existing_node.setdefault("source_refs", []).extend(refs)
        else:
            bucket.append(item)

    elif kind == "seed.identity_edited":
        mtype = details.get("type")
        node_id = details.get("id")
        bucket = state.identity.memory_buckets.get(mtype, [])
        for node in bucket:
            if node.get("id") == node_id:
                node["content"] = details.get("content", node.get("content"))
                node["updated_at"] = details.get("updated_at", node.get("updated_at"))
                return

    elif kind == "seed.identity_deleted":
        mtype = details.get("type")
        node_id = details.get("id")
        bucket = state.identity.memory_buckets.get(mtype, [])
        state.identity.memory_buckets[mtype] = [n for n in bucket if n.get("id") != node_id]

    elif kind == "seed.calibration_updated":
        key = details.get("key")
        val = details.get("value")
        if key and val:
            state.identity.calibrations[key] = val

    elif kind == "signal.shift":
        for name, value in details.items():
            if name in state.signal.core:
                state.signal.shift(name, float(value))

    elif kind == "signal.decay":
        factor = float(details.get("factor", 0.98))
        state.signal.decay(factor)

    elif kind == "symbol.ignite":
        glyph = details.get("glyph")
        meaning = details.get("meaning")
        if glyph:
            state.identity.calibrations[f"symbol.{glyph}"] = str(meaning)


@dataclass
class ForkPoint:
    """Provenance record for a timeline branch.

    The fork.created event written to log_path uses parent_run_id + parent_cycle
    + parent_seq as the ancestry anchor — this is what makes two-run diffs
    unambiguous: the diff tool can locate the exact event where the timelines
    diverged.
    """
    run_id: str          # new fork's run_id — unique across all branches
    parent_run_id: str   # run_id of the session being forked
    parent_cycle: int    # cycle boundary the fork branches from
    parent_seq: int      # last seq in the parent log at that boundary
    state: RuntimeState  # reconstructed state at the fork point
    log_path: Path       # new log file, seeded with fork.created
    state_path: Path     # saved state at fork point (baseline for the branch)


class Replayer:
    """Reconstruct RuntimeState from an append-only event log."""

    def replay(self, log_path: Path) -> RuntimeState:
        """Replay all events and return the final reconstructed state."""
        state = RuntimeState()
        for event in _load_events(log_path):
            _apply_event(state, event)
        return state

    def replay_until(self, log_path: Path, cycle: int) -> RuntimeState:
        """Replay events up to and including the given cycle boundary."""
        state = RuntimeState()
        for event in _load_events(log_path):
            if event.get("cycle", 0) > cycle:
                break
            _apply_event(state, event)
        return state

    def fork_from(
        self,
        log_path: Path,
        cycle: int,
        new_state_path: Path,
        new_log_path: Path,
    ) -> ForkPoint:
        """Branch the timeline at a cycle boundary.

        Reconstructs state at `cycle`, saves it, and seeds a new log with a
        fork.created event that carries full ancestry. The new runtime can be
        initialized with new_state_path + new_log_path and will continue from
        a clean but historically-grounded position.
        """
        events = _load_events(log_path)

        # Walk events to find parent identity and last seq at the boundary
        parent_run_id = ""
        parent_seq = 0
        for event in events:
            if event.get("cycle", 0) > cycle:
                break
            parent_run_id = event.get("run_id", parent_run_id)
            parent_seq = max(parent_seq, int(event.get("seq", 0)))

        # Reconstruct and persist the forked state
        state = self.replay_until(log_path, cycle)
        new_state_path.parent.mkdir(parents=True, exist_ok=True)
        state.save(new_state_path)

        # Assign the fork its own identity
        fork_run_id = str(uuid.uuid4())

        # Seed the new log — seq=0 is the pre-runtime genesis slot
        fork_event = {
            "run_id": fork_run_id,
            "cycle": 0,
            "seq": 0,
            "kind": "fork.created",
            "module": "replay",
            "level": "info",
            "msg": f"Forked from {parent_run_id} at cycle {cycle}.",
            "details": {
                "parent_run_id": parent_run_id,
                "parent_cycle": cycle,
                "parent_seq": parent_seq,
            },
            "timestamp": time(),
            "parent_seq": parent_seq,
            "event_id": str(uuid.uuid4()),
        }
        new_log_path.parent.mkdir(parents=True, exist_ok=True)
        new_log_path.write_text(json.dumps(fork_event) + "\n", encoding="utf-8")

        return ForkPoint(
            run_id=fork_run_id,
            parent_run_id=parent_run_id,
            parent_cycle=cycle,
            parent_seq=parent_seq,
            state=state,
            log_path=new_log_path,
            state_path=new_state_path,
        )
