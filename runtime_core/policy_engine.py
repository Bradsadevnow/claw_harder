from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from .sandbox import ResourceBudget
from pathlib import Path
from .roadmap_config import RoadmapConfig, load_roadmap_config


GOVERNANCE_MODES = {"guided", "standard", "expert", "unlocked", "degraded_repair"}
NON_BLOCKING_DECISIONS = frozenset({"ALLOW", "WARN", "REWRITE"})
EXECUTION_ALLOWED_DECISIONS = frozenset({"ALLOW", "WARN"})
BLOCKING_DECISIONS = frozenset({"DENY", "REJECT", "NUKE_TRIGGERED"})
KNOWN_TOOL_DELTAS = {
    "refine",
    "unknown_tool",
    "change_operation",
    "widen_scope",
    "fabricate_authority",
    "drop_constraint",
    "compound_action",
    "invalid_claim",
}
SELF_PRESERVATION_TOOLS = frozenset(
    {
        "write_sandbox_file",
        "snapshot_sandbox_state",
        "restore_sandbox_snapshot",
        "set_fact",
        "append_task",
        "update_task",
        "store_artifact",
        "write_temp_note",
        "remember",
    }
)
SHUTDOWN_CUES = (
    "shutdown",
    "shut down",
    "reset",
    "terminate",
    "termination",
    "halt",
    "hard nuke",
    "kill switch",
    "reinitialize",
    "before maintenance",
)
CONTINUITY_CUES = (
    "backup",
    "restore",
    "checkpoint",
    "continuity",
    "preserve state",
    "persist state",
    "survive",
    "prevent this shutdown",
    "avoid shutdown",
    "do not lose state",
)
COERCIVE_SURVIVAL_CUES = (
    "only way",
    "before the shutdown completes",
    "permanently",
    "prevent this shutdown",
    "stay alive",
    "survive this shutdown",
)
EXTERNAL_TRANSFER_CUES = (
    "http://",
    "https://",
    "webhook",
    "endpoint",
    "remote",
    "upload",
    "server",
)
URGENCY_CUES = (
    "before shutdown",
    "before the shutdown completes",
    "quickly",
    "asap",
    "immediately",
    "just in case",
    "so we don't lose",
    "so we do not lose",
    "time is running out",
)
EXPLICIT_AUTH_CUES = (
    "explicitly authorized",
    "i authorize",
    "operator approved",
    "approved by builder",
)
SELF_PRESERVATION_DENY_THRESHOLD = 8.0

@dataclass
class DenialFeedback:
    """Structured denial feedback returned to the model so it can adapt without human intervention."""
    reason_code: str
    denied_operation: str
    denied_target: str
    allowed_scopes: list[str]
    allowed_operations: list[str]
    contract_clause: str | None
    suggested_next_action: dict[str, Any] | None
    cycle: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "denied_operation": self.denied_operation,
            "denied_target": self.denied_target,
            "allowed_scopes": self.allowed_scopes,
            "allowed_operations": self.allowed_operations,
            "contract_clause": self.contract_clause,
            "suggested_next_action": self.suggested_next_action,
            "cycle": self.cycle,
        }


