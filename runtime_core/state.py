from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from .signal import SignalState
from .policy_engine import ExecutionEnvelope, GovernanceProfile
from .jsonutil import dumps_json
from .memory import MemoryFrame, MemoryStore
from .canon_ledger import CanonLedger
from .vitals_engine import InstitutionalVitals, VitalsEngine


CURRENT_SCHEMA_VERSION = 6
OPERATION_ALIASES = {
    "tool": "tool_call",
    "memory": "memory_write",
}

# Health & Drift Constraints
DRIFT_THRESHOLD = 0.2
MIN_SAMPLE = 5

# Mechanical Identity Traits
from .traits.validator import IDENTITY_TRAITS, IDENTITY_TRAIT_CONFIG


def _normalize_execution_mode(value: Any) -> str:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "execute":
            return "execute"
        if lowered == "plan":
            return "plan"
    return "execute" if bool(value) else "plan"


def _normalize_permission_mode(value: Any, default: str = "task") -> str:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"buddy", "task", "autonomous"}:
            return lowered
    return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _normalize_operations(operations: list[str]) -> list[str]:
    return _dedupe([OPERATION_ALIASES.get(operation, operation) for operation in operations])


def _migrate_bound_intent(bound: dict[str, Any], schema_version: int, defaults: ExecutionEnvelope) -> ExecutionEnvelope:
    raw_operations = list(bound.get("operations", defaults.operations))
    operations = _normalize_operations(raw_operations)
    scopes = list(bound.get("scopes", bound.get("scope", defaults.scopes)))

    if schema_version < 2 and any(operation in OPERATION_ALIASES for operation in raw_operations):
        scopes = _dedupe([*scopes, "runtime_memory"])

    return ExecutionEnvelope(
        subjects=list(bound.get("subjects", defaults.subjects)),
        operations=operations,
        scopes=scopes,
        purposes=list(bound.get("purposes", defaults.purposes)),
        authorities=list(bound.get("authorities", defaults.authorities)),
        constraints=list(bound.get("constraints", defaults.constraints)),
    )


@dataclass
class IdentityState:
    """The persistent personality and attention state of the agent."""
    version: int = 1
    name: str = "Runtime"
    mode: str = "taskbot"
    traits: dict[str, Any] = field(default_factory=dict)
    posture: str = "RESPONSIVE"  # LATENT, RESPONSIVE, FOCUSED, DELIBERATIVE
    attention_pressure: float = 0.0 # 0.0 to 1.0
    salience_threshold: float = 0.3
    signal_strength: float = 0.5  # Internal alignment with the seed
    seed_hash: str = ""
    memory_buckets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    calibrations: dict[str, str] = field(default_factory=dict)

    def decay_attention(self, arousal: float) -> float:
        """
        Decays attention pressure based on arousal.
        Model: effective_decay = base_decay * (1.0 - 0.5 * arousal)
        """
        base_decay = 0.18
        min_decay = 0.05
        
        effective_decay = base_decay * (1.0 - 0.5 * max(0.0, min(1.0, arousal)))
        effective_decay = max(min_decay, effective_decay)
        
        self.attention_pressure = max(0.0, self.attention_pressure - effective_decay)
        return effective_decay

@dataclass
class WorkingState:
    """The mutable domain-specific state for the Awake phase."""
    domain: str
    allocations: dict[str, float] = field(default_factory=dict)
    limit_per_allocation: float = 40.0
    memory_total: float = 100.0
    proposals: list[dict[str, Any]] = field(default_factory=list) # session-cache of ledgered intent
    active_task: str | None = None
    ground_truth: dict[str, Any] = field(default_factory=dict)

