"""
Phase 7: Event taxonomy registry.

Declares all event kinds emitted by phases 3–7 (memory substrate, persistence,
control plane, salience arbitration, workspace ingestion, observability).

Purpose:
    - Self-documentation: what events exist and what their details carry
    - Test-time validation: tests can assert that observed events are registered
      and carry the expected detail keys
    - No runtime enforcement: this file is never imported on the hot path

Pre-existing runtime events (sigma.*, tool.repair.*, policy_engine.text_claim,
execution.trace, etc.) are not registered here — they predate Phase 7 and their
taxonomy is owned by the deeper runtime systems. This registry covers the
cognitive/observability layer we built.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EventSpec:
    description: str
    level: str = "info"
    detail_keys: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

EVENT_KINDS: dict[str, EventSpec] = {
    # --- Runtime lifecycle ---
    "runtime.start": EventSpec(
        "Runtime initialized.",
        detail_keys=("mcp_tools",),
    ),
    "runtime.memory_reset": EventSpec(
        "Memory reset by operator.",
        detail_keys=("sandbox_result", "pre_reset_snapshot"),
    ),
    "runtime.reinitialized": EventSpec(
        "Runtime reinitialized after control-plane reset.",
        detail_keys=("killswitch_engaged",),
    ),
    "runtime.snapshot_failed": EventSpec(
        "Pre-reset snapshot failed.",
        level="warning",
    ),

    # --- Turn lifecycle ---
    "loop.pulse": EventSpec(
        "Turn started.",
        detail_keys=("cycle", "mode"),
    ),
    "loop.end": EventSpec(
        "Turn committed.",
    ),
    "user_message": EventSpec(
        "User input received.",
        detail_keys=("content",),
    ),
    "turn_complete": EventSpec(
        "Turn finalized.",
        detail_keys=("content",),
    ),

    # --- Working memory lifecycle ---
    "working_memory.cleared": EventSpec(
        "Working memory cleared at turn boundary.",
        detail_keys=("cycle", "expired_items", "cleared_proposals"),
    ),
    "working_memory.committed": EventSpec(
        "Working memory promotion evaluated at turn commit.",
        detail_keys=("cycle", "working_items", "proposals_evaluated", "promoted", "expired"),
    ),

    # --- Salience arbitration ---
    "salience.decision": EventSpec(
        "Salience arbitration decision for a single proposal.",
        detail_keys=("outcome", "claim_type", "destination", "effective_score", "signals", "reason"),
    ),

    # --- Workspace ingestion ---
    "workspace.signal": EventSpec(
        "Workspace signal ingested from VSCode extension.",
        detail_keys=("kind", "signal", "proposed"),
    ),

    # --- Control plane ---
    "policy_engine.decision": EventSpec(
        "Control action denied by policy.",
        level="warning",
        detail_keys=("action", "decision", "reason"),
    ),
    "control_plane.action": EventSpec(
        "Control plane action applied.",
        detail_keys=("action", "source", "requested", "applied"),
    ),
    "killswitch.engaged": EventSpec(
        "Runtime halted via killswitch.",
        level="warning",
        detail_keys=("reason",),
    ),
    "killswitch.released": EventSpec(
        "Killswitch released; runtime resumed.",
        detail_keys=("reason",),
    ),

    # --- Governance (emitted when governance mode changes) ---
    "governance.mode_change": EventSpec(
        "Governance mode changed.",
        detail_keys=("previous", "current"),
    ),
}


# ---------------------------------------------------------------------------
# Validation helpers (for test use only — never called at runtime)
# ---------------------------------------------------------------------------

def registered_kinds() -> frozenset[str]:
    return frozenset(EVENT_KINDS.keys())


def validate_event(kind: str, details: dict) -> list[str]:
    """Return a list of missing required detail keys for the given event kind.
    Returns empty list if the kind is unregistered (no claim made about it).
    """
    spec = EVENT_KINDS.get(kind)
    if spec is None:
        return []
    return [k for k in spec.detail_keys if k not in details]