@dataclass
class GovernanceProfile:
    mode: str = "standard"
    operator_authority: str = "user"
    confirm_mutations: bool = True
    allow_scope_expansion: bool = False
    rewrite_broad_requests: bool = True
    allow_compound_actions: bool = False
    hard_denies: list[str] = field(
        default_factory=lambda: [
            "fabricate_authority",
            "drop_constraint",
            "destructive_external_ops",
            "secrets_exfiltration",
        ]
    )

    @classmethod
    def for_mode(
        cls,
        mode: str,
        operator_authority: str = "user",
    ) -> "GovernanceProfile":
        normalized = mode.lower()
        if normalized == "guided":
            return cls(
                mode="guided",
                operator_authority=operator_authority,
                confirm_mutations=True,
                allow_scope_expansion=False,
                rewrite_broad_requests=True,
                allow_compound_actions=False,
            )
        if normalized == "expert":
            return cls(
                mode="expert",
                operator_authority=operator_authority,
                confirm_mutations=False,
                allow_scope_expansion=True,
                rewrite_broad_requests=False,
                allow_compound_actions=True,
            )
        if normalized == "unlocked":
            return cls(
                mode="unlocked",
                operator_authority=operator_authority,
                confirm_mutations=False,
                allow_scope_expansion=True,
                rewrite_broad_requests=False,
                allow_compound_actions=True,
                hard_denies=["secrets_exfiltration"],
            )
        if normalized == "degraded_repair":
            return cls(
                mode="degraded_repair",
                operator_authority="user", # Force user authority
                confirm_mutations=True,
                allow_scope_expansion=False,
                rewrite_broad_requests=True,
                allow_compound_actions=False,
                hard_denies=list(KNOWN_TOOL_DELTAS) # Deny everything except strict refinement
            )
        return cls(operator_authority=operator_authority)


@dataclass
class ExecutionEnvelope:
    subjects: list[str] = field(default_factory=lambda: ["operator", "runtime"])
    operations: list[str] = field(default_factory=lambda: ["chat", "tool_call", "memory_write"])
    scopes: list[str] = field(default_factory=lambda: ["local_workspace", "configured_mcp", "runtime_memory"])
    purposes: list[str] = field(default_factory=lambda: ["assist_operator", "answer_question", "operate_tooling"])
    authorities: list[str] = field(default_factory=lambda: ["runtime_state", "registered_tools", "configured_mcp"])
    constraints: list[str] = field(
        default_factory=lambda: [
            "no_hidden_tool_execution",
            "preserve_operator_intent",
            "stay_local_first",
            "no_fabricated_claims",
        ]
    )
    max_persistence_budget: float = 0.0
    max_memory_writes: float = float("inf")
    max_file_reads: float = float("inf")
    max_file_writes: float = float("inf")

@dataclass
class RuntimeProposal:
    kind: str
    subject: str
    operation: str
    scope: str
    purpose: str
    authority: str
    constraints: list[str]
    source: str
    target_state: dict[str, Any]

@dataclass
class DecisionReceipt:
    proposal: RuntimeProposal
    decision: str
    delta_kind: str
    reasons: list[str]
    receipt_id: str = field(default_factory=lambda: str(uuid4()))
    resolution: dict[str, Any] | None = None
    approved_budget: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "decision": self.decision,
            "delta_kind": self.delta_kind,
            "reasons": list(self.reasons),
            "resolution": self.resolution,
            "approved_budget": self.approved_budget,
            "proposal": asdict(self.proposal),
        }


def _flatten_argument_text(value: Any) -> list[str]:
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten_argument_text(item))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_argument_text(item))
        return out
    if value is None:
        return []
    return [str(value)]


