"""
Phase 6A: Workspace signal ingestion layer.

Converts raw VSCode workspace events into ProposalMutation candidates
staged in working memory for salience arbitration at turn commit.

Architecture:
    VSCode extension → /api/workspace/signal → WorkspaceIngestionLayer
        → working memory (transient context, always)
        → ProposalMutation (persistence candidate, for qualifying signals only)
        → memory.propose()
        → _commit_cycle() → SalienceArbitrator → PromotionMembrane
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any

from .salience import (
    SIGNAL_WEIGHTS,
    CLAIM_BASE_WEIGHTS,
    PROMOTION_THRESHOLDS,
    VALID_CLAIM_TYPES,
    make_proposal,
    ProposalMutation,
)


# ---------------------------------------------------------------------------
# Workspace event kinds → signal taxonomy mapping
# ---------------------------------------------------------------------------

WORKSPACE_SIGNAL_MAP: dict[str, str] = {
    "file_opened":               "observed",
    "file_referenced":           "referenced",
    "symbol_referenced":         "referenced",
    "file_edited":               "edited",
    "file_saved":                "edited",
    "terminal_command":          "terminal_referenced",
    "terminal_referenced":       "terminal_referenced",
    "file_revisited":            "repeatedly_revisited",
    "structurally_central":      "structurally_central",
    "operator_focus":            "operator_focused",
    "operator_focused":          "operator_focused",
    "git_staged_discussed":      "git_staged_discussed",
}

# Signal → (claim_type, preferred_destination) for proposal generation.
# Weak signals (observed) don't generate proposals — they're transient context only.
SIGNAL_CLAIM_MAP: dict[str, tuple[str, str]] = {
    "referenced":            ("task_relevance",        "episodic"),
    "edited":                ("task_relevance",        "episodic"),
    "terminal_referenced":   ("task_relevance",        "episodic"),
    "repeatedly_revisited":  ("recurrence",            "episodic"),
    "structurally_central":  ("task_relevance",        "semantic"),
    "operator_focused":      ("user_declared",         "semantic"),
    "git_staged_discussed":  ("unresolved_obligation", "semantic"),
}

# Minimum signal weight required to generate a proposal.
# Signals below this (i.e. "observed") go to working memory only — no proposal.
MIN_PROPOSAL_SIGNAL_WEIGHT = SIGNAL_WEIGHTS["referenced"]  # 0.25

# How many times an entity must be seen before recurrence signal is added.
RECURRENCE_THRESHOLD = 3

# Per-cycle exponential decay applied to entity counts when they have been idle.
# At 0.85/cycle: count=3 survives ~5 idle cycles; count=10 survives ~14 idle cycles.
RECURRENCE_DECAY_RATE = 0.85


# ---------------------------------------------------------------------------
# WorkspaceEvent
# ---------------------------------------------------------------------------

@dataclass
class WorkspaceEvent:
    kind: str
    path: str | None = None       # file path, if relevant
    symbol: str | None = None     # symbol/identifier, if relevant
    command: str | None = None    # terminal command text, if relevant
    content: str | None = None    # snippet or description for semantic content
    ts: float = field(default_factory=time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def entity(self) -> str:
        """The primary addressable entity for recurrence tracking."""
        return self.path or self.symbol or self.command or ""

    def describe(self) -> str:
        """Human-readable content for proposal.content."""
        if self.content:
            return self.content
        parts = []
        if self.path:
            parts.append(self.path)
        if self.symbol:
            parts.append(f"symbol:{self.symbol}")
        if self.command:
            parts.append(f"$ {self.command[:120]}")
        return " | ".join(parts) if parts else f"workspace event: {self.kind}"


# ---------------------------------------------------------------------------
# WorkspaceIngestionLayer
# ---------------------------------------------------------------------------

class WorkspaceIngestionLayer:
    """
    Stateful ingestion layer. Tracks entity recurrence across the session
    (cross-turn — recurrence is about operator attention patterns over time,
    not just within a single turn).

    Each call to ingest():
    1. Writes transient context to working memory (always)
    2. Generates a ProposalMutation for qualifying signals (signal weight >= threshold)
    3. Adds recurrence signal if entity has been seen >= RECURRENCE_THRESHOLD times
    4. Calls memory.propose() to stage the proposal for arbitration
    """

    def __init__(self) -> None:
        self._entity_counts: dict[str, float] = {}
        self._entity_last_cycle: dict[str, int] = {}

    def ingest(
        self,
        event: WorkspaceEvent,
        memory: Any,  # MemoryStore
        cycle: int,
    ) -> dict[str, Any]:
        """
        Process a workspace event. Returns a summary of what was done.
        """
        signal = WORKSPACE_SIGNAL_MAP.get(event.kind)
        if signal is None:
            return {"ok": False, "reason": f"unknown event kind: {event.kind!r}"}

        # Track entity recurrence with exponential decay.
        # Counts decay per idle cycle so entities that go cold lose their
        # recurrence signal without operator action.
        entity = event.entity()
        if entity:
            last_cycle = self._entity_last_cycle.get(entity, cycle)
            cycles_idle = max(0, cycle - last_cycle)
            prior = self._entity_counts.get(entity, 0.0) * (RECURRENCE_DECAY_RATE ** cycles_idle)
            self._entity_counts[entity] = prior + 1.0
            self._entity_last_cycle[entity] = cycle

        # Always write to working memory — it's the transient cognition surface
        working_key = f"ws:{event.kind}:{entity or id(event)}"
        memory.working.set(working_key, {
            "kind": event.kind,
            "path": event.path,
            "symbol": event.symbol,
            "signal": signal,
            "entity": entity,
            "ts": event.ts,
        })

        # Signals below the proposal threshold are transient context only
        if SIGNAL_WEIGHTS.get(signal, 0.0) < MIN_PROPOSAL_SIGNAL_WEIGHT:
            return {"ok": True, "signal": signal, "proposed": False, "reason": "below_proposal_threshold"}

        # Build effective signal list (add recurrence if threshold met)
        effective_signals = [signal]
        count = self._entity_counts.get(entity, 0)
        if entity and count >= RECURRENCE_THRESHOLD and "repeatedly_revisited" not in effective_signals:
            effective_signals.append("repeatedly_revisited")

        proposal = self._build_proposal(event, signal, effective_signals, cycle)
        if proposal is None:
            return {"ok": True, "signal": signal, "proposed": False, "reason": "no_qualifying_claim"}

        memory.propose(proposal)
        return {
            "ok": True,
            "signal": signal,
            "proposed": True,
            "claim_type": proposal.claim_type,
            "destination": proposal.destination,
            "signals": effective_signals,
            "recurrence_count": count,
        }

    def _build_proposal(
        self,
        event: WorkspaceEvent,
        signal: str,
        effective_signals: list[str],
        cycle: int,
    ) -> ProposalMutation | None:
        if signal not in SIGNAL_CLAIM_MAP:
            return None

        claim_type, preferred_destination = SIGNAL_CLAIM_MAP[signal]

        # Compute expected score to select best qualifying destination
        destination = self._select_destination(claim_type, effective_signals, preferred_destination)
        if destination is None:
            return None

        return make_proposal(
            source="runtime",
            claim_type=claim_type,
            content=event.describe(),
            destination=destination,
            signals=effective_signals,
            reason=f"workspace:{event.kind}",
            cycle=cycle,
            key=self._derive_key(event, destination),
        )

    def _select_destination(
        self,
        claim_type: str,
        signals: list[str],
        preferred: str,
    ) -> str | None:
        base = CLAIM_BASE_WEIGHTS.get(claim_type, 0.0)
        best_signal = max((SIGNAL_WEIGHTS.get(s, 0.0) for s in signals), default=0.0)
        score = (base + best_signal) / 2.0 if signals else base * 0.8

        # Try preferred destination first, then fall back down
        candidates = [preferred]
        if preferred == "semantic":
            candidates += ["episodic"]

        for dest in candidates:
            if score >= PROMOTION_THRESHOLDS[dest]:
                return dest

        return None

    def _derive_key(self, event: WorkspaceEvent, destination: str) -> str | None:
        if destination not in ("semantic", "procedural"):
            return None
        entity = event.entity()
        if entity:
            safe = entity.replace("/", "_").replace(".", "_").replace(" ", "_")
            return f"ws_{event.kind}_{safe}"[:80]
        return None

    def reset_recurrence(self) -> None:
        """Clear recurrence tracking. Call when runtime session restarts."""
        self._entity_counts.clear()
        self._entity_last_cycle.clear()
