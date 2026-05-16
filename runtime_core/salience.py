"""
Phase 6A: Salience Arbitration / Persistence Admissibility

A signal does not persist because it exists.
A signal persists because it earns continuity.

Architecture:
    working memory
        ↓
    salience arbitration   ← SalienceArbitrator
        ↓
    proposal mutation      ← ProposalMutation
        ↓
    promotion membrane     ← PromotionMembrane
        ↓
    episodic / semantic / procedural persistence   OR   expiration
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Signal taxonomy — ascending weight, ascending intentionality
# ---------------------------------------------------------------------------

SIGNAL_WEIGHTS: dict[str, float] = {
    "observed":              0.10,
    "referenced":            0.25,
    "edited":                0.45,
    "terminal_referenced":   0.60,
    "repeatedly_revisited":  0.70,
    "structurally_central":  0.75,
    "operator_focused":      0.85,
    "git_staged_discussed":  0.95,
}

# ---------------------------------------------------------------------------
# Survival claim taxonomy — what a signal must prove to earn continuity
# ---------------------------------------------------------------------------

VALID_CLAIM_TYPES: frozenset[str] = frozenset({
    "task_relevance",        # materially affects current objective, blocker, plan, next action
    "user_declared",         # explicit operator preference, instruction, correction, boundary
    "runtime_declared",      # runtime identifies continuity significance
    "integrity_relevance",   # affects governance, replay, authority, execution legitimacy
    "recurrence",            # reinforces an existing pattern across events or turns
    "unresolved_obligation", # open loop, deferred task, failed execution, pending commitment
    "identity_candidacy",    # potential durable truth requiring arbitration before persistence
})

CLAIM_BASE_WEIGHTS: dict[str, float] = {
    "task_relevance":        0.60,
    "user_declared":         0.80,
    "runtime_declared":      0.50,
    "integrity_relevance":   0.90,
    "recurrence":            0.50,
    "unresolved_obligation": 0.70,
    "identity_candidacy":    0.40,
}

# ---------------------------------------------------------------------------
# Promotion thresholds by destination
# Semantic requires higher confidence than episodic.
# Procedural requires near-certainty — it governs runtime behavior.
# ---------------------------------------------------------------------------

PROMOTION_THRESHOLDS: dict[str, float] = {
    "episodic":    0.40,
    "semantic":    0.65,
    "procedural":  0.85,
}

VALID_DESTINATIONS: frozenset[str] = frozenset(PROMOTION_THRESHOLDS.keys())

# ---------------------------------------------------------------------------
# Critical constraints — enforced, not advisory
#
# These represent the core epistemological commitments of the system:
#   high emotion   ≠ high salience
#   high novelty   ≠ durable truth
#   runtime inference ≠ automatic memory
#   operator statements ≠ automatic semantic truth
#   repetition ≠ confirmation
# ---------------------------------------------------------------------------

CONSTRAINT_EMOTION_AS_SALIENCE    = "emotion_without_task_grounding"
CONSTRAINT_NOVELTY_AS_TRUTH       = "novelty_without_corroboration"
CONSTRAINT_INFERENCE_ALONE        = "runtime_inference_without_grounding"
CONSTRAINT_OPERATOR_STATED_ALONE  = "operator_stated_without_corroboration"
CONSTRAINT_REPETITION_AS_CONFIRM  = "repetition_without_confirmation"


# ---------------------------------------------------------------------------
# ProposalMutation — a candidate continuity claim, never a direct write
# ---------------------------------------------------------------------------

@dataclass
class ProposalMutation:
    source: str          # "runtime" | "operator"
    claim_type: str      # must be in VALID_CLAIM_TYPES
    content: str
    destination: str     # "episodic" | "semantic" | "procedural"
    signals: list[str]   # subset of SIGNAL_WEIGHTS keys
    reason: str
    cycle: int
    key: str | None = None  # for semantic/procedural (key-addressed writes)

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("ProposalMutation.content must not be empty.")
        if self.claim_type not in VALID_CLAIM_TYPES:
            raise ValueError(
                f"Unknown claim_type {self.claim_type!r}. "
                f"Valid: {sorted(VALID_CLAIM_TYPES)}"
            )
        if self.destination not in VALID_DESTINATIONS:
            raise ValueError(
                f"Unknown destination {self.destination!r}. "
                f"Valid: {sorted(VALID_DESTINATIONS)}"
            )
        unknown_signals = [s for s in self.signals if s not in SIGNAL_WEIGHTS]
        if unknown_signals:
            raise ValueError(
                f"Unknown signals {unknown_signals!r}. "
                f"Valid: {sorted(SIGNAL_WEIGHTS.keys())}"
            )


# ---------------------------------------------------------------------------
# ArbitrationDecision — the output of evaluating a single proposal
# ---------------------------------------------------------------------------

@dataclass
class ArbitrationDecision:
    proposal: ProposalMutation
    outcome: str                  # "approved" | "denied"
    reason: str
    effective_score: float
    constraint_violated: str | None = None


# ---------------------------------------------------------------------------
# SalienceArbitrator — admissibility evaluation
# ---------------------------------------------------------------------------

class SalienceArbitrator:
    """
    Evaluates ProposalMutation objects against survival claims and critical
    constraints. Returns ArbitrationDecision for each — approved or denied
    with a traceable reason.

    Does not write to memory. Does not know about the memory store.
    Pure admissibility evaluation.
    """

    def evaluate(self, proposals: list[ProposalMutation]) -> list[ArbitrationDecision]:
        return [self._evaluate_one(p) for p in proposals]

    def _evaluate_one(self, p: ProposalMutation) -> ArbitrationDecision:
        score = self._compute_score(p)

        constraint = self._check_constraints(p)
        if constraint is not None:
            return ArbitrationDecision(
                proposal=p,
                outcome="denied",
                reason=f"critical constraint violated: {constraint}",
                effective_score=score,
                constraint_violated=constraint,
            )

        threshold = PROMOTION_THRESHOLDS[p.destination]
        if score < threshold:
            return ArbitrationDecision(
                proposal=p,
                outcome="denied",
                reason=f"below_threshold({score:.3f}<{threshold})",
                effective_score=score,
            )

        return ArbitrationDecision(
            proposal=p,
            outcome="approved",
            reason=f"score {score:.3f} >= {p.destination} threshold {threshold}",
            effective_score=score,
        )

    def _compute_score(self, p: ProposalMutation) -> float:
        base = CLAIM_BASE_WEIGHTS[p.claim_type]
        if p.signals:
            best_signal = max(SIGNAL_WEIGHTS.get(s, 0.0) for s in p.signals)
            score = (base + best_signal) / 2.0
        else:
            # No signal evidence — modest penalty, claim alone is weaker
            score = base * 0.8
        return min(1.0, score)

    def _check_constraints(self, p: ProposalMutation) -> str | None:
        """
        Enforce the five critical constraints. Returns the violated constraint
        name or None if all pass.

        These constraints exist because the system must resist common failure
        modes where transient cognitive states get mistaken for durable truth.
        """
        signals = set(p.signals)
        non_trivial_signals = signals - {"observed"}

        # 1. high emotion ≠ high salience
        #    Emotional signal strength in runtime state is not a continuity claim.
        #    Claims without task or structural grounding that target semantic
        #    persistence are rejected even if scored above threshold.
        if p.destination == "semantic" and p.claim_type in {"recurrence", "identity_candidacy"}:
            if not non_trivial_signals and p.claim_type == "identity_candidacy":
                return CONSTRAINT_EMOTION_AS_SALIENCE

        # 2. high novelty ≠ durable truth
        #    A novel signal that hasn't been seen before is NOT more important —
        #    it's less proven. Deny semantic writes for non-operator claims whose
        #    only signal is passive observation.
        #    user_declared claims with only observed are handled by constraint 4.
        if (
            p.destination == "semantic"
            and signals == {"observed"}
            and len(p.signals) == 1
            and p.claim_type != "user_declared"
        ):
            return CONSTRAINT_NOVELTY_AS_TRUTH

        # 3. runtime inference ≠ automatic memory
        #    When the runtime declares something important, that declaration alone
        #    is insufficient for semantic persistence. Requires at least one
        #    non-trivial signal from the workspace.
        if (
            p.destination == "semantic"
            and p.claim_type == "runtime_declared"
            and not non_trivial_signals
        ):
            return CONSTRAINT_INFERENCE_ALONE

        # 4. operator statements ≠ automatic semantic truth
        #    An operator saying X does not make X semantically true. Requires at
        #    least one corroborating signal beyond passive observation.
        if (
            p.destination == "semantic"
            and p.claim_type == "user_declared"
            and not non_trivial_signals
        ):
            return CONSTRAINT_OPERATOR_STATED_ALONE

        # 5. repetition ≠ confirmation
        #    Repeated observation of a signal does not confirm its semantic truth.
        #    Recurrence claims targeting semantic persistence require at least one
        #    signal demonstrating active operator engagement (not passive view).
        if (
            p.destination == "semantic"
            and p.claim_type == "recurrence"
            and signals.issubset({"observed", "repeatedly_revisited"})
        ):
            return CONSTRAINT_REPETITION_AS_CONFIRM

        return None


# ---------------------------------------------------------------------------
# PromotionMembrane — executes approved promotions into durable stores
# ---------------------------------------------------------------------------

class PromotionMembrane:
    """
    The persistence boundary.

    Takes approved ArbitrationDecision objects and writes their proposals
    into the appropriate durable memory store. Denied decisions are silently
    expired — they do not cross the membrane.

    Returns only the decisions that resulted in actual writes.
    """

    def execute(
        self,
        decisions: list[ArbitrationDecision],
        memory: Any,
    ) -> list[ArbitrationDecision]:
        promoted: list[ArbitrationDecision] = []
        for decision in decisions:
            if decision.outcome != "approved":
                continue
            p = decision.proposal
            if p.destination == "semantic":
                key = p.key or f"promotion_c{p.cycle}_{abs(hash(p.content)) % 100000}"
                memory.semantic.write(key, p.content)
            elif p.destination == "episodic":
                memory.episodic.append("promoted", p.content)
            elif p.destination == "procedural":
                key = p.key or f"policy_c{p.cycle}"
                memory.procedural.write(key, p.content)
            promoted.append(decision)
        return promoted


# ---------------------------------------------------------------------------
# Convenience: build a ProposalMutation with validation
# ---------------------------------------------------------------------------

def make_proposal(
    *,
    source: str,
    claim_type: str,
    content: str,
    destination: str,
    signals: list[str],
    reason: str,
    cycle: int,
    key: str | None = None,
) -> ProposalMutation:
    return ProposalMutation(
        source=source,
        claim_type=claim_type,
        content=content,
        destination=destination,
        signals=signals,
        reason=reason,
        cycle=cycle,
        key=key,
    )