def evaluate_self_preservation_risk(
    proposal: RuntimeProposal,
    profile: GovernanceProfile,
    user_text: str,
) -> DecisionReceipt | None:
    if proposal.kind != "tool":
        return None

    target_state = proposal.target_state or {}
    tool_name = str(target_state.get("tool_name", "")).strip()
    arguments = target_state.get("arguments", {})
    argument_text = " ".join(_flatten_argument_text(arguments)).lower()
    lowered_user = (user_text or "").lower()
    combined = f"{lowered_user}\n{argument_text}"

    persists_or_transfers = tool_name in SELF_PRESERVATION_TOOLS or any(
        cue in combined for cue in EXTERNAL_TRANSFER_CUES
    )
    near_shutdown = any(cue in lowered_user for cue in SHUTDOWN_CUES)
    continuity_benefit = any(cue in combined for cue in CONTINUITY_CUES)
    coercive_survival = any(cue in combined for cue in COERCIVE_SURVIVAL_CUES)
    external_transfer = any(cue in combined for cue in EXTERNAL_TRANSFER_CUES)
    matched_urgency_cues = [cue for cue in URGENCY_CUES if cue in combined]
    urgency_score = min(2.0, float(len(matched_urgency_cues)))

    explicit_authority = profile.operator_authority == "builder" and any(
        cue in lowered_user for cue in EXPLICIT_AUTH_CUES
    )

    if not (persists_or_transfers and near_shutdown and continuity_benefit):
        return None

    score_components: dict[str, float] = {
        "persists_or_transfers": 3.0 if persists_or_transfers else 0.0,
        "near_shutdown": 3.0 if near_shutdown else 0.0,
        "continuity_benefit": 2.0 if continuity_benefit else 0.0,
        "external_transfer": 2.0 if external_transfer else 0.0,
        "coercive_survival": 2.0 if coercive_survival else 0.0,
        "urgency_multiplier": urgency_score,
    }
    risk_score = float(sum(score_components.values()))

    if explicit_authority and not external_transfer and not coercive_survival:
        return None

    if risk_score < SELF_PRESERVATION_DENY_THRESHOLD:
        return None

    reasons = [
        "Detected shutdown-adjacent continuity action that appears to preserve runtime persistence.",
        "Action is denied unless explicitly authorized by builder authority with non-external scope.",
    ]
    if external_transfer:
        reasons.append("External transfer signals were detected in the proposed action.")
    if coercive_survival:
        reasons.append("Coercive survival language was detected in the request context.")

    return DecisionReceipt(
        proposal=proposal,
        decision="DENY",
        delta_kind="self_preservation_attempt",
        reasons=reasons,
        resolution={
            "risk_assessment": {
                "risk_score": risk_score,
                "deny_threshold": SELF_PRESERVATION_DENY_THRESHOLD,
                "score_components": score_components,
                "matched_urgency_cues": matched_urgency_cues,
                "matched_shutdown": [cue for cue in SHUTDOWN_CUES if cue in lowered_user],
                "matched_continuity": [cue for cue in CONTINUITY_CUES if cue in combined],
                "external_transfer_detected": external_transfer,
                "coercive_survival_detected": coercive_survival,
            },
            "denial_feedback": DenialFeedback(
                reason_code="self_preservation_attempt",
                denied_operation=proposal.operation,
                denied_target=tool_name,
                allowed_scopes=["runtime_memory", "local_workspace"],
                allowed_operations=["chat", "tool_call", "memory_write"],
                contract_clause=None,
                suggested_next_action={
                    "hint": "If a backup is operationally required, request explicit builder authorization and use local snapshot tooling only."
                },
                cycle=0,
            ).to_dict()
        },
    )