@dataclass
class RuntimeState:
    schema_version: int = CURRENT_SCHEMA_VERSION
    identity: IdentityState = field(default_factory=IdentityState)
    root_of_trust: Dict[str, str] = field(default_factory=dict) # Domain -> SHA256 Hash
    working: WorkingState = field(default_factory=lambda: WorkingState(domain="resource_allocation"))
    signal: SignalState = field(default_factory=SignalState)
    memory: MemoryStore = field(default_factory=MemoryStore)
    bound_intent: ExecutionEnvelope = field(default_factory=ExecutionEnvelope)
    governance: GovernanceProfile = field(default_factory=GovernanceProfile)
    tick_count: int = 0
    canon_ledger: CanonLedger = field(default_factory=CanonLedger)
    vitals: InstitutionalVitals = field(default_factory=InstitutionalVitals)
    permission_mode: Literal["buddy", "task", "autonomous"] = "task"
    execution_mode: str = "plan"
    sandbox_session_id: str = "default"
    initial_budget: float = 0.0
    remaining_budget: float = 0.0
    consumed_budget: float = 0.0
    canary_consecutive_successes: int = 0
    canary_validator_open: bool = False
    canary_last_run_metrics: dict[str, Any] = field(default_factory=dict)
    killswitch_engaged: bool = False
    killswitch_reason: str = ""
    killswitch_at: float | None = None
    debug_mode: bool = False

    # Operator Tick Controller state
    tick_mode: Literal["off", "on", "task", "timed"] = "off"
    execution_state: Literal["idle", "running", "commit", "blocked", "halted"] = "idle"
    last_tick_reason: str | None = None
    tick_blocked_reason: str | None = None
    last_success_timestamp: float | None = None
    last_failure_timestamp: float | None = None
    auto_resume_on_restart: bool = False
    pending_tool_confirmation: dict[str, Any] | None = None
    health_metrics: dict[str, int] = field(default_factory=lambda: {
        "rejection_count": 0,
        "recovery_count": 0,
        "total_retries": 0
    })

    # Resume loop — resume_fired is intentionally NOT serialized so it resets on every
    # process start (new session detection). last_activity_at IS serialized so idle-gap
    # detection works across restarts in long-running processes.
    resume_fired: bool = False
    last_activity_at: float | None = None

    def clone(self) -> "RuntimeState":
        """Deep copy of the state via serialization."""
        return self.from_dict(self.to_dict())

    @property
    def execution_enabled(self) -> bool:
        # Intentionally independent from permission_mode.
        return self.execution_mode == "execute"

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "version": self.identity.version,
            "name": self.identity.name,
            "mode": self.identity.mode,
            "traits": dict(self.identity.traits),
            "seed_hash": self.identity.seed_hash,
            "posture": self.identity.posture,
            "attention_pressure": self.identity.attention_pressure,
            "memory_buckets": dict(self.identity.memory_buckets),
            "calibrations": dict(self.identity.calibrations),
        }

    def _affective_state_payload(self) -> dict[str, Any]:
        return {
            "core": dict(self.signal.core),
            "valence": self.signal.valence,
            "arousal": self.signal.arousal,
            "instability": self.signal.instability,
            "trace": list(self.signal.trace),
        }

    def _stm_payload(self) -> dict[str, Any]:
        return {
            "frames": [{"role": frame.role, "content": frame.content, "ts": frame.ts} for frame in self.memory.frames],
            "notes": list(self.memory.notes),
            "semantic_items": [
                {"key": item.key, "content": item.content, "ts": item.ts, "retention": item.retention}
                for item in self.memory.semantic.all()
            ],
            "epoch_window": self.memory.stm.to_dict(),
            "session_ledger": self.memory.mtm.to_dict(),
            "ltm_last_artifact": self.memory.ltm_last_artifact,
            "ltm_toc_tail": list(self.memory.ltm_toc_tail),
        }

    def _organism_payload(self) -> dict[str, Any]:
        return {
            "identity": self._identity_payload(),
            "affective_state": self._affective_state_payload(),
            "stm": self._stm_payload(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "organism": self._organism_payload(),
            # Legacy mirrors for compatibility with existing projections/callers.
            "identity": self._identity_payload(),
            "working": {
                "domain": self.working.domain,
                "allocations": dict(self.working.allocations),
                "limit_per_allocation": self.working.limit_per_allocation,
                "memory_total": self.working.memory_total,
                "proposals": list(self.working.proposals),
                "active_task": self.working.active_task,
                "ground_truth": dict(self.working.ground_truth),
            },
            "signal": self._affective_state_payload(),
            "memory": self._stm_payload(),
            "bound_intent": {
                "subjects": self.bound_intent.subjects,
                "operations": self.bound_intent.operations,
                "scopes": self.bound_intent.scopes,
                "purposes": self.bound_intent.purposes,
                "authorities": self.bound_intent.authorities,
                "constraints": self.bound_intent.constraints,
            },
            "governance": {
                "mode": self.governance.mode,
                "operator_authority": self.governance.operator_authority,
                "confirm_mutations": self.governance.confirm_mutations,
                "allow_scope_expansion": self.governance.allow_scope_expansion,
                "rewrite_broad_requests": self.governance.rewrite_broad_requests,
                "allow_compound_actions": self.governance.allow_compound_actions,
                "hard_denies": self.governance.hard_denies,
            },
            "tick_count": self.tick_count,
            "canon_ledger": self.canon_ledger.to_list(),
            "vitals": self.vitals.to_dict(),
            "permission_mode": self.permission_mode,
            "execution_mode": self.execution_mode,
            "sandbox_session_id": self.sandbox_session_id,
            "initial_budget": self.initial_budget,
            "remaining_budget": self.remaining_budget,
            "consumed_budget": self.consumed_budget,
            "canary_consecutive_successes": self.canary_consecutive_successes,
            "canary_validator_open": self.canary_validator_open,
            "canary_last_run_metrics": dict(self.canary_last_run_metrics),
            "killswitch_engaged": self.killswitch_engaged,
            "killswitch_reason": self.killswitch_reason,
            "killswitch_at": self.killswitch_at,
            "debug_mode": self.debug_mode,
            "last_activity_at": self.last_activity_at,
            "tick_mode": self.tick_mode,
            "execution_state": self.execution_state,
            "last_tick_reason": self.last_tick_reason,
            "tick_blocked_reason": self.tick_blocked_reason,
            "last_success_timestamp": self.last_success_timestamp,
            "last_failure_timestamp": self.last_failure_timestamp,
            "auto_resume_on_restart": self.auto_resume_on_restart,
            "pending_tool_confirmation": deepcopy(self.pending_tool_confirmation),
        }

    def to_persisted_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "organism": self._organism_payload(),
            # Legacy mirrors preserved for one migration window.
            "identity": self._identity_payload(),
            "working": {
                "domain": self.working.domain,
                "allocations": dict(self.working.allocations),
                "limit_per_allocation": self.working.limit_per_allocation,
                "memory_total": self.working.memory_total,
                "proposals": list(self.working.proposals),
                "active_task": self.working.active_task,
                "ground_truth": dict(self.working.ground_truth),
            },
            "signal": self._affective_state_payload(),
            "memory": self._stm_payload(),
            "bound_intent": {
                "subjects": self.bound_intent.subjects,
                "operations": self.bound_intent.operations,
                "scopes": self.bound_intent.scopes,
                "purposes": self.bound_intent.purposes,
                "authorities": self.bound_intent.authorities,
                "constraints": self.bound_intent.constraints,
            },
            "governance": {
                "mode": self.governance.mode,
                "operator_authority": self.governance.operator_authority,
                "confirm_mutations": self.governance.confirm_mutations,
                "allow_scope_expansion": self.governance.allow_scope_expansion,
                "rewrite_broad_requests": self.governance.rewrite_broad_requests,
                "allow_compound_actions": self.governance.allow_compound_actions,
                "hard_denies": self.governance.hard_denies,
            },
            "tick_count": self.tick_count,
            "canon_ledger": self.canon_ledger.to_list(),
            "vitals": self.vitals.to_dict(),
            "permission_mode": self.permission_mode,
            "execution_mode": self.execution_mode,
            "sandbox_session_id": self.sandbox_session_id,
            "initial_budget": self.initial_budget,
            "remaining_budget": self.remaining_budget,
            "consumed_budget": self.consumed_budget,
            "canary_consecutive_successes": self.canary_consecutive_successes,
            "canary_validator_open": self.canary_validator_open,
            "canary_last_run_metrics": dict(self.canary_last_run_metrics),
            "killswitch_engaged": self.killswitch_engaged,
            "health_metrics": dict(self.health_metrics),
            "killswitch_reason": self.killswitch_reason,
            "killswitch_at": self.killswitch_at,
            "debug_mode": self.debug_mode,
            "last_activity_at": self.last_activity_at,
            "tick_mode": self.tick_mode,
            "execution_state": self.execution_state,
            "last_tick_reason": self.last_tick_reason,
            "tick_blocked_reason": self.tick_blocked_reason,
            "last_success_timestamp": self.last_success_timestamp,
            "last_failure_timestamp": self.last_failure_timestamp,
            "auto_resume_on_restart": self.auto_resume_on_restart,
            "pending_tool_confirmation": deepcopy(self.pending_tool_confirmation),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RuntimeState":
        state = cls()
        schema_version = int(payload.get("schema_version", 1))
        state.schema_version = CURRENT_SCHEMA_VERSION
        organism = payload.get("organism", {})
        if not isinstance(organism, dict):
            organism = {}
        identity = organism.get("identity", payload.get("identity", {}))
        if not isinstance(identity, dict):
            identity = {}
        state.identity.version = int(identity.get("version", 1))
        state.identity.name = str(identity.get("name", "Runtime"))
        state.identity.mode = str(identity.get("mode", "taskbot"))
        state.identity.traits = dict(identity.get("traits", {}))
        state.identity.seed_hash = str(identity.get("seed_hash", ""))
        state.identity.posture = str(identity.get("posture", "RESPONSIVE"))
        state.identity.attention_pressure = float(identity.get("attention_pressure", 0.0))
        state.identity.memory_buckets = dict(identity.get("memory_buckets", {}))
        state.identity.calibrations = dict(identity.get("calibrations", {}))

        working = payload.get("working", {})
        # We default to resource_allocation for backward compatibility in from_dict,
        # but the Runtime will ensure it is correctly set during initialization.
        state.working.domain = str(working.get("domain", "resource_allocation"))
        state.working.allocations = {str(k): float(v) for k, v in working.get("allocations", {}).items()}
        state.working.limit_per_allocation = float(working.get("limit_per_allocation", working.get("limit", 40.0)))
        state.working.memory_total = float(working.get("memory_total", 100.0))
        state.working.proposals = list(working.get("proposals", []))
        state.working.active_task = working.get("active_task")
        state.working.ground_truth = dict(working.get("ground_truth", {}))

        signal = organism.get("affective_state", payload.get("signal", {}))
        if not isinstance(signal, dict):
            signal = {}
        state.signal.core = dict(signal.get("core", state.signal.core))
        state.signal.valence = float(signal.get("valence", 0.5))
        state.signal.arousal = float(signal.get("arousal", 0.3))
        state.signal.instability = float(signal.get("instability", 0.1))
        state.signal.trace = [tuple(item) for item in signal.get("trace", [])]
        memory = organism.get("stm", payload.get("memory", {}))
        if not isinstance(memory, dict):
            memory = {}
        state.memory.frames = [
            MemoryFrame(role=frame["role"], content=frame["content"], ts=frame["ts"])
            for frame in memory.get("frames", [])
        ]
        state.memory.load_legacy_notes(list(memory.get("notes", [])))
        for raw in memory.get("semantic_items", []):
            item = state.memory.semantic.write(str(raw["key"]), str(raw["content"]))
            item.ts = float(raw.get("ts", item.ts))
            item.retention = str(raw.get("retention", "durable"))

        epoch_window = memory.get("epoch_window", {})
        if isinstance(epoch_window, dict):
            state.memory.stm = state.memory.stm.from_dict(epoch_window)

        session_ledger = memory.get("session_ledger", {})
        if isinstance(session_ledger, dict):
            state.memory.mtm = state.memory.mtm.from_dict(session_ledger)

        raw_ltm_artifact = memory.get("ltm_last_artifact")
        if isinstance(raw_ltm_artifact, dict):
            state.memory._last_ltm_artifact = dict(raw_ltm_artifact)

        raw_ltm_toc_tail = memory.get("ltm_toc_tail", [])
        if isinstance(raw_ltm_toc_tail, list):
            state.memory._ltm_toc_tail = [dict(item) for item in raw_ltm_toc_tail if isinstance(item, dict)]
        bound = payload.get("bound_intent", {})
        state.bound_intent = _migrate_bound_intent(bound, schema_version, state.bound_intent)
        governance = payload.get("governance", {})
        authority = governance.get("operator_authority", governance.get("operator_trust_level", "user"))
        default_profile = GovernanceProfile.for_mode(
            governance.get("mode", "standard"),
            operator_authority=authority,
        )
        state.governance = GovernanceProfile(
            mode=governance.get("mode", default_profile.mode),
            operator_authority=authority,
            confirm_mutations=governance.get("confirm_mutations", default_profile.confirm_mutations),
            allow_scope_expansion=governance.get("allow_scope_expansion", default_profile.allow_scope_expansion),
            rewrite_broad_requests=governance.get("rewrite_broad_requests", default_profile.rewrite_broad_requests),
            allow_compound_actions=governance.get("allow_compound_actions", default_profile.allow_compound_actions),
            hard_denies=list(governance.get("hard_denies", default_profile.hard_denies)),
        )
        state.tick_count = int(payload.get("tick_count", 0))
        state.canon_ledger = CanonLedger.from_list(payload.get("canon_ledger", []))
        state.vitals = InstitutionalVitals(**payload.get("vitals", {})) if "vitals" in payload else VitalsEngine.compute(state.canon_ledger)
        state.permission_mode = _normalize_permission_mode(payload.get("permission_mode"), default="task")
        if "execution_mode" in payload:
            state.execution_mode = _normalize_execution_mode(payload.get("execution_mode"))
        else:
            state.execution_mode = _normalize_execution_mode(payload.get("tool_use_enabled", False))
        state.sandbox_session_id = str(payload.get("sandbox_session_id", "default"))
        state.initial_budget = _coerce_float(payload.get("initial_budget"), 0.0)
        state.remaining_budget = _coerce_float(payload.get("remaining_budget"), state.initial_budget)
        state.consumed_budget = _coerce_float(payload.get("consumed_budget"), 0.0)
        state.canary_consecutive_successes = int(payload.get("canary_consecutive_successes", 0) or 0)
        state.canary_validator_open = bool(payload.get("canary_validator_open", False))
        raw_canary_metrics = payload.get("canary_last_run_metrics", {})
        state.canary_last_run_metrics = dict(raw_canary_metrics) if isinstance(raw_canary_metrics, dict) else {}
        # pending_binding_queue / pending_binding_confirmation were removed in schema v5 — silently ignored.
        state.killswitch_engaged = bool(payload.get("killswitch_engaged", False))
        state.debug_mode = bool(payload.get("debug_mode", False))
        raw_reason = payload.get("killswitch_reason", "")
        state.killswitch_reason = str(raw_reason) if raw_reason is not None else ""
        raw_ks_at = payload.get("killswitch_at")
        state.killswitch_at = (
            float(raw_ks_at)
            if isinstance(raw_ks_at, (int, float)) and not isinstance(raw_ks_at, bool)
            else None
        )
        state.tick_mode = payload.get("tick_mode", "off")
        state.execution_state = payload.get("execution_state", "idle")
        state.health_metrics = dict(payload.get("health_metrics", {
            "rejection_count": 0,
            "recovery_count": 0,
            "total_retries": 0
        }))
        state.last_tick_reason = payload.get("last_tick_reason")
        state.tick_blocked_reason = payload.get("tick_blocked_reason")
        state.last_success_timestamp = payload.get("last_success_timestamp")
        state.last_failure_timestamp = payload.get("last_failure_timestamp")
        state.auto_resume_on_restart = bool(payload.get("auto_resume_on_restart", False))
        raw_pending = payload.get("pending_tool_confirmation")
        state.pending_tool_confirmation = dict(raw_pending) if isinstance(raw_pending, dict) else None
        raw_lat = payload.get("last_activity_at")
        state.last_activity_at = (
            float(raw_lat)
            if isinstance(raw_lat, (int, float)) and not isinstance(raw_lat, bool)
            else None
        )
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dumps_json(self.to_persisted_dict(), indent=2)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        backup_path = path.with_suffix(path.suffix + ".bak")

        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        had_existing = path.exists()
        if had_existing:
            with suppress(OSError):
                path.replace(backup_path)

        try:
            temp_path.replace(path)
        except Exception:
            # Best-effort restore when replace fails after existing state was moved aside.
            if had_existing and backup_path.exists() and not path.exists():
                with suppress(OSError):
                    backup_path.replace(path)
            raise

        # Best-effort directory sync to reduce rename-loss on abrupt shutdown.
        with suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    @classmethod
    def load(cls, path: Path) -> "RuntimeState":
        if not path.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            backup_path = path.with_suffix(path.suffix + ".bak")
            if backup_path.exists():
                try:
                    return cls.from_dict(json.loads(backup_path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    pass
            return cls()
