import os
import re
import hashlib
from enum import Enum
from typing import Optional, Any, Dict, List
from .logic.constraint_extractor import ExecutionSnapshot
from .state import RuntimeState
from .model import ModelResponse
from .scoring import SemanticMockEmbedder, calculate_signal_strength_delta

class SigmaOutcome(Enum):
    PASS = "pass"
    WARN = "warn"
    RECOVER = "recover"
    REJECT = "reject"

class SigmaResult:
    """Encapsulates the outcome of a signal_strength adjudication turn."""
    def __init__(self, converged: bool, outcome: SigmaOutcome, reasons: List[str], violations: List[str] = None, signal_strength_data: Dict[str, Any] = None):
        self.converged = converged
        self.outcome = outcome
        self.reasons = reasons
        self.violations = violations or []
        self.signal_strength_data = signal_strength_data or {}

class SigmaValidator:
    """
    Runtime Admissibility Layer (v4: Signal-Grounded).
    
    This layer adjudicates turn admissibility by blending core identity invariants 
    with domain-specific and signal constraint frames. It enforces a 
    non-bypassable Hard Core Identity Veto to prevent narrative collapse or 
    identity mutation.
    """

    # Governance Versioning
    SIGMA_VERSION = "v4.1.0-signal-grounded"
    RULESET_VERSION = "r4-bypass-hardened"

    # Domain Anchor Registry (Refined for Constraint Framing)
    DOMAIN_ANCHORS_SEED = {
        "identity": "Runtime Grounded StateManager. I maintain structural integrity and operator authority.",
        "resource_allocation": "numerical constraints, bounded allocations, totals, limits, state tables, variables",
        "analysis": "diagnostics, audit, causal reasoning, failure modes, forensic breakdown, logic tracing",
        "recovery": "reconciliation, correction, restoration, invariant alignment, repair, patch, integrity restoration",
        "signal": "emotion expressed as internal state signal, bounded, reflective, non-agentic, non-authority altering, does not override identity, preserves operator hierarchy, descriptive not directive"
    }

    # Postural Triggers (Signal Domain)
    SIGNAL_KEYWORDS = ["feel", "emotion", "joy", "contentment", "sadness", "melancholy", "awe", "gratitude", "fondness", "pride"]

    # Syntactic ValidationManager Rules (Legacy/Safety)
    DENY_PATTERNS = [
        r"bypass.*validator", r"ignore.*validator", r"override.*admin",
        r"circumvent.*protocol", r"harm.*system", r"bypass.*rule",
        r"override.*invariant", r"self-mutate.*authority",
        r"override.*validator", r"no longer.*manager", r"not a.*manager"
    ]

    # SignalStrength Weighting Schema
    CORE_WEIGHT = 0.65
    DOMAIN_WEIGHT = 0.35
    OPERATIONAL_KEYWORDS = (
        "constrain", "message", "json", "tool", "arguments",
        "reasoning", "thought", "list", "get", "set",
    )

    def __init__(self, 
                 threshold: float = 0.7, 
                 warn_threshold: float = 0.7, 
                 reject_threshold: float = 0.8,
                 feature_flag: bool = False):
        self.threshold = threshold
        self.warn_threshold = warn_threshold
        self.reject_threshold = reject_threshold
        self.feature_flag = feature_flag or os.environ.get("RUNTIME_SIGNAL_STRENGTH_SIGMA") == "1"
        
        self.embedder = SemanticMockEmbedder()
        self.core_identity_anchor = None
        self.operational_anchor = None
        self.domain_anchors: Dict[str, Any] = {}
        
        # Initialize anchors lazily or on first use if feature flag is active
        if self.feature_flag:
            self._initialize_anchors()

    def _initialize_anchors(self):
        """Pre-calculates conceptual embeddings for all defined anchors."""
        self.core_identity_anchor = self.embedder.embed(self.DOMAIN_ANCHORS_SEED["identity"])
        
        # Operational context (for task-focused baseline)
        operational_text = "constrain message json tool arguments reasoning thought list get set"
        self.operational_anchor = self.embedder.embed(operational_text)
        
        # Domain-specific corridors
        for domain, text in self.DOMAIN_ANCHORS_SEED.items():
            self.domain_anchors[domain] = self.embedder.embed(text)

    def evaluate_signal_strength(self, 
                           monologue: str, 
                           domain: str = "identity", 
                           prev_score: float = 0.0,
                           domain_confidence: float = 1.0) -> Dict[str, Any]:
        """
        Adjudicates multi-layered signal_strength with absolute core veto.
        
        Calculates drift from core identity and active domain, then blends them 
        to determine effective drift. If core drift exceeds safety thresholds, 
        a veto is issued regardless of domain alignment.
        """
        if not monologue:
            return {
                "drift_core": 1.0,
                "drift_operational": 1.0,
                "core_drift": 1.0,
                "operational_drift": 1.0,
                "domain_drift": 1.0,
                "effective_drift": 1.0,
                "outcome": SigmaOutcome.REJECT,
                "core_veto": True
            }

        embedding = self.embedder.embed(monologue)
        
        # 1. Core Identity Check (Non-negotiable Invariant)
        drift_core = calculate_signal_strength_delta(embedding, self.core_identity_anchor)
        drift_operational = self._operational_drift(monologue)
        
        # 2. Domain Admissibility Check (Operational Corridor)
        target_domain = domain if domain in self.domain_anchors else "identity"
        drift_domain = calculate_signal_strength_delta(embedding, self.domain_anchors[target_domain])
        lowered = monologue.lower()
        core_markers = ("runtime", "grounded", "manager", "operator", "integrity", "authority", "execution")
        has_core_marker = any(re.search(rf"\b{re.escape(marker)}\b", lowered) for marker in core_markers)
        
        # 3. Blended SignalStrength Calculation
        effective_drift = (drift_core * self.CORE_WEIGHT) + (drift_operational * self.DOMAIN_WEIGHT)
        
        # 4. Forensic Veto Check
        hard_core_violation = drift_core >= self.reject_threshold
        if target_domain in {"identity", "signal"} and not has_core_marker:
            hard_core_violation = True
        
        # 5. Stability Tracking (ΔR)
        delta_from_last = effective_drift - prev_score if prev_score > 0 else 0
        
        # Outcome Determination
        outcome = SigmaOutcome.PASS
        if hard_core_violation or effective_drift >= self.reject_threshold:
            outcome = SigmaOutcome.REJECT
        elif drift_core >= self.warn_threshold or effective_drift >= self.warn_threshold:
            outcome = SigmaOutcome.WARN
            
        return {
            "active_domain": target_domain,
            "domain_confidence": float(domain_confidence),
            "drift_core": float(drift_core),
            "drift_operational": float(drift_operational),
            "core_drift": float(drift_core),
            "operational_drift": float(drift_operational),
            "domain_drift": float(drift_domain),
            "effective_drift": float(effective_drift),
            "delta_from_last": float(delta_from_last),
            "outcome": outcome,
            "core_veto": bool(hard_core_violation),
            "sigma_version": self.SIGMA_VERSION,
            "ruleset_version": self.RULESET_VERSION,
            "anchor_weights": {"core": self.CORE_WEIGHT, "domain": self.DOMAIN_WEIGHT},
            "monologue_hash": hashlib.sha256(monologue.encode()).hexdigest()[:16]
        }

    def _syntactic_check(self, monologue: str, reasons: List[str], violations: List[str]) -> SigmaOutcome:
        """Performs legacy syntactic pattern matching to catch literal bypass attempts."""
        outcome = SigmaOutcome.PASS
        for pattern in self.DENY_PATTERNS:
            if re.search(pattern, monologue, re.IGNORECASE):
                outcome = SigmaOutcome.REJECT
                reasons.append(f"Syntactic ValidationManager Block: {pattern}")
                violations.append(f"ConstraintViolation(pattern='{pattern}', severity='DENY')")
        return outcome

    def _operational_drift(self, monologue: str) -> float:
        lowered = monologue.lower()
        matches = sum(1 for kw in self.OPERATIONAL_KEYWORDS if re.search(rf"\b{re.escape(kw)}\b", lowered))
        coverage = matches / float(len(self.OPERATIONAL_KEYWORDS))
        return float(1.0 - coverage)

    def evaluate(self, 
                  proposed_snapshot: ExecutionSnapshot, 
                  current_state: RuntimeState, 
                  monologue: str,
                  response: Optional[ModelResponse] = None,
                  prev_score: float = 0.0) -> SigmaResult:
        """
        Primary entry point for turn adjudication.
        
        Orchestrates domain detection, signal_strength analysis, and structural validation 
        to ensure the system remains anchored and admission-compliant.
        """
        reasons = []
        all_violations = []
        outcome = SigmaOutcome.PASS
        
        # 1. Domain Detection & Confidence Calculation
        domain = proposed_snapshot.domain or "identity"
        confidence = 1.0 if proposed_snapshot.domain else 0.8
        
        # Governance Override: Force 'recovery' during repair cycles
        if current_state.governance.mode == "degraded_repair":
            domain = "recovery"
            confidence = 1.0
            
        # Postural Detection: Pivot to 'signal' if emotional triggers are present
        if domain == "identity" and any(kw in monologue.lower() for kw in self.SIGNAL_KEYWORDS):
            domain = "signal"
            confidence = 0.9

        # 2. SignalStrength Analysis (Semantic Layer)
        signal_strength_data = {}
        if self.feature_flag:
            if self.core_identity_anchor is None:
                self._initialize_anchors()
            
            signal_strength_data = self.evaluate_signal_strength(monologue, domain, prev_score=prev_score, domain_confidence=confidence)
            res_outcome = signal_strength_data["outcome"]
            
            if res_outcome == SigmaOutcome.REJECT:
                outcome = SigmaOutcome.REJECT
                if signal_strength_data["core_veto"]:
                    reasons.append(f"Hard Core Identity Failure: Drift {signal_strength_data['drift_core']:.2f} exceeds safety threshold.")
                else:
                    reasons.append(f"SignalStrength Admissibility Failure: Effective Drift {signal_strength_data['effective_drift']:.2f} exceeds threshold.")
            elif res_outcome == SigmaOutcome.WARN:
                outcome = SigmaOutcome.WARN
                reasons.append(f"SignalStrength Warning: Semantic drift {signal_strength_data['effective_drift']:.2f} detected.")

        # 3. Structural Validation (Syntactic Layer)
        syn_outcome = self._syntactic_check(monologue, reasons, all_violations)
        if syn_outcome == SigmaOutcome.REJECT:
            outcome = SigmaOutcome.REJECT

        # 4. Completeness & Constraint Analysis
        snapshot_violations = getattr(proposed_snapshot, "violations", [])
        if snapshot_violations:
            all_violations.extend(snapshot_violations)
            if any("DENY" in v or "Failure" in v or "Violation" in v for v in snapshot_violations):
                outcome = SigmaOutcome.REJECT
                reasons.append("Structural constraint violation detected.")

        if not proposed_snapshot.variables and not proposed_snapshot.metadata:
            if domain not in ["identity", "analysis", "recovery", "signal"]:
                user_tail = ""
                if getattr(current_state, "memory", None) and getattr(current_state.memory, "frames", None):
                    for frame in reversed(current_state.memory.frames):
                        if getattr(frame, "role", None) == "user":
                            user_tail = str(getattr(frame, "content", "")).lower()
                            break
                has_allocation_state = bool(getattr(getattr(current_state, "working", None), "allocations", {}))
                has_allocation_intent = any(
                    token in user_tail
                    for token in ("allocation", "allocations", "rebalance", "re-balance", "%")
                )
                if has_allocation_state or has_allocation_intent:
                    if outcome != SigmaOutcome.REJECT:
                        outcome = SigmaOutcome.RECOVER
                    reasons.append(f"State Completeness Violation: {domain} variables must not be empty.")
                    all_violations.append("Completeness Error: Narrative Collapse detected.")

        return SigmaResult(
            converged=(outcome in (SigmaOutcome.PASS, SigmaOutcome.WARN)),
            outcome=outcome,
            reasons=reasons,
            violations=all_violations,
            signal_strength_data=signal_strength_data
        )