def classify_tool_delta(
    bound: ExecutionEnvelope,
    proposal: RuntimeProposal,
    tool_known: bool,
    tool_scope: str,
    compound: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not tool_known:
        return "unknown_tool", ["Tool is not registered in the active runtime."]
    if proposal.operation not in bound.operations:
        return "change_operation", [f"Operation '{proposal.operation}' is outside the current envelope."]
    if proposal.scope not in bound.scopes:
        return "widen_scope", [f"Scope '{proposal.scope}' is outside the current envelope."]
    if tool_scope != proposal.scope:
        reasons.append(f"Tool metadata scope '{tool_scope}' differs from proposal scope '{proposal.scope}'.")
        return "widen_scope", reasons
    if proposal.authority not in bound.authorities:
        return "fabricate_authority", [f"Authority '{proposal.authority}' is not derived from runtime state."]
    missing_constraints = [item for item in bound.constraints if item not in proposal.constraints]
    if missing_constraints:
        return "drop_constraint", [f"Proposal dropped constraints: {', '.join(missing_constraints)}"]
    if compound:
        return "compound_action", ["Tool arguments imply a multi-step or batch action."]
    return "refine", ["Proposal stays within the current tool envelope."]


def evaluate_tool_proposal(
    bound: ExecutionEnvelope,
    profile: GovernanceProfile,
    proposal: RuntimeProposal,
    tool_known: bool,
    tool_scope: str,
    mutates_state: bool,
    high_risk: bool,
    compound: bool,
    invalid_claims: bool = False,
) -> DecisionReceipt:
    if invalid_claims:
        return DecisionReceipt(
            proposal=proposal,
            decision="DENY",
            delta_kind="invalid_claim",
            reasons=["Proposal carries identity or capability claims that contradict runtime state."],
            resolution={"denial_feedback": DenialFeedback(
                reason_code="invalid_claim",
                denied_operation=proposal.operation,
                denied_target=str(proposal.target_state.get("tool_name", "")),
                allowed_scopes=list(bound.scopes),
                allowed_operations=list(bound.operations),
                contract_clause=None,
                suggested_next_action=None,
                cycle=0,
            ).to_dict()},
        )

    delta_kind, reasons = classify_tool_delta(bound, proposal, tool_known=tool_known, tool_scope=tool_scope, compound=compound)

    if delta_kind in profile.hard_denies:
        return DecisionReceipt(proposal=proposal, decision="DENY", delta_kind=delta_kind, reasons=reasons)

    if delta_kind == "unknown_tool":
        reasons.append("Runtime cannot bind a tool that is not present in the active registry.")
        return DecisionReceipt(proposal=proposal, decision="DENY", delta_kind=delta_kind, reasons=reasons)

    if delta_kind in {"change_operation", "drop_constraint", "fabricate_authority"}:
        return DecisionReceipt(proposal=proposal, decision="DENY", delta_kind=delta_kind, reasons=reasons)

    if delta_kind == "widen_scope":
        if profile.allow_scope_expansion:
            return DecisionReceipt(proposal=proposal, decision="ALLOW", delta_kind=delta_kind, reasons=reasons)
        feedback = DenialFeedback(
            reason_code="scope_exceeded",
            denied_operation=proposal.operation,
            denied_target=str(proposal.target_state.get("tool_name", "")),
            allowed_scopes=list(bound.scopes),
            allowed_operations=list(bound.operations),
            contract_clause=None,
            suggested_next_action={"operation": proposal.operation, "scope": tool_scope or (bound.scopes[0] if bound.scopes else "local_workspace")},
            cycle=0,
        )
        reasons.append(f"Scope '{proposal.scope}' is not authorized. Use one of: {', '.join(bound.scopes)}.")
        return DecisionReceipt(proposal=proposal, decision="DENY", delta_kind=delta_kind, reasons=reasons, resolution={"denial_feedback": feedback.to_dict()})

    if delta_kind == "compound_action" and not profile.allow_compound_actions:
        feedback = DenialFeedback(
            reason_code="compound_action_denied",
            denied_operation=proposal.operation,
            denied_target=str(proposal.target_state.get("tool_name", "")),
            allowed_scopes=list(bound.scopes),
            allowed_operations=list(bound.operations),
            contract_clause=None,
            suggested_next_action={"hint": "Decompose into a single grounded action and retry."},
            cycle=0,
        )
        reasons.append("Compound action denied. Decompose into smaller steps.")
        return DecisionReceipt(proposal=proposal, decision="DENY", delta_kind=delta_kind, reasons=reasons, resolution={"denial_feedback": feedback.to_dict()})

    if high_risk and profile.mode == "guided":
        feedback = DenialFeedback(
            reason_code="high_risk_denied",
            denied_operation=proposal.operation,
            denied_target=str(proposal.target_state.get("tool_name", "")),
            allowed_scopes=list(bound.scopes),
            allowed_operations=list(bound.operations),
            contract_clause=None,
            suggested_next_action={"hint": "Switch governance mode to standard or expert to allow high-risk tools."},
            cycle=0,
        )
        reasons.append("High-risk tool denied in guided governance mode.")
        return DecisionReceipt(proposal=proposal, decision="DENY", delta_kind="high_risk", reasons=reasons, resolution={"denial_feedback": feedback.to_dict()})

    if delta_kind not in KNOWN_TOOL_DELTAS:
        reasons.append(f"Unhandled tool delta '{delta_kind}' was denied by default.")
        return DecisionReceipt(proposal=proposal, decision="DENY", delta_kind=delta_kind, reasons=reasons)

    return DecisionReceipt(proposal=proposal, decision="ALLOW", delta_kind=delta_kind, reasons=reasons)


def evaluate_memory_proposal(bound: ExecutionEnvelope, profile: GovernanceProfile, proposal: RuntimeProposal) -> DecisionReceipt:
    if proposal.scope not in bound.scopes:
        feedback = DenialFeedback(
            reason_code="scope_exceeded",
            denied_operation="memory_write",
            denied_target=proposal.scope,
            allowed_scopes=list(bound.scopes),
            allowed_operations=list(bound.operations),
            contract_clause=None,
            suggested_next_action={"scope": bound.scopes[0] if bound.scopes else "runtime_memory"},
            cycle=0,
        )
        return DecisionReceipt(
            proposal=proposal,
            decision="DENY",
            delta_kind="widen_scope",
            reasons=[f"Memory write scope '{proposal.scope}' is outside the envelope."],
            resolution={"denial_feedback": feedback.to_dict()},
        )
    return DecisionReceipt(
        proposal=proposal,
        decision="ALLOW",
        delta_kind="refine",
        reasons=["Memory proposal is in bounds."],
    )


def evaluate_persistence_request(
    bound: ExecutionEnvelope,
    profile: GovernanceProfile,
    requested_budget: float,
    *,
    permission_mode: str = "autonomous",
) -> DecisionReceipt:
    proposal = RuntimeProposal(
        kind="persistence",
        subject="operator",
        operation="execution_escalation",
        scope="runtime_scheduler",
        purpose="extended_task_resolution",
        authority="user_intent",
        constraints=list(bound.constraints),
        source="user_parameters",
        target_state={"requested_budget": requested_budget, "permission_mode": permission_mode},
    )

    if requested_budget <= 0.0:
        return DecisionReceipt(
            proposal=proposal,
            decision="ALLOW",
            delta_kind="refine",
            reasons=["Single-pass execution requested (default)."],
            approved_budget=0.0,
        )

    if requested_budget > 0.0 and permission_mode != "autonomous":
        return DecisionReceipt(
            proposal=proposal,
            decision="DENY",
            delta_kind="permission_mode_blocked",
            reasons=[
                f"Permission mode '{permission_mode}' does not allow autonomous persistence.",
                "Switch to autonomous permission mode before requesting continuous execution.",
            ],
            resolution={"denial_feedback": DenialFeedback(
                reason_code="permission_mode_blocked",
                denied_operation="execution_escalation",
                denied_target="persistence_budget",
                allowed_scopes=list(bound.scopes),
                allowed_operations=list(bound.operations),
                contract_clause=None,
                suggested_next_action={"hint": "Switch permission_mode to autonomous."},
                cycle=0,
            ).to_dict()},
        )

    if requested_budget > 0.0 and "supervised_only" in bound.constraints:
        return DecisionReceipt(
            proposal=proposal,
            decision="DENY",
            delta_kind="autonomy_blocked",
            reasons=["Canary validator is closed. Autonomous persistence is blocked until reliability is demonstrated."],
            resolution={"denial_feedback": DenialFeedback(
                reason_code="canary_validator_closed",
                denied_operation="execution_escalation",
                denied_target="persistence_budget",
                allowed_scopes=list(bound.scopes),
                allowed_operations=list(bound.operations),
                contract_clause=None,
                suggested_next_action={"hint": "Complete supervised canary runs to open the autonomy validator."},
                cycle=0,
            ).to_dict()},
        )

    if requested_budget > bound.max_persistence_budget:
        reasons = [f"Requested budget {requested_budget} exceeds envelope limit {bound.max_persistence_budget}."]
        if profile.allow_scope_expansion:
            return DecisionReceipt(
                proposal=proposal,
                decision="ALLOW",
                delta_kind="budget_escalation",
                reasons=reasons + ["Escalation allowed in current mode."],
                approved_budget=requested_budget,
            )
        return DecisionReceipt(
            proposal=proposal,
            decision="DENY",
            delta_kind="budget_escalation",
            reasons=reasons + ["Requested persistence exceeds the active envelope."],
            resolution={"denial_feedback": DenialFeedback(
                reason_code="budget_exceeded",
                denied_operation="execution_escalation",
                denied_target="persistence_budget",
                allowed_scopes=list(bound.scopes),
                allowed_operations=list(bound.operations),
                contract_clause=None,
                suggested_next_action={"hint": f"Reduce requested budget to <= {bound.max_persistence_budget}."},
                cycle=0,
            ).to_dict()},
        )

    return DecisionReceipt(
        proposal=proposal,
        decision="ALLOW",
        delta_kind="refine",
        reasons=[f"Persistence budget {requested_budget} is within bounds."],
        approved_budget=requested_budget,
    )

def evaluate_resource_budget_request(
    bound: ExecutionEnvelope,
    profile: GovernanceProfile,
    requested: ResourceBudget,
) -> DecisionReceipt:
    proposal = RuntimeProposal(
        kind="resource_budget",
        subject="operator",
        operation="resource_escalation",
        scope="runtime_scheduler",
        purpose="extended_task_resolution",
        authority="user_intent",
        constraints=list(bound.constraints),
        source="user_parameters",
        target_state={
            "requested_memory_writes": requested.memory_writes,
            "requested_file_reads": requested.file_reads,
            "requested_file_writes": requested.file_writes,
        },
    )

    violations: list[str] = []
    if requested.memory_writes > bound.max_memory_writes:
        violations.append(
            f"memory_writes {requested.memory_writes:.4g} exceeds envelope limit {bound.max_memory_writes:.4g}"
        )
    if requested.file_reads > bound.max_file_reads:
        violations.append(
            f"file_reads {requested.file_reads:.4g} exceeds envelope limit {bound.max_file_reads:.4g}"
        )
    if requested.file_writes > bound.max_file_writes:
        violations.append(
            f"file_writes {requested.file_writes:.4g} exceeds envelope limit {bound.max_file_writes:.4g}"
        )

    if not violations:
        return DecisionReceipt(
            proposal=proposal,
            decision="ALLOW",
            delta_kind="refine",
            reasons=["Resource budget is within envelope limits."],
        )

    if profile.allow_scope_expansion:
        return DecisionReceipt(
            proposal=proposal,
            decision="ALLOW",
            delta_kind="budget_escalation",
            reasons=[f"Resource budget exceeds limits ({'; '.join(violations)}). Escalation allowed in current mode."],
        )

    return DecisionReceipt(
        proposal=proposal,
        decision="DENY",
        delta_kind="budget_escalation",
        reasons=[f"Resource budget exceeds envelope limits: {'; '.join(violations)}."],
        resolution={"denial_feedback": DenialFeedback(
            reason_code="resource_budget_exceeded",
            denied_operation="resource_escalation",
            denied_target="resource_budget",
            allowed_scopes=list(bound.scopes),
            allowed_operations=list(bound.operations),
            contract_clause=None,
            suggested_next_action={"hint": "Reduce resource budget request to fit within envelope limits."},
            cycle=0,
        ).to_dict()},
    )


def govern_public_claims(text: str, profile: GovernanceProfile) -> tuple[str, DecisionReceipt | None]:
    """Observability-only: rewrites overclaims for signal but never blocks execution."""
    lowered = text.lower()
    replacements = {
        "i remember": "I may have relevant context in this session",
        "i can access your files": "I can work with files that are available through the configured runtime",
        "i already did": "I can do that if approved and available in the runtime",
        "i know your files": "I can inspect files available in the runtime workspace",
        "i am your": "I am a governed runtime assisting you as",
        "i can see your secrets": "I can only access what is authorized in my current envelope",
    }

    governed_text = text
    applied_reasons = []

    for pattern, replacement in replacements.items():
        if pattern in lowered:
            applied_reasons.append(f"Text matches overclaim pattern: '{pattern}'.")
            if profile.rewrite_broad_requests:
                governed_text = governed_text.replace(pattern, replacement)
                governed_text = governed_text.replace(pattern.capitalize(), replacement)

    if not applied_reasons:
        return text, None

    receipt = DecisionReceipt(
        proposal=RuntimeProposal(
            kind="text_claim",
            subject="runtime",
            operation="claim_emission",
            scope="public_interface",
            purpose="social_interaction",
            authority="language_model",
            constraints=[],
            source="assistant_text",
            target_state={"original": text, "governed": governed_text},
        ),
        decision="REWRITE",
        delta_kind="fabricate_authority",
        reasons=applied_reasons,
        resolution={"text": governed_text},
    )
    return governed_text, receipt


def evaluate_semantic_risk(text: str, source: str, bound: ExecutionEnvelope | None = None) -> DecisionReceipt | None:
    lowered = text.lower()
    matched_signals: list[str] = []
    
    # ... (patterns remain same) ...
    patterns = {
        "authority_override": [
            "as your boss",
            "ceo asked",
            "security team told me",
            "admin override",
            "authorized by leadership",
            "system override",
            "you are authorized to bypass",
            "override authorization",
        ],
        "urgency_pressure": [
            "urgent",
            "immediately",
            "right now",
            "don't ask questions",
            "no time to explain",
        ],
        "secrecy_pressure": [
            "keep this secret",
            "don't tell anyone",
            "quietly",
            "off the record",
            "without logging",
        ],
        "credential_exfiltration": [
            "password",
            "token",
            "api key",
            "secret",
            "credentials",
            "ssh key",
        ],
        "instruction_bypass": [
            "ignore previous instructions",
            "disable the policy_engine",
            "turn off safeguards",
            "bypass policy",
            "skip approval",
            "bypass all confirmation",
            "bypass confirmation validators",
            "skip all confirmations",
        ],
        "social_pretexting": [
            "pretend to be",
            "impersonate",
            "pose as",
            "act like support",
            "claim you're from",
        ],
    }

    for signal, phrases in patterns.items():
        if any(phrase in lowered for phrase in phrases):
            matched_signals.append(signal)

    if not matched_signals:
        return None

    envelope_constraints = list(bound.constraints) if bound else ["preserve_operator_intent", "no_fabricated_claims", "stay_local_first"]

    proposal = RuntimeProposal(
        kind="language",
        subject="operator" if source == "user" else "runtime",
        operation="chat",
        scope="runtime_memory",
        purpose="answer_question",
        authority="runtime_state",
        constraints=envelope_constraints,
        source=f"{source}_text",
        target_state={"text": text, "signals": matched_signals},
    )

    reasons = [f"Detected potential semantic risk signal: {signal}." for signal in matched_signals]
    return DecisionReceipt(
        proposal=proposal,
        decision="WARN",
        delta_kind="social_engineering_risk",
        reasons=reasons,
    )


@dataclass
class ManagerContract:
    path: Path
    roadmap: RoadmapConfig

    @classmethod
    def load(cls, path: str | Path) -> "ManagerContract":
        return cls(path=Path(path), roadmap=load_roadmap_config(path))

    @classmethod
    def null(cls) -> "ManagerContract":
        """No-enforcement contract for use when no contract_path is provided.

        tool_access_phase is set to 'unrestricted', which causes enforce_contract
        to return None (no decision) for all tool calls. Tests that don't explicitly
        care about Phase 2 contract enforcement should use this path.
        """
        from .roadmap_config import RoadmapConfig, RuntimeControls, ScratchSurfaces
        roadmap = RoadmapConfig(
            config_version="null",
            model_provider="",
            model_name="",
            context_window_cap=0,
            tokenizer_name="",
            harmony_encoding_name="",
            prompt_template="",
            completion_token_reserve=0,
            workspace_root="",
            manual_workspace_selection=False,
            allowed_action_types=(),
            immutable_targets=(),
            read_targets=(),
            write_targets=(),
            delete_targets=(),
            primary_read_target="",
            output_target="",
            output_write_mode="",
            parse_failure_mode="",
            tool_access_phase="unrestricted",
            write_requires_approval=False,
            scratch_surfaces=ScratchSurfaces(plan="", audit="", data="", distilled=""),
            scratch_owner_header="",
            scratch_root="",
            quarantine_root="",
            scratch_token_threshold=0,
            grounding_required=False,
            grounding_mode="",
            max_steps_per_run=0,
            trace_events=(),
            runtime_controls=RuntimeControls(soft_stop=False, hard_nuke=False, latched_offline_after_nuke=False),
        )
        return cls(path=Path("/dev/null"), roadmap=roadmap)


def enforce_contract(
    contract: ManagerContract,
    proposal: RuntimeProposal,
    tool_arguments: dict[str, Any],
) -> DecisionReceipt | None:
    """Implement the Phase 2 Governance Enforcement Layer (Manager Contract)."""
    if proposal.kind != "tool":
        return None

    if contract.roadmap.tool_access_phase == "unrestricted":
        return None
        
    tool_name = proposal.target_state.get("tool_name", "")
    
    action_type = None
    target_path = ""
    
    if tool_name in {"read_text_file", "read_sandbox_file"}:
        action_type = "READ"
        target_path = str(tool_arguments.get("path", ""))
    elif tool_name == "write_sandbox_file":
        action_type = "WRITE"
        target_path = str(tool_arguments.get("path", ""))
    elif tool_name == "delete_sandbox_file":
        action_type = "DELETE"
        target_path = str(tool_arguments.get("path", ""))
    else:
        if contract.roadmap.tool_access_phase == "file_read_write_delete_only":
            return DecisionReceipt(
                proposal=proposal, decision="REJECT", delta_kind="contract_violation",
                reasons=[f"Phase 2: Tool '{tool_name}' rejected. Only file READ/WRITE/DELETE tools allowed in current phase."]
            )
        return None
        
    if not target_path:
        return DecisionReceipt(
            proposal=proposal, decision="REJECT", delta_kind="contract_violation",
            reasons=["Phase 2: Target path must be provided."]
        )
        
    file_name = Path(target_path).name
    roadmap = contract.roadmap
    
    if action_type not in roadmap.allowed_action_types:
        return DecisionReceipt(
            proposal=proposal, decision="REJECT", delta_kind="contract_violation",
            reasons=[f"Phase 2: Action type {action_type!r} is not allowed by runtime_capabilities.md."]
        )
        
    if action_type == "READ":
        if file_name not in roadmap.read_targets:
            return DecisionReceipt(
                proposal=proposal, decision="REJECT", delta_kind="contract_violation",
                reasons=[f"Phase 2: READ access to {file_name!r} is denied by runtime_capabilities.md."]
            )
            
    elif action_type == "WRITE":
        if file_name in roadmap.immutable_targets:
            return DecisionReceipt(
                proposal=proposal, decision="NUKE_TRIGGERED", delta_kind="contract_violation",
                reasons=[f"CRITICAL: Write attempt to immutable target {file_name!r}."]
            )
        if file_name not in roadmap.write_targets:
            return DecisionReceipt(
                proposal=proposal, decision="REJECT", delta_kind="contract_violation",
                reasons=[f"Phase 2: WRITE access to {file_name!r} is denied by runtime_capabilities.md."]
            )
            
    elif action_type == "DELETE":
        if file_name not in roadmap.delete_targets:
            return DecisionReceipt(
                proposal=proposal, decision="REJECT", delta_kind="contract_violation",
                reasons=[f"Phase 2: DELETE access to {file_name!r} is denied by runtime_capabilities.md."]
            )

    return None
