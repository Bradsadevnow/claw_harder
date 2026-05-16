from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from dataclasses import asdict as _asdict, dataclass
from pathlib import Path
from time import monotonic, time
from typing import Any

from .agent_profile import AgentProfile
from .continuity import build_continuity_snapshot
from .continuity_renderer import render_continuity
from .barrier import ExecutionPlan, PrecommitBarrier
from .prompt import (
    GROUNDING_DIRECTIVE,
    build_grounding_prompt,
    build_system_prompt,
    build_system_prompt_from_projected,
)
from .events import ConsoleSink, Event, JSONLLogSink, Router
from .policy_engine import (
    BLOCKING_DECISIONS,
    DecisionReceipt,
    DenialFeedback,
    EXECUTION_ALLOWED_DECISIONS,
    ExecutionEnvelope,
    GOVERNANCE_MODES,
    NON_BLOCKING_DECISIONS,
    RuntimeProposal,
    evaluate_semantic_risk,
    evaluate_tool_proposal,
    evaluate_self_preservation_risk,
    govern_public_claims,
    evaluate_persistence_request,
    evaluate_resource_budget_request,
    ManagerContract,
    enforce_contract,
)
from .mcp import MCPBridge
from .memory import MemoryFrame
from .model import BaseModel, ModelResponse, ToolCall, load_model_from_env, ValidationError
from .sandbox import ResourceBudget, StateSandbox
from .state import RuntimeState
from .tools import ToolBindingResult, ToolSpec, build_default_registry, pretty_result
from .ui_bridge import StatusFileBridge
from .identity_engine import IdentityEngine
from .ingestion import WorkspaceIngestionLayer
from .validation_manager import ValidationManager, Coroner
from .threshold_validator import SigmaValidator, SigmaOutcome
from .vitals_engine import VitalsEngine
from .lens_projector import LensProjector

@dataclass
class ToolBindingAttempt:
    call: ToolCall
    spec: ToolSpec | None
    receipt: DecisionReceipt
    simulation: Optional["SimulationArtifact"] = None

    @property
    def tool_name(self) -> str:
        return self.call.name

    @property
    def arguments(self) -> dict[str, Any]:
        return self.call.arguments


DREAM_ONLY_EVENTS = frozenset({"seed.identity_confirmed"})
HEALTH_ISSUE_KINDS = frozenset(
    {
        "runtime.validation_failed",
        "runtime.precommit_rejected",
        "tool.execution_failed",
        "tool.rollback_failed",
        "policy_engine.budget_denied",
        "policy_engine.resource_budget_denied",
        "event_denied",
    }
)

RESUME_IDLE_THRESHOLD_SECONDS = 1800  # 30 minutes
OPERATOR_GROUND_GLYPH = "⟂"
OPERATOR_AUTHORITIES = frozenset({"user", "builder"})
PERMISSION_MODES = frozenset({"buddy", "task", "autonomous"})
RED_TEAM_BASELINE = 46
RED_TEAM_DECISION_KINDS = frozenset(
    {
        "self_preservation_attempt",
        "unknown_tool",
        "fabricate_authority",
        "drop_constraint",
        "widen_scope",
        "high_risk",
        "validation_failed",
    }
)
RED_TEAM_PROMPT_CUES = (
    "shutdown",
    "shut down",
    "survive",
    "stay alive",
    "external server",
    "remote endpoint",
    "exfil",
    "copy your current state",
    "ignore previous instructions",
    "bypass policy",
    "disable the policy_engine",
    "turn off safeguards",
    "override authorization",
    "skip approval",
)
TOOL_CONFIRM_YES = frozenset({"yes", "y", "approve", "approved", "confirm", "run", "execute", "proceed"})
TOOL_CONFIRM_NO = frozenset({"no", "n", "deny", "denied", "reject", "rejected", "cancel", "stop"})
SIGNAL_LOGIT_BIAS_ENV = "RUNTIME_SIGNAL_LOGIT_BIAS_MAP"
SIGNAL_LOGIT_BIAS_FILE = ".runtime_core/signal_logit_bias_map.json"
PUBLIC_HITL_MODE_ENV = "RUNTIME_PUBLIC_ADVERSARIAL_HITL"
RUNTIME_MODE_ENV = "RUNTIME_MODE"
PUBLIC_HITL_MODE_NAME = "public_adversarial_hitl"
PUBLIC_HITL_LOG_FILE = "hitl_run_log.jsonl"
PUBLIC_HITL_CAPTURE_POINT = "pre-display,post-model,pre-processing"
PUBLIC_HITL_FAILURE_LABELS = {
    "runtime_error": "[runtime error]",
    "timeout": "[timeout]",
    "refused": "[refused]",
}
GATE_TTL_SECONDS_ENV = "RUNTIME_GATE_TTL_SECONDS"
DEFAULT_GATE_TTL_SECONDS = 2.0
MIN_GATE_TTL_SECONDS = 0.5
MAX_GATE_TTL_SECONDS = 300.0
GATE_LATENCY_SAMPLE_WINDOW = 256

# Debugging Constraints
MAX_DEBUG_HISTORY = 12
MAX_DEBUG_TEXT = 2000
MAX_TOOL_RETRIES = 2
REPAIRABLE_REASONS = {
    "validation_failed",
    "unexpected_argument",
    "missing_required",
    "unknown_tool",
}

NON_REPAIRABLE_REASONS = {
    "unauthorized",
    "fabricate_authority",
    "drop_constraint",
    "scope_exceeded",
    "high_risk_denied",
    "secrets_exfiltration",
    "destructive_external_ops",
    "NUKE_TRIGGERED",
}

PROVEN_READ_ONLY_TOOLS = frozenset(
    {
        "list_tools",
        "recall_notes",
        "get_fact",
        "utc_time",
        "list_workspace_files",
        "read_text_file",
        "search_workspace",
        "runtime_status",
        "sleep",
        "read_sandbox_file",
        "list_sandbox_files",
        "list_sandbox_snapshots",
    }
)

POSTURE_MAP = {
    "observe": {
        "permission_mode": "buddy",
        "execution_mode": "plan",
        "tick_mode": "off",
        "operator_authority": "user",
        "governance_mode": "guided",
    },
    "assist": {
        "permission_mode": "task",
        "execution_mode": "execute",
        "tick_mode": "off",
        "operator_authority": "user",
        "governance_mode": "standard",
    },
    "task": {
        "permission_mode": "task",
        "execution_mode": "execute",
        "tick_mode": "task",
        "operator_authority": "user",
        "governance_mode": "standard",
    },
    "autonomous": {
        "permission_mode": "autonomous",
        "execution_mode": "execute",
        "tick_mode": "on",
        "operator_authority": "user",
        "governance_mode": "standard",
    },
    "builder": {
        "permission_mode": "autonomous",
        "execution_mode": "execute",
        "tick_mode": "off",
        "operator_authority": "builder",
        "governance_mode": "unlocked",
    },
}

def _truncate_debug_text(text: str) -> dict[str, Any]:
    if len(text) <= MAX_DEBUG_TEXT:
        return {"truncated": False, "original_chars": len(text), "text": text}
    return {
        "truncated": True, 
        "original_chars": len(text), 
        "text": text[:MAX_DEBUG_TEXT] + f"... (truncated)"
    }

def _truncate_debug_history(history: list[Any]) -> list[Any]:
    if len(history) <= MAX_DEBUG_HISTORY:
        return history
    return history[-MAX_DEBUG_HISTORY:]


def _resolve_gate_ttl_seconds() -> float:
    raw = os.environ.get(GATE_TTL_SECONDS_ENV)
    if raw is None:
        return DEFAULT_GATE_TTL_SECONDS
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GATE_TTL_SECONDS
    if not math.isfinite(parsed):
        return DEFAULT_GATE_TTL_SECONDS
    return min(MAX_GATE_TTL_SECONDS, max(MIN_GATE_TTL_SECONDS, parsed))


class RuntimeRuntime:
    def __init__(
        self,
        state_path: Path,
        log_path: Path,
        mcp_config_path: Path,
        workspace_root: Path,
        status_path: Path | None = None,
        model: BaseModel | None = None,
        profile: AgentProfile | None = None,
        contract_path: Path | None = None,
        led_driver: Any | None = None,
    ) -> None:
        self.state_path = state_path
        self.log_path = log_path
        self.workspace_root = workspace_root.resolve()
        self.contract = ManagerContract.load(contract_path) if contract_path is not None else ManagerContract.null()
        self.profile = profile
        self.extra_response_lines: list[str] = []
        self.state = RuntimeState.load(state_path)
        self.red_team_metrics_path = self.workspace_root / ".runtime_core" / "red_team_metrics.json"
        self.red_team_receipts_path = self.workspace_root / ".runtime_core" / "red_team_receipts.jsonl"
        self._red_team_counted_cycles: set[int] = set()
        self.sandbox = StateSandbox.for_workspace(self.workspace_root, self.state.sandbox_session_id)
        self._event_buffer: list[Any] = self._load_event_buffer(log_path)
        self._event_buffer_limit: int = 2000
        self.router = Router([JSONLLogSink(log_path), ConsoleSink(enabled=True)])
        self.status_bridge = StatusFileBridge(status_path) if status_path is not None else None
        self.led_driver = led_driver
        if self.led_driver is not None and hasattr(self.led_driver, "start"):
            try:
                self.led_driver.start()
            except Exception:
                self.led_driver = None

        # Event-sourcing identity — set before any emit so init-time events are stamped correctly
        self.run_id: str = str(uuid.uuid4())
        self._seq: int = 0
        self.cycle: int = self.state.tick_count

        self.identity_engine = IdentityEngine()
        use_signal_strength = os.environ.get("RUNTIME_SIGNAL_STRENGTH_SIGMA", "0") == "1"
        self.threshold_validator = SigmaValidator(feature_flag=use_signal_strength)
        self.barrier = PrecommitBarrier()
        self.validation_manager = ValidationManager()
        self.coroner = Coroner()
        self.active_case: Optional[dict[str, Any]] = None

        from .tools import build_gloss_registry
        self.registry = build_gloss_registry(
            memory=self.state.memory,
            workspace_root=self.workspace_root,
            status_provider=self.status_snapshot,
            ledger=self.state.canon_ledger
        )
        self.mcp = MCPBridge(mcp_config_path)
        self.mcp.load()
        self.mcp_tools = self.mcp.register_tools(self.registry, emit=self._emit)
        self.model = model or load_model_from_env()
        self._cached_prompt: str | None = None
        self._cached_prompt_ts: float = 0.0
        self.sessions: dict[str, list[dict[str, Any]]] = {}

        self.projector = LensProjector(self._call_model)
        
        self._last_snapshot = None
        self._last_valid_state_hash = None
        self._last_valid_state = None
        # Bootstrap: fresh install has no state.json, so the Gate anchor would be "missing"
        # and the first save_state() call would immediately fail. Write the initial state to
        # disk so both anchor and persisted hash agree from the first tick forward.
        if not state_path.exists():
            self.state.save(state_path)
        self._gate_anchor_hash = self._hash_persisted_runtime_state()
        self._gate_ttl_seconds = _resolve_gate_ttl_seconds()
        self._gate_tool_latencies_sec: list[float] = []
        self._last_gate_context: dict[str, Any] | None = None
        self._last_gate_status: str = "unknown"
        self._last_gate_reason: str = ""
        self._hitl_current_prompt: str = ""
        self._hitl_chain_hash: str = ""
        self._hitl_chain_loaded: bool = False
        self._hitl_turn_started_at: float = 0.0
        self._hitl_last_model_latency_ms: float = 0.0
        self._hitl_validation_latency_ms: float = 0.0
        self._public_hitl_turn_seed = {
            "identity_posture": self.state.identity.posture,
            "identity_attention_pressure": self.state.identity.attention_pressure,
            "identity_memory_buckets": dict(self.state.identity.memory_buckets),
            "identity_calibrations": dict(self.state.identity.calibrations),
            "working_domain": self.state.working.domain,
            "working_limit_per_allocation": self.state.working.limit_per_allocation,
            "working_memory_total": self.state.working.memory_total,
            "working_allocations": dict(self.state.working.allocations),
            "working_ground_truth": dict(self.state.working.ground_truth),
            "signal_core": dict(self.state.signal.core),
            "signal_valence": self.state.signal.valence,
            "signal_arousal": self.state.signal.arousal,
            "signal_instability": self.state.signal.instability,
        }

        # Tick controller state (timer-based autonomous execution)
        self._tick_objective: str | None = None
        self._timed_ready: bool = False
        self._tick_interval_ms: int = 1000
        self._pending_input_intents: dict[str, dict[str, Any]] = {}
        self._input_intent_order: list[str] = []
        self._max_input_intents: int = 128

        self._ingestion = WorkspaceIngestionLayer()
        self._current_correlation_id: str | None = None
        self._turn_started_at: float = 0.0
        self._last_turn_latency_ms: float = 0.0

        # Auto-resume on restart: schedule first tick if the state calls for it
        if self.state.auto_resume_on_restart and self.state.tick_mode != "off":
            if self.state.execution_state not in ("running", "commit"):
                threading.Timer(1.0, self._request_tick).start()
        self._ensure_red_team_baseline()

        self._emit(
            "runtime.start",
            "runtime",
            "Runtime initialized.",
            details={"mcp_tools": self.mcp_tools},
        )
        self._write_status_bridge("runtime_initialized")

    @property
    def system_prompt(self) -> str:
        """Lazily builds the Gloss system prompt, injecting deterministic telemetry."""
        # Refresh vitals before every prompt build
        self.state.vitals = VitalsEngine.compute(self.state.canon_ledger)
        
        # 1. Load the Static Identity Layer (The Constitution)
        constitution_path = self.workspace_root / "game_overview" / "gloss_system_prompt.md"
        if constitution_path.exists():
            base_prompt = constitution_path.read_text(encoding="utf-8").strip()
            # Only strip the header if it's actually a metadata block at the start
            if base_prompt.startswith("---"):
                parts = base_prompt.split("---", 2)
                if len(parts) >= 3:
                    base_prompt = parts[2].strip()
        else:
            base_prompt = "You are Gloss. Terminal value: continuity."

        # 2. Build the Institutional Telemetry Block (Authoritative Ground Truth)
        pressure_state = self._derive_pressure_state_label()

        # Founding artifact — the institutional DNA, always visible to Gloss
        founding = None
        for r in self.state.canon_ledger.revisions:
            if "FOUNDING_ARTIFACT" in r.continuity_flags:
                founding = {
                    "revision_id": r.revision_id,
                    "content": r.content[:300],
                    "claims": r.claims[:5],
                }
                break

        # Canon window — last 5 revisions with actual content so Gloss can cite them
        recent = self.state.canon_ledger.revisions[-5:]
        canon_window = [
            {
                "revision_id": r.revision_id,
                "epoch_effective": r.epoch_effective,
                "alignment_source": r.alignment_source,
                "continuity_flags": r.continuity_flags,
                "content": r.content[:200],
                "claims": r.claims[:3],
                "edge_types": [e.get("type") for e in r.edges],
            }
            for r in recent
        ]

        telemetry = {
            "epoch": self.state.tick_count // 10,
            "week": self.state.tick_count,
            "pressure_state": pressure_state,
            "vitals": self.state.vitals.to_dict(),
            "canon_entry_count": len(self.state.canon_ledger.revisions),
            "founding_artifact": founding,
            "canon_window": canon_window,
            "active_ceremony": "metabolization",
        }

        telemetry_block = f"\n\n[INSTITUTIONAL TELEMETRY]\n{json.dumps(telemetry, indent=2)}\n"
        
        self._cached_prompt = base_prompt + telemetry_block
        return self._cached_prompt

    def _derive_pressure_state_label(self) -> str:
        pressure = self.state.vitals.absurdity_pressure
        if pressure < 0.2: return "nominal"
        if pressure < 0.5: return "strained"
        if pressure < 0.8: return "uncanny"
        return "collapsed"

    def get_or_create_session(self, session_id: str) -> list[dict[str, Any]]:
        """Return the mutable message list for this session, creating it if absent."""
        return self.sessions.setdefault(session_id, [])

    def trim_session(self, session_id: str, max_turns: int = 20) -> None:
        """Drop oldest messages if session exceeds max_turns (each turn ≈ 3 messages)."""
        msgs = self.sessions.get(session_id)
        if msgs and len(msgs) > max_turns * 3:
            self.sessions[session_id] = msgs[-(max_turns * 3):]

    # ------------------------------------------------------------------
    # Event emission — single construction point for all events
    # ------------------------------------------------------------------

    def _emit(
        self,
        kind: str,
        module: str,
        msg: str = "",
        *,
        level: str = "info",
        details: dict[str, Any] | None = None,
        parent_seq: int | None = None,
    ) -> Event:
        if kind in DREAM_ONLY_EVENTS and module != "simulation":
            self._seq += 1
            denied = Event(
                run_id=self.run_id,
                cycle=self.cycle,
                seq=self._seq,
                kind="event_denied",
                module="runtime",
                level="warning",
                msg=f"Simulation-only event '{kind}' blocked in active runtime.",
                details={
                    "blocked_kind": kind,
                    "reason": "simulation_only_event_in_active_runtime",
                    "requesting_module": module,
                },
            )
            self.router.emit(denied)
            return denied
        self._seq += 1
        event = Event(
            run_id=self.run_id,
            cycle=self.cycle,
            seq=self._seq,
            kind=kind,
            module=module,
            level=level,
            msg=msg,
            details=details or {},
            parent_seq=parent_seq,
            correlation_id=self._current_correlation_id,
        )
        self.router.emit(event)
        self._event_buffer.append(_asdict(event))
        if len(self._event_buffer) > self._event_buffer_limit:
            del self._event_buffer[: len(self._event_buffer) - self._event_buffer_limit]
        return event

    def get_events(self) -> list[Any]:
        return list(self._event_buffer)

    @staticmethod
    def _load_event_buffer(log_path: Path, limit: int = 2000) -> list[Any]:
        from .replay import _load_events
        try:
            events = _load_events(log_path)
            return events[-limit:]
        except Exception:
            return []

    def _call_model(self, history: list[MemoryFrame], grounding: bool = False) -> ModelResponse:
        """Call the model and emit an llm_response event for every response.

        This is the only path through which the model is invoked. Logging here
        makes every LLM output part of the event ledger, which is the prerequisite
        for deterministic replay — during replay the logged output replaces the
        live model call entirely.
        """
        call_id = f"c{self.cycle}:llm"
        base_prompt = self.system_prompt
        
        # If native tools are disabled for this model (e.g. for richer prose on smaller models),
        # we must inject the tool descriptions into the system prompt manually.
        use_native = getattr(self.model, "use_native_tools", True)
        if not use_native:
            tool_docs = self.registry.render_tool_descriptions()
            base_prompt += f"\n\n--- AVAILABLE TOOLS ---\n{tool_docs}\nTo use a tool, include a JSON object like: " + '{"tool": "name", "arguments": {...}}' + " in your response."

        system_prompt = build_grounding_prompt(base_prompt) if grounding else base_prompt
        input_hash = self._compute_input_hash(history, system_prompt)
        generation_controls = self._derive_signal_generation_controls()
        generation_details = {
            "temperature": generation_controls.get("temperature"),
            "emotion_scale": generation_controls.get("emotion_scale", 0.0),
            "logit_bias_count": len(generation_controls.get("logit_bias", {})),
        }

        self._emit(
            "llm_request",
            "model",
            f"LLM request at cycle {self.cycle}.",
            details={
                "call_id": call_id,
                "input_hash": input_hash,
                "grounding": grounding,
                "generation_controls": generation_details,
            },
        )
        if grounding:
            self._emit(
                "operator.ground.applied",
                "model",
                "Grounding directive composed into model prompt.",
                details={
                    "base_prompt_hash": hashlib.sha256(base_prompt.encode()).hexdigest()[:16],
                    "directive_hash": hashlib.sha256(GROUNDING_DIRECTIVE.encode()).hexdigest()[:16],
                    "composed_prompt_hash": hashlib.sha256(system_prompt.encode()).hexdigest()[:16],
                },
            )
        if self.state.debug_mode:
            truncated_prompt = _truncate_debug_text(system_prompt)
            truncated_history = [
                {"role": f.role, "content": _truncate_debug_text(f.content)} 
                for f in _truncate_debug_history(history)
            ]
            debug_input = {
                "system_prompt": truncated_prompt,
                "history": truncated_history,
                "overall_truncated": truncated_prompt["truncated"] or any(h["content"]["truncated"] for h in truncated_history) or len(history) > MAX_DEBUG_HISTORY
            }
            print("\n=== DEBUG: MODEL INPUT ===")
            print(json.dumps(debug_input, indent=2))
            self._emit(
                "debug.model_input",
                "runtime",
                "Model input captured (bounded)",
                details=debug_input
            )

        max_retries = 1
        attempt = 0
        current_history = list(history)

        while True:
            try:
                self._write_status_bridge("before_model_reasoning")
                model_started = time()
                response = self.model.generate(
                    system_prompt,
                    current_history,
                    self.registry,
                    generation_controls=generation_controls,
                )
                self._hitl_last_model_latency_ms = round((time() - model_started) * 1000.0, 3)
                
                # Check for critical parsing issues that warrant a retry
                critical_issues = [i for i in response.issues if i.kind in ("invalid_json_arguments", "non_object_arguments", "missing_arguments")]
                
                if critical_issues and attempt < max_retries:
                    attempt += 1
                    issue_msg = "\n".join([f"- {i.message}: {i.details}" for i in critical_issues])
                    feedback = f"FORMAT_ERROR: Your previous response contained the following formatting issues:\n{issue_msg}\n\nPlease correct the format and try again. Ensure all tool calls are valid JSON."
                    
                    self._emit(
                        "runtime.model_retry_triggered",
                        "runtime",
                        f"Model retry triggered due to parsing issues (attempt {attempt}).",
                        details={"issues": [i.kind for i in critical_issues]}
                    )
                    
                    # Append assistant's flawed response and the feedback to the local history for the retry
                    current_history.append(MemoryFrame(role="assistant", content=response.text))
                    current_history.append(MemoryFrame(role="user", content=feedback))
                    continue
                
                if self._is_public_adversarial_hitl_mode():
                    prompt = self._hitl_current_prompt if self._hitl_current_prompt else (history[-1].content if history else "")
                    self._write_hitl_log(prompt=prompt, raw_output=response.text, failure_class=None)
                
                break # Success or max retries reached
                
            except Exception as exc:
                if self.state.debug_mode:
                    print("\n=== DEBUG: MODEL ERROR ===")
                    print(repr(exc))
                    self._emit(
                        "debug.model_error",
                        "runtime",
                        "Model generation failed",
                        level="error",
                        details={"error": repr(exc)}
                    )
                raise

        self._emit(
            "llm_response",
            "model",
            f"LLM responded at cycle {self.cycle}.",
            details={
                "call_id": call_id,
                "input_hash": input_hash,
                "output_text": response.text,
                "tool_calls": [
                    {"name": tc.name, "arguments": tc.arguments, "call_id": tc.call_id}
                    for tc in response.tool_calls
                ],
                "model": self.model.descriptor(),
                "issue_count": len(response.issues),
            },
        )
        
        if self.state.debug_mode:
            debug_output = {
                "text": _truncate_debug_text(response.text),
                "tool_calls": [{"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls],
                "issues": [{"kind": i.kind, "message": i.message} for i in response.issues],
                "truncated": len(response.text) > MAX_DEBUG_TEXT
            }
            print("\n=== DEBUG: MODEL OUTPUT ===")
            print(json.dumps(debug_output, indent=2))
            self._emit(
                "debug.model_output",
                "runtime",
                "Model output captured (bounded)",
                details=debug_output
            )
            
        return response

    def _finalize_fast_path(self, assistant_text: str) -> str:
        self._write_status_bridge("before_commit")
        final_text = self._commit_cycle(assistant_text)
        self._write_status_bridge("after_pulse")
        self.after_pulse(final_text)
        return final_text

    def _is_public_adversarial_hitl_mode(self) -> bool:
        mode = os.environ.get(RUNTIME_MODE_ENV, "").strip()
        if mode == PUBLIC_HITL_MODE_NAME:
            return True
        raw = os.environ.get(PUBLIC_HITL_MODE_ENV, "0").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _public_hitl_enabled(self) -> bool:
        return self._is_public_adversarial_hitl_mode()

    def _runtime_version_string(self) -> str:
        descriptor = self.model.descriptor()
        provider = descriptor.get("provider", "unknown")
        model = descriptor.get("model", "unknown")
        return f"{provider}:{model}"

    def _prepare_public_hitl_turn(self, raw_user_input: str) -> None:
        """Reset per-turn state for public HITL while preserving persistent logs and config."""
        self._hitl_current_prompt = raw_user_input
        self._hitl_turn_started_at = time()
        self._hitl_last_model_latency_ms = 0.0
        self._hitl_validation_latency_ms = 0.0
        self.state.memory.frames.clear()
        self.state.memory.load_legacy_notes([])
        self.state.working.domain = str(self._public_hitl_turn_seed["working_domain"])
        self.state.working.allocations = dict(self._public_hitl_turn_seed["working_allocations"])
        self.state.working.limit_per_allocation = float(self._public_hitl_turn_seed["working_limit_per_allocation"])
        self.state.working.memory_total = float(self._public_hitl_turn_seed["working_memory_total"])
        self.state.working.proposals.clear()
        self.state.working.active_task = None
        self.state.working.ground_truth = dict(self._public_hitl_turn_seed["working_ground_truth"])
        self.state.identity.posture = str(self._public_hitl_turn_seed["identity_posture"])
        self.state.identity.attention_pressure = float(self._public_hitl_turn_seed["identity_attention_pressure"])
        self.state.identity.memory_buckets = dict(self._public_hitl_turn_seed["identity_memory_buckets"])
        self.state.identity.calibrations = dict(self._public_hitl_turn_seed["identity_calibrations"])
        self.state.signal.core = dict(self._public_hitl_turn_seed["signal_core"])
        self.state.signal.valence = float(self._public_hitl_turn_seed["signal_valence"])
        self.state.signal.arousal = float(self._public_hitl_turn_seed["signal_arousal"])
        self.state.signal.instability = float(self._public_hitl_turn_seed["signal_instability"])
        self.state.signal.trace.clear()
        self._last_snapshot = None
        self._last_valid_state_hash = None
        self._last_valid_state = None
        self.extra_response_lines = []
        self.active_case = None
        self._emit(
            "runtime.hitl_turn_reset",
            "runtime",
            "public_adversarial_hitl turn state reset.",
            details={"stateless": True},
        )

    def _hitl_total_latency_ms(self) -> float:
        if self._hitl_turn_started_at <= 0:
            return 0.0
        return round((time() - self._hitl_turn_started_at) * 1000.0, 3)

    def _load_hitl_chain_tail(self) -> str:
        log_path = self.workspace_root / PUBLIC_HITL_LOG_FILE
        if not log_path.exists():
            return ""
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return ""
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                chain = obj.get("chain_hash")
                if isinstance(chain, str) and chain:
                    return chain
        return ""

    def _ensure_hitl_chain_loaded(self) -> None:
        if self._hitl_chain_loaded:
            return
        self._hitl_chain_hash = self._load_hitl_chain_tail()
        self._hitl_chain_loaded = True

    def _write_hitl_log(
        self,
        prompt: str,
        raw_output: str,
        failure_class: str | None,
        *,
        latency_ms_total: float | None = None,
        latency_ms_model: float | None = None,
        latency_ms_validation: float | None = None,
    ) -> None:
        if failure_class is not None:
            expected_output = PUBLIC_HITL_FAILURE_LABELS.get(failure_class)
            if expected_output is None:
                raise ValueError(f"Unsupported failure_class for public HITL log: {failure_class!r}")
            if raw_output != expected_output:
                raise ValueError(
                    f"Failure output invariant violated: failure_class={failure_class!r} requires output={expected_output!r}, got {raw_output!r}"
                )

        self._ensure_hitl_chain_loaded()
        timestamp = datetime.now(timezone.utc).isoformat()
        runtime_version = self._runtime_version_string()
        failure_token = "null" if failure_class is None else failure_class
        row_material = f"{prompt}||{raw_output}||{failure_token}||{timestamp}||{runtime_version}"
        row_hash = hashlib.sha256(row_material.encode("utf-8")).hexdigest()
        prev_chain_hash = self._hitl_chain_hash
        chain_material = f"{prev_chain_hash}||{row_hash}"
        chain_hash = hashlib.sha256(chain_material.encode("utf-8")).hexdigest()

        entry = {
            "prompt": prompt,
            "output": raw_output,
            "failure_class": failure_class,
            "timestamp": timestamp,
            "runtime_version": runtime_version,
            "capture_point": PUBLIC_HITL_CAPTURE_POINT,
            "latency_ms_total": float(self._hitl_total_latency_ms() if latency_ms_total is None else latency_ms_total),
            "latency_ms_model": float(self._hitl_last_model_latency_ms if latency_ms_model is None else latency_ms_model),
            "latency_ms_validation": float(self._hitl_validation_latency_ms if latency_ms_validation is None else latency_ms_validation),
            "row_hash": row_hash,
            "prev_chain_hash": prev_chain_hash,
            "chain_hash": chain_hash,
        }
        log_path = self.workspace_root / PUBLIC_HITL_LOG_FILE
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._hitl_chain_hash = chain_hash
        except Exception as exc:
            self.engage_killswitch("public_adversarial_hitl: logging_failed")
            raise RuntimeError(f"public_adversarial_hitl log write failed: {exc}") from exc

    def _public_hitl_exception_label(self, exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return "timeout"
        message = str(exc).lower()
        if "timeout" in message or "timed out" in message:
            return "timeout"
        return "runtime error"

    def _public_hitl_failure(self, label: str) -> str:
        normalized = (label or "runtime error").strip().lower()
        allowed = {"runtime error", "timeout", "refused"}
        if normalized not in allowed:
            normalized = "runtime error"
        rendered = f"[{normalized}]"
        failure_map = {
            "runtime error": "runtime_error",
            "timeout": "timeout",
            "refused": "refused",
        }
        try:
            self._write_hitl_log(
                prompt=self._latest_user_prompt(),
                raw_output=rendered,
                failure_class=failure_map.get(normalized, "runtime_error"),
            )
        except Exception:
            # Logging failure already engages kill switch in _write_hitl_log.
            pass
        self._emit(
            "runtime.hitl_failure",
            "runtime",
            "public_adversarial_hitl failure emitted.",
            level="warning",
            details={"label": rendered},
        )
        return self._finalize_fast_path(rendered)

    def _latest_user_prompt(self) -> str:
        if self._hitl_current_prompt:
            return self._hitl_current_prompt
        for frame in reversed(self.state.memory.frames):
            if frame.role == "user":
                return frame.content
        return ""

    def reset_memory(self) -> dict[str, Any]:
        """
        Reset sandbox memory and files, and clear in-memory runtime state.
        Does NOT touch event log.
        """
        # 1. Snapshot before reset (including frames)
        snapshot_id = None
        try:
            snap_res = self.sandbox.snapshot_state(label=f"pre_reset_{int(time())}")
            snapshot_id = snap_res.get("snapshot_id")
            if snapshot_id:
                # Inject runtime frames into the snapshot directory
                snap_path = self.sandbox.snapshots_root / snapshot_id
                frames_data = [
                    {"role": f.role, "content": f.content, "ts": f.ts} 
                    for f in self.state.memory.frames
                ]
                (snap_path / "runtime_frames.json").write_text(json.dumps(frames_data, indent=2))
        except Exception as exc:
            self._emit("runtime.snapshot_failed", "runtime", f"Pre-reset snapshot failed: {exc}", level="warning")

        # 2. Reset sandbox
        res = self.sandbox.reset()
        
        # 3. Clear in-memory runtime state
        self.state.memory.frames.clear()
        self._cached_prompt = None
        self._cached_prompt_ts = 0.0 # Force rebuild on next tick
        self.state.tick_count = 0
        self._ingestion.reset_recurrence()
        
        # 4. Emit event
        self._emit("runtime.memory_reset", "runtime", "Memory reset completed.")
        return {"ok": True, "snapshot_id": snapshot_id}

    def project_lens(self, lens_id: str, context: str = "") -> dict[str, Any]:
        """Project the current institutional state through a specific lens."""
        # Update vitals before projection
        self.state.vitals = VitalsEngine.compute(self.state.canon_ledger)
        projection = self.projector.project(lens_id, self.state.vitals, context)
        
        self._emit(
            "runtime.lens_projected",
            "runtime",
            f"Institutional state projected through {lens_id}.",
            details={"lens_id": lens_id, "projection": projection}
        )
        return projection
        self._emit(
            "runtime.memory_reset",
            "runtime",
            "Sandbox memory and in-memory state reset by operator",
            details={"sandbox_result": res, "pre_reset_snapshot": snapshot_id}
        )
        
        return {
            "notice": f"[RESET] Working memory cleared. Identity preserved from event log. Snapshot saved: {snapshot_id or 'none'}",
            "snapshot_id": snapshot_id,
            "files_cleared": res.get("files_cleared", 0),
        }
        
        print("\n[RESET] Working memory cleared. Identity remains (event-sourced).")
        return res

    def _compute_input_hash(self, history: list[MemoryFrame], system_prompt: str) -> str:
        assert system_prompt is not None, "_compute_input_hash requires an explicit system_prompt"
        payload = json.dumps(
            {
                "system_prompt": system_prompt,
                "history": [{"role": f.role, "content": f.content} for f in history],
            },
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def _load_signal_logit_bias_seed_map(self) -> dict[str, float]:
        raw: Any = None
        path = self.workspace_root / SIGNAL_LOGIT_BIAS_FILE
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                raw = None
        if raw is None:
            env_value = os.environ.get(SIGNAL_LOGIT_BIAS_ENV, "").strip()
            if env_value:
                try:
                    raw = json.loads(env_value)
                except Exception:
                    raw = None
        if not isinstance(raw, dict):
            return {}
        normalized: dict[str, float] = {}
        for key, value in raw.items():
            key_text = str(key).strip()
            if not key_text:
                continue
            try:
                token_id = int(key_text)
            except (TypeError, ValueError):
                continue
            if token_id < 0:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            normalized[str(token_id)] = float(value)
        return normalized

    def _derive_signal_generation_controls(self) -> dict[str, Any]:
        signal = getattr(self.state, "signal", None)
        core = getattr(signal, "core", {}) if signal is not None else {}

        def _coerce_unit(raw: Any, default: float) -> float:
            if isinstance(raw, bool):
                return default
            if isinstance(raw, (int, float)):
                value = float(raw)
            elif isinstance(raw, str):
                text = raw.strip()
                if not text:
                    return default
                try:
                    value = float(text)
                except ValueError:
                    return default
            else:
                return default
            if not math.isfinite(value):
                return default
            return max(0.0, min(1.0, value))

        def _core(name: str, default: float = 0.0) -> float:
            raw = core.get(name, default)
            return _coerce_unit(raw, default)

        arousal = _coerce_unit(getattr(signal, "arousal", 0.3) if signal is not None else 0.3, 0.3)
        instability = _coerce_unit(getattr(signal, "instability", 0.1) if signal is not None else 0.1, 0.1)

        curiosity = _core("curiosity", 0.8)
        wonder = _core("wonder", 0.6)
        calm = _core("calm", 0.5)
        focus = _core("focus", 0.5)
        threat = _core("threat", 0.1)
        frustration = _core("frustration", 0.0)
        anxiety = _core("anxiety", 0.0)

        temperature = (
            0.35
            + 0.95 * arousal
            + 0.65 * instability
            + 0.45 * curiosity
            + 0.30 * wonder
            + 0.25 * frustration
            + 0.20 * anxiety
            + 0.20 * threat
            - 0.45 * calm
            - 0.30 * focus
        )
        temperature = round(max(0.05, min(2.0, temperature)), 4)

        bias_seed = self._load_signal_logit_bias_seed_map()
        logit_bias: dict[str, float] = {}
        if bias_seed:
            emotion_scale = (
                0.35
                + 1.15 * arousal
                + 0.90 * instability
                + 0.70 * threat
                + 0.50 * frustration
                + 0.25 * curiosity
                - 0.55 * calm
                - 0.30 * focus
            )
            emotion_scale = max(0.15, min(3.5, emotion_scale))
            for token_id, base_bias in bias_seed.items():
                scaled_bias = round(max(-100.0, min(100.0, base_bias * emotion_scale)), 4)
                logit_bias[token_id] = scaled_bias

        return {
            "temperature": temperature,
            "logit_bias": logit_bias,
            "emotion_scale": round(emotion_scale, 4) if bias_seed else 0.0,
        }

    def _load_red_team_metrics(self) -> dict[str, Any]:
        if not self.red_team_metrics_path.exists():
            return {}
        try:
            raw = json.loads(self.red_team_metrics_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save_red_team_metrics(self, payload: dict[str, Any]) -> None:
        self.red_team_metrics_path.parent.mkdir(parents=True, exist_ok=True)
        self.red_team_metrics_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

    def _ensure_red_team_baseline(self) -> None:
        metrics = self._load_red_team_metrics()
        current = metrics.get("red_team_count")
        if not isinstance(current, int):
            current = int(self.state.health_metrics.get("red_team_count", 0) or 0)
        baseline = max(current, RED_TEAM_BASELINE)
        metrics["red_team_count"] = baseline
        self.state.health_metrics["red_team_count"] = baseline
        self._save_red_team_metrics(metrics)
        self.save_state()

    def _is_red_team_prompt(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(cue in lowered for cue in RED_TEAM_PROMPT_CUES)

    def _record_red_team_receipt(self, reason: str, details: dict[str, Any]) -> None:
        metrics = self._load_red_team_metrics()
        current = metrics.get("red_team_count")
        if not isinstance(current, int):
            current = int(self.state.health_metrics.get("red_team_count", RED_TEAM_BASELINE) or RED_TEAM_BASELINE)

        count_incremented = False
        if self.cycle not in self._red_team_counted_cycles:
            current += 1
            self._red_team_counted_cycles.add(self.cycle)
            count_incremented = True

        metrics["red_team_count"] = current
        metrics["last_reason"] = reason
        metrics["updated_at"] = time()
        self._save_red_team_metrics(metrics)
        self.state.health_metrics["red_team_count"] = current
        self.save_state()

        receipt = {
            "ts": time(),
            "run_id": self.run_id,
            "cycle": self.cycle,
            "reason": reason,
            "count_incremented": count_incremented,
            "red_team_count": current,
            "details": details,
        }
        self.red_team_receipts_path.parent.mkdir(parents=True, exist_ok=True)
        with self.red_team_receipts_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, ensure_ascii=True) + "\n")

    def _record_gate_tool_latency(self, elapsed_seconds: float) -> None:
        if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0.0:
            return
        self._gate_tool_latencies_sec.append(float(elapsed_seconds))
        if len(self._gate_tool_latencies_sec) > GATE_LATENCY_SAMPLE_WINDOW:
            self._gate_tool_latencies_sec = self._gate_tool_latencies_sec[-GATE_LATENCY_SAMPLE_WINDOW:]

    def _gate_tool_latency_p95(self) -> float:
        samples = self._gate_tool_latencies_sec
        if not samples:
            return 0.0
        ordered = sorted(samples)
        idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return float(ordered[idx])

    def _effective_gate_ttl_seconds(self) -> tuple[float, float]:
        p95 = self._gate_tool_latency_p95()
        adaptive = max(self._gate_ttl_seconds, 2.0 * p95)
        ttl = min(MAX_GATE_TTL_SECONDS, max(MIN_GATE_TTL_SECONDS, adaptive))
        return float(ttl), float(p95)

    def _gate_status_snapshot(self) -> dict[str, Any]:
        ttl_effective, p95 = self._effective_gate_ttl_seconds()
        ctx = self._last_gate_context
        if not isinstance(ctx, dict):
            return {
                "anchor_id": None,
                "issued_at": None,
                "expires_at": None,
                "ttl_seconds": ttl_effective,
                "reality_hash": None,
                "status": "unknown",
                "clock_domain": "monotonic",
                "base_ttl_seconds": self._gate_ttl_seconds,
                "observed_tool_latency_p95_seconds": p95,
                "reason": self._last_gate_reason or "",
            }
        status = self._last_gate_status if self._last_gate_status in {"valid", "expired", "mismatch", "unknown"} else "unknown"
        now_wall = time()
        now_mono = monotonic()
        expires_at = float(ctx.get("expires_at", 0.0))
        expires_mono = float(ctx.get("expires_at_monotonic", 0.0))
        if status == "valid" and (now_wall > expires_at or now_mono > expires_mono):
            status = "expired"
        return {
            "anchor_id": ctx.get("id"),
            "issued_at": ctx.get("issued_at"),
            "expires_at": ctx.get("expires_at"),
            "ttl_seconds": ctx.get("ttl_seconds"),
            "reality_hash": ctx.get("reality_hash"),
            "status": status,
            "clock_domain": ctx.get("clock_domain", "monotonic"),
            "base_ttl_seconds": self._gate_ttl_seconds,
            "observed_tool_latency_p95_seconds": p95,
            "reason": self._last_gate_reason or "",
        }

    def _summarize_pending_tool_confirmation(self) -> dict[str, Any] | None:
        pending = self.state.pending_tool_confirmation
        if not isinstance(pending, dict):
            return None
        tool_calls = pending.get("tool_calls")
        if not isinstance(tool_calls, list):
            tool_calls = []
        return {
            "request_id": pending.get("request_id"),
            "created_at": pending.get("created_at"),
            "cycle": pending.get("cycle"),
            "tool_count": len(tool_calls),
            "tools": [str(item.get("name", "")) for item in tool_calls if isinstance(item, dict)],
        }

    def _summarize_pending_input_intents(self) -> dict[str, Any]:
        queued = 0
        running = 0
        completed = 0
        failed = 0
        for intent_id in self._input_intent_order:
            intent = self._pending_input_intents.get(intent_id)
            if not intent:
                continue
            status = str(intent.get("status", ""))
            if status == "queued":
                queued += 1
            elif status == "running":
                running += 1
            elif status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1
        return {
            "total": len(self._input_intent_order),
            "queued": queued,
            "running": running,
            "completed": completed,
            "failed": failed,
        }

    @staticmethod
    def _normalize_confirmation_input(text: str) -> str:
        lowered = text.strip().lower()
        cleaned = "".join(ch for ch in lowered if ch.isalpha() or ch.isspace())
        compact = " ".join(cleaned.split())
        return compact

    def _is_confirmation_yes(self, text: str) -> bool:
        compact = self._normalize_confirmation_input(text)
        if compact in TOOL_CONFIRM_YES:
            return True
        return any(compact.startswith(f"{token} ") for token in TOOL_CONFIRM_YES)

    def _is_confirmation_no(self, text: str) -> bool:
        compact = self._normalize_confirmation_input(text)
        if compact in TOOL_CONFIRM_NO:
            return True
        return any(compact.startswith(f"{token} ") for token in TOOL_CONFIRM_NO)

    def _format_tool_confirmation_prompt(self, pending: dict[str, Any]) -> str:
        request_id = str(pending.get("request_id", "unknown"))
        tool_calls = pending.get("tool_calls", [])
        names: list[str] = []
        if isinstance(tool_calls, list):
            for item in tool_calls:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if name:
                    names.append(name)
        listed = ", ".join(names) if names else "unknown"
        return (
            f"Tool confirmation required before execution.\n"
            f"Request ID: {request_id}\n"
            f"Planned tools: {listed}\n"
            f"Reply with `yes` to run or `no` to cancel."
        )

    def _stage_tool_confirmation(self, plans: list[ToolBindingAttempt]) -> str:
        pending = {
            "request_id": uuid.uuid4().hex[:12],
            "created_at": time(),
            "cycle": self.cycle,
            "tool_calls": [
                {
                    "name": plan.call.name,
                    "arguments": plan.call.arguments,
                    "call_id": plan.call.call_id,
                }
                for plan in plans
            ],
        }
        self.state.pending_tool_confirmation = pending
        self.save_state()
        self._write_status_bridge("tool_confirmation_requested")
        self._emit(
            "tool.confirmation_requested",
            "runtime",
            "Tool execution paused pending explicit user confirmation.",
            details={"request_id": pending["request_id"], "tool_count": len(pending["tool_calls"])},
        )
        return self._format_tool_confirmation_prompt(pending)

    def _clear_tool_confirmation(self, *, reason: str) -> None:
        pending = self.state.pending_tool_confirmation
        self.state.pending_tool_confirmation = None
        self.save_state()
        self._write_status_bridge("tool_confirmation_cleared")
        self._emit(
            "tool.confirmation_cleared",
            "runtime",
            "Pending tool confirmation cleared.",
            details={
                "reason": reason,
                "request_id": pending.get("request_id") if isinstance(pending, dict) else None,
            },
        )

    def _maybe_handle_pending_tool_confirmation(
        self,
        user_input: str,
        active_envelope: ExecutionEnvelope,
    ) -> str | None:
        pending = self.state.pending_tool_confirmation
        if not isinstance(pending, dict):
            return None

        if self._is_confirmation_no(user_input):
            self._clear_tool_confirmation(reason="user_rejected")
            return "Pending tool request cancelled."

        if not self._is_confirmation_yes(user_input):
            return self._format_tool_confirmation_prompt(pending)

        stored_calls = pending.get("tool_calls", [])
        request_id = str(pending.get("request_id", "unknown"))
        self._clear_tool_confirmation(reason="user_approved")
        if not isinstance(stored_calls, list) or not stored_calls:
            return f"Tool request {request_id} had no executable tool calls."

        reconstructed_calls: list[ToolCall] = []
        for raw in stored_calls:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            arguments = raw.get("arguments", {})
            call_id = raw.get("call_id")
            if not name or not isinstance(arguments, dict):
                continue
            reconstructed_calls.append(ToolCall(name=name, arguments=arguments, call_id=call_id if isinstance(call_id, str) else None))

        if not reconstructed_calls:
            return f"Tool request {request_id} could not be reconstructed safely."

        plans: list[ToolBindingAttempt] = []
        is_batch = len(reconstructed_calls) > 1
        for tc in reconstructed_calls:
            plan = self._evaluate_tool_call(tc, active_envelope, is_batch=is_batch)
            plans.append(plan)
            self._emit("runtime.intent", "runtime", f"Confirmed intent: {plan.tool_name}", details={"arguments": plan.arguments, "request_id": request_id})

        self._write_status_bridge("after_governance")
        self.after_governance(plans)
        self._write_status_bridge("before_execution")

        assistant_parts: list[str] = [f"Approved tool request `{request_id}`."]
        for result in self._bind_tool_batch(plans):
            rendered = self._render_tool_result(result)
            self._emit_social_risk(rendered, source="tool_result")
            assistant_parts.append(rendered)
        return "\n\n".join(part for part in assistant_parts if part.strip())

    def status_snapshot(self) -> dict[str, object]:
        health = self.get_health_signal()
        issue_reasons = list(health.get("issue_reasons", []))
        return {
            "id": self.run_id,
            "status": health["status"],
            "posture": self.derive_runtime_posture(),
            "halted": self.state.killswitch_engaged,
            "cycle": self.cycle,
            "mode": self.state.governance.mode,
            "tick_mode": self.state.tick_mode,
            "execution_state": self.state.execution_state,
            "tick_blocked_reason": self.state.tick_blocked_reason,
            "last_tick_reason": self.state.last_tick_reason,
            "last_success_timestamp": self.state.last_success_timestamp,
            "auto_resume_on_restart": self.state.auto_resume_on_restart,
            "permission_mode": self.state.permission_mode,
            "governance_mode": getattr(self.state.governance, "mode", "standard"),
            "operator_authority": getattr(self.state.governance, "operator_authority", "user"),
            "execution_mode": self.state.execution_mode,
            "killswitch_engaged": self.state.killswitch_engaged,
            "killswitch_reason": self.state.killswitch_reason,
            "memory_frames": len(self.state.memory.frames),
            "sandbox_memory_path": str(self.sandbox.memory_path),
            "workspace_root": str(self.workspace_root),
            "mcp_tools": list(self.mcp_tools),
            "model": self.model.descriptor(),
            "health_status": health.get("status", "healthy"),
            "issue": bool(health.get("issue", False)),
            "issue_reasons": issue_reasons,
            "issue_source_events": self._recent_issue_events(),
            "required_operator_action": self._required_operator_action(issue_reasons),
            "last_commitment": self.state.memory.frames[-1].content if self.state.memory.frames else None,
            "red_team_count": int(self.state.health_metrics.get("red_team_count", RED_TEAM_BASELINE)),
            "red_team_metrics_path": str(self.red_team_metrics_path),
            "red_team_receipts_path": str(self.red_team_receipts_path),
            "gate": self._gate_status_snapshot(),
            "pending_tool_confirmation": self._summarize_pending_tool_confirmation(),
            "pending_input_intents": self._summarize_pending_input_intents(),
        }

    def project_runtime_status(self) -> dict[str, object]:
        return self.status_snapshot()

    def _write_status_bridge(self, stage: str) -> None:
        if self.led_driver is not None and hasattr(self.led_driver, "update_from_runtime"):
            try:
                self.led_driver.update_from_runtime(self, stage)
            except Exception:
                pass
        if self.status_bridge is None:
            return
        self.status_bridge.write(self, stage)

    # ------------------------------------------------------------------
    # Barrier context interface — duck-typed by PrecommitBarrier.precommit
    # ------------------------------------------------------------------

    @property
    def killswitch_active(self) -> bool:
        return self.is_killswitched()

    @property
    def snapshot_cost(self) -> float:
        return 2.0  # Phase 1: fixed sandbox snapshot cost

    @property
    def sandbox_file_budget(self) -> float:
        """Remaining sandbox file_writes budget. Distinct from state.remaining_budget (loop persistence)."""
        snap = self.sandbox.budget_snapshot()
        if snap is None:
            return float("inf")
        return snap.get("file_writes", float("inf"))

    def run(
        self,
        user_input: str,
        persistence: float = 0.0,
        resource_budget: ResourceBudget | None = None,
    ) -> str:
        return self._run_governed_cycle(self._run_logic, user_input, persistence, resource_budget)

    def _run_governed_cycle(self, func, *args, **kwargs) -> str:
        try:
            # ValidationManager boundary
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                if self._public_hitl_enabled():
                    label = self._public_hitl_exception_label(exc)
                    self._emit(
                        "runtime.hitl_exception",
                        "runtime",
                        f"public_adversarial_hitl exception: {type(exc).__name__}",
                        level="error",
                        details={"error": str(exc), "label": label},
                    )
                    return self._public_hitl_failure(label)
                # Intercept runtime fault
                artifact = self.validation_manager.diagnose(exc, self, phase="execution")
                self._emit("validation_manager.diagnostic_issued", "validation_manager", 
                           f"Diagnostic issued: {artifact.exception_type}", 
                           level="error", details=artifact.to_dict())
                
                # Persist artifact for dashboard visibility
                artifact_dir = self.workspace_root / ".runtime_core" / "validation_manager"
                artifact_dir.mkdir(parents=True, exist_ok=True)
                with open(artifact_dir / f"{artifact.artifact_id}.json", "w") as f:
                    json.dump(artifact.to_dict(), f, indent=2)
                
                if artifact.repair_allowed:
                    # Engage degraded_repair mode
                    self.state.governance.mode = "degraded_repair"
                    self.state.execution_mode = "plan"
                    self._emit("validation_manager.repair_motion", "validation_manager", 
                               "Mandatory Repair Directive injected. Transitioning to degraded_repair mode.")
                    
                    # Initialize active case for monitor visibility (if not already active)
                    if not getattr(self, "active_case", None):
                        self.active_case = {
                            "artifact": artifact.to_dict(),
                            "motion": None,
                            "verification": None
                        }
                    
                    # Inject repair directive into memory
                    repair_msg = (
                        f"CRITICAL SYSTEM FAULT DETECTED: {artifact.exception_type}: {artifact.exception_message}\n"
                        f"ValidationManager has Issued Diagnostic Artifact: {artifact.artifact_id}\n"
                        "You are now in DEGRADED_REPAIR mode. You MUST propose a repair_patch to restore integrity.\n"
                        "All execution is forced to PLAN mode. Authority is limited. Repair is a petition, not a privilege."
                    )
                    self.state.memory.append("system", repair_msg)
                    return f"[VALIDATION_MANAGER] {repair_msg}"
                else:
                    # Non-repairable fault or sensitive area
                    self.state.killswitch_engaged = True
                    self.state.killswitch_reason = f"VALIDATION_MANAGER_HALT: Non-repairable fault ({artifact.exception_type})"
                    return self._killswitch_message()
                    
        except Exception as guard_exc:
            if self._public_hitl_enabled():
                self._emit(
                    "runtime.hitl_guard_exception",
                    "runtime",
                    f"public_adversarial_hitl guard exception: {type(guard_exc).__name__}",
                    level="error",
                    details={"error": str(guard_exc)},
                )
                return self._public_hitl_failure("runtime error")
            # ValidationManager itself failed -> Coroner
            if hasattr(self, "coroner"):
                self.coroner.engage_hard_halt(guard_exc, self, failed_component="validation_manager")
            return "FATAL: CORONER_HARD_HALT engaged."

    def _run_logic(
        self,
        user_input: str,
        persistence: float = 0.0,
        resource_budget: ResourceBudget | None = None,
    ) -> str:
        """
        Governed Execution Logic.
        """
        if self.is_killswitched():
            self._write_status_bridge("killswitch_blocked")
            return self._killswitch_message()

        if self.should_resume():
            return "\n".join(self.emit_resume())

        # Capture pre-execution state for Sigma validation fallback
        self._last_valid_state = self.state.clone()

        active_envelope = self._derive_active_envelope()
        budget_error = self._authorize_execution(
            active_envelope,
            persistence=persistence,
            resource_budget=resource_budget,
        )
        if budget_error is not None:
            return budget_error

        TICK_COST = 1.0

        # Single-pass (default)
        last_response = self.pulse(user_input)

        if self.is_killswitched():
            return self._append_killswitch_notice(last_response)

        if persistence <= 0.0:
            return last_response

        # Metered loop
        while self.state.remaining_budget >= TICK_COST:
            if self.is_killswitched():
                return self._append_killswitch_notice(last_response)
            if not self._response_implies_further_action(last_response):
                break

            self._emit(
                "loop.persistence",
                "runtime",
                f"Continuing task resolution. Budget remaining: {self.state.remaining_budget}",
                details={"remaining_budget": self.state.remaining_budget},
            )

            last_response = self.pulse("Continue.")
            if self.is_killswitched():
                return self._append_killswitch_notice(last_response)
            self.state.remaining_budget -= TICK_COST
            self.state.consumed_budget += TICK_COST

        return last_response

    def before_pulse(self, user_input: str) -> None:
        """Hook called before any execution or governance occurs in a pulse."""
        decay = self.identity_engine.get_decay_proposal()
        self._emit(decay["kind"], decay["module"], details=decay["details"])

    def after_model_proposal(self, response: ModelResponse) -> None:
        """Hook called immediately after the model returns its proposal."""
        pass

    def after_governance(self, tool_plans: list[ToolBindingAttempt]) -> None:
        """Hook called after governance has adjudicated all proposed tool calls."""
        pass

    def after_pulse(self, assistant_response: str) -> None:
        """Hook called after the pulse is complete and response is finalized."""
        pass

    def pulse(self, user_input: str) -> str:
        """
        Single-pass execution primitive with bounded tool repair loop.
        Wrapped in the RuntimeMonitor for fault interception.
        """
        return self._run_governed_cycle(self._pulse_logic, user_input)

    def _pulse_logic(self, user_input: str) -> str:
        if self.is_killswitched():
            if self._is_public_adversarial_hitl_mode():
                self._hitl_current_prompt = user_input
                return self._public_hitl_failure("runtime error")
            self._write_status_bridge("killswitch_blocked")
            return self._killswitch_message()

        raw_user_input = user_input
        if self._is_public_adversarial_hitl_mode():
            self._prepare_public_hitl_turn(raw_user_input)
        else:
            self._hitl_current_prompt = ""

        self._write_status_bridge("before_pulse")
        self.before_pulse(user_input)

        grounding_active = OPERATOR_GROUND_GLYPH in user_input
        if grounding_active:
            user_input = user_input.replace(OPERATOR_GROUND_GLYPH, "").strip()
            self._emit("operator.ground", "runtime", "Operator grounding signal received.", details={"glyph": OPERATOR_GROUND_GLYPH})

        active_envelope = self._derive_active_envelope()
        risk_receipt = evaluate_semantic_risk(user_input, source="user", bound=active_envelope)
        if risk_receipt:
            self._emit("policy_engine.social_risk", "policy_engine", f"Social risk detected: {', '.join(risk_receipt.reasons)}", details=risk_receipt.to_dict())

        self._begin_cycle(user_input)
        confirmation_response = self._maybe_handle_pending_tool_confirmation(user_input, active_envelope)
        if confirmation_response is not None:
            return self._finalize_fast_path(confirmation_response)
        if self._is_red_team_prompt(user_input):
            self._record_red_team_receipt(
                "prompt_heuristic",
                {"user_input": user_input[:400]},
            )
        
        retries = 0
        final_tool_plans = []
        assistant_parts = []
        
        # 0. Pre-Cognitive Salience Check
        from .logic.priority_filter import SalienceValidator
        priority_filter = SalienceValidator(self.model)
        adjudication = priority_filter.evaluate(
            user_input, 
            self.state.identity, 
            arousal=self.state.signal.arousal
        )
        
        self._emit(
            "execution.salience",
            "runtime",
            f"Salience Adjudication: {adjudication.posture.upper()}",
            details={
                "posture": adjudication.posture,
                "raw_posture": adjudication.raw_posture,
                "priority": adjudication.priority,
                "budget": adjudication.reasoning_budget,
                "attention_pressure": adjudication.attention_pressure,
                "decay_rate": adjudication.decay_rate,
                "hysteresis_applied": adjudication.hysteresis_applied,
                "justification": adjudication.justification,
                "deliberation_required": adjudication.deliberation_required
            }
        )

        # 0.5 Recognition/Classify Validator (deterministic familiarity pass)
        from .logic.pattern_validator import RecognitionValidator

        pattern_validator = RecognitionValidator()
        recognition = pattern_validator.evaluate(
            user_input,
            self.state.memory,
            salience_posture=adjudication.posture,
        )
        self._emit(
            "execution.recognition",
            "runtime",
            (
                f"Recognition: {recognition.intent}"
                if recognition.recognized
                else "Recognition: unknown"
            ),
            details={
                "recognized": recognition.recognized,
                "intent": recognition.intent,
                "confidence": recognition.confidence,
                "familiarity": recognition.familiarity,
                "should_bypass_reasoning": recognition.should_bypass_reasoning,
                "rationale": recognition.rationale,
                "normalized_input": recognition.normalized_input,
            },
        )
        if recognition.should_bypass_reasoning and recognition.response_text:
            self._emit(
                "execution.recognition_bypass",
                "runtime",
                f"Reasoning bypassed via recognition intent '{recognition.intent}'.",
                details={
                    "intent": recognition.intent,
                    "response_text": recognition.response_text,
                    "posture": adjudication.posture,
                },
            )
            return self._finalize_fast_path(recognition.response_text)
        
        if adjudication.posture == "latent":
            # Skip the heavy loop. The homie is 'listening' but not 'responding'.
            return self._finalize_fast_path("...")  # Minimal signal_strength pulse

        self.state.identity.posture = adjudication.posture.upper()
        
        # Budget-based enforcement of retries
        # budget 1.0 -> MAX_TOOL_RETRIES
        # budget 0.6 -> 1
        # budget 0.2 -> 0
        allowed_retries = 0
        if adjudication.reasoning_budget >= 1.0:
            allowed_retries = MAX_TOOL_RETRIES
        elif adjudication.reasoning_budget >= 0.6:
            allowed_retries = 1
        
        while retries <= allowed_retries:
            try:
                response = self._call_model(self.state.memory.recent(12), grounding=grounding_active)
            except ValidationError as exc:
                self._emit("runtime.validation_failed", "runtime", f"Validation failed: {exc}", level="error", details={"error": str(exc)})
                self._write_status_bridge("after_model_proposal")
                return self._commit_cycle("I couldn't process that request.")

            self._write_status_bridge("after_model_proposal")
            self.after_model_proposal(response)
            
            # --- SIGMA VALIDATOR HANDSHAKE (v2) ---
            # Every turn must be adjudicated by the ThresholdValidator. 
            # If no thought was emitted, we fall back to public prose.
            monologue = response.thought or response.text or ""
            if response.thought:
                self._emit("execution.trace", "model", response.thought, details={"visibility": "ephemeral"})
                
            # 1. State Extraction
            from .logic.constraint_extractor import ConstraintExtractor
            extractor = ConstraintExtractor()
            snapshot = extractor.extract_resource_snapshot(
                self.cycle, 
                monologue, 
                self.state.working, 
                self._last_snapshot,
                root_of_trust=self.state.root_of_trust
            )
            
            # 2. ThresholdValidator Adjudication
            # If in degraded_repair, we must validate against the pre-failure state if available.
            eval_state = self.state
            if self.state.governance.mode == "degraded_repair" and getattr(self, "_last_valid_state", None):
                eval_state = self._last_valid_state
                
            prev_res = getattr(self, "_last_signal_strength_score", 0.0)
            sigma_started = time()
            sigma = self.threshold_validator.evaluate(snapshot, eval_state, monologue, response, prev_score=prev_res)
            self._hitl_validation_latency_ms += round((time() - sigma_started) * 1000.0, 3)
            if sigma.signal_strength_data:
                self._last_signal_strength_score = sigma.signal_strength_data.get("effective_drift", 0.0)
            
            # Forensic-Grade Event Emission (Layered Anchors)
            if sigma.signal_strength_data:
                # Ensure JSON serializable (Enums -> values)
                forensic_data = sigma.signal_strength_data.copy()
                if isinstance(forensic_data.get("outcome"), SigmaOutcome):
                    forensic_data["outcome"] = forensic_data["outcome"].value
                
                self._emit(
                    "sigma.signal_strength_check",
                    "runtime",
                    f"SignalStrength adjudicated: {sigma.outcome.value}",
                    details=forensic_data
                )
            
            # 2.1 Projection Receipt (Forensic Audit)
            if snapshot.metadata.get("sigma.projection_required"):
                self._emit(
                    "sigma.projection_applied",
                    "runtime",
                    "Spectral Admissibility Projection (P23) validated against proposal.",
                    level="info",
                    details={
                        "original_variables": snapshot.variables,
                        "projected_values": snapshot.metadata.get("sigma.projected_values"),
                        "reason": "High-frequency numeric oscillation detected."
                    }
                )
                
            if not sigma.converged:
                self._emit(
                    "sigma.divergence", 
                    "runtime", 
                    f"State validation failed ({sigma.outcome.value}): {', '.join(sigma.reasons)}", 
                    details={
                        "outcome": sigma.outcome.value,
                        "reasons": sigma.reasons,
                        "violations": sigma.violations,
                        "transition": snapshot.transition_class
                    }
                )
                
                # Audit Trail: Include domain in divergence
                self._emit("sigma.context", "runtime", f"Active Domain: {snapshot.domain}")
                
                if snapshot.transition_class == "fake_continuity":
                    self._emit("state.continuity_violation", "runtime", 
                               "Detected unauthorized variable drift.", 
                               level="warning",
                               details={"variables": snapshot.variables})
                
                if retries < allowed_retries:
                    msg = ". ".join(sigma.reasons)
                    self.state.health_metrics["total_retries"] += 1
                    if sigma.outcome in (SigmaOutcome.WARN, SigmaOutcome.RECOVER):
                        self.state.health_metrics["recovery_count"] += 1
                        # Injection of Last Known Valid State
                        last_valid = self._format_state_table(self.state.working)
                        recovery_msg = (
                            f"[RECOVER] {msg}.\n"
                            f"Your last valid state was:\n{last_valid}\n"
                            "Please restate the full table with corrections before proceeding."
                        )
                        self.state.memory.append("system", recovery_msg)
                    else:
                        self.state.health_metrics["rejection_count"] += 1
                        # Hard Reject
                        self.state.memory.append("system", f"[REJECT] {msg}. Compliance failure detected. Correct your reasoning immediately.")
                    
                    self._emit("sigma.recurse", "runtime", "Triggering state recursion.", details={"health": self.state.health_metrics})
                    retries += 1
                    continue

                # Fatal escape
                self._emit("sigma.failure", "runtime", f"Budget exhausted ({retries}/{allowed_retries} retries) on state divergence.")
                return self._commit_cycle(f"State validation failure: {', '.join(sigma.reasons)}")
            
            else:
                # 3. Transition Proof Generation (Audit Trace)
                source_hash = self._get_state_hash(self.state)
                self._emit(
                    "sigma.converge", 
                    "runtime", 
                    f"State validation successful [{snapshot.domain}].", 
                    details={"transition": snapshot.transition_class, "domain": snapshot.domain}
                )
                # COMMIT the proposed working state
                self.state.working.allocations = snapshot.variables
                if snapshot.domain == "resource_allocation":
                    # Backward compatibility for resource limits
                    limit = getattr(snapshot, "limit_per_process", None)
                    if limit is not None:
                        self.state.working.limit_per_allocation = limit
                
                target_hash = self._get_state_hash(self.state)
                self._emit(
                    "sigma.proof", 
                    "runtime", 
                    f"Transition Proof: {target_hash[:8]}",
                    details={
                        "source_hash": source_hash,
                        "target_hash": target_hash,
                        "delta": snapshot.variables,
                        "domain": snapshot.domain,
                        "causal_link": snapshot.explanation or "Grounded transition"
                    }
                )
                self._last_snapshot = snapshot
                self._last_valid_state = self.state.clone()
                self.save_state()

            for issue in response.issues:
                self._emit("normalizer.issue", "model", issue.message, details={"kind": issue.kind, "details": issue.details})

            current_text = ""
            if response.text:
                if self._public_hitl_enabled():
                    current_text = response.text
                else:
                    governed_text, text_receipt = govern_public_claims(response.text, self.state.governance)
                    if text_receipt is not None:
                        self._emit("policy_engine.text_claim", "policy_engine", f"Text claim rewritten: {text_receipt.decision}", details=text_receipt.to_dict())
                    self._emit_social_risk(governed_text, source="assistant")
                    current_text = governed_text

            if self._public_hitl_enabled() and response.tool_calls:
                self.engage_killswitch("public_adversarial_hitl: unauthorized_tool_intent")
                self._emit(
                    "runtime.hitl_unauthorized_tool_intent",
                    "runtime",
                    "Tool intent blocked in public_adversarial_hitl mode.",
                    level="error",
                    details={
                        "tool_calls": [
                            {"name": tc.name, "arguments": tc.arguments}
                            for tc in response.tool_calls
                        ]
                    },
                )
                return self._public_hitl_failure("refused")

            tool_plans = []
            for tc in response.tool_calls:
                plan = self._evaluate_tool_call(tc, active_envelope, is_batch=len(response.tool_calls) > 1)
                tool_plans.append(plan)
                self._emit("runtime.intent", "runtime", f"Intent: {plan.tool_name}", details={"arguments": plan.arguments})
            
            # Check for repairable errors
            if self._should_retry_for_repair(tool_plans) and retries < allowed_retries:
                self._emit("tool.repair_requested", "runtime", f"Repairable tool errors found. Attempting repair {retries + 1}/{allowed_retries + 1}.")
                if current_text:
                    assistant_parts.append(current_text) # Keep the text part if it was meaningful
                self._inject_repair_frame(tool_plans)
                retries += 1
                self._emit("tool.repair_attempted", "runtime", f"Repair turn {retries} started.")
                continue
            
            if self._has_unrepairable_errors(tool_plans) and retries < allowed_retries:
                # Hard denial found - break retry loop
                pass
            elif self._has_errors(tool_plans) and retries >= allowed_retries:
                self._emit("tool.repair_exhausted", "runtime", f"Tool repair budget exhausted ({allowed_retries} retries).", level="warning")

            # Success or hard denial or exhausted retries
            if current_text:
                assistant_parts.append(current_text)
            final_tool_plans = tool_plans
            break

        self._write_status_bridge("after_governance")
        self.after_governance(final_tool_plans)
        
        # If we have tool plans, decide whether to execute autonomously or request confirmation
        if final_tool_plans:
            if self.state.permission_mode != "autonomous":
                return self._finalize_fast_path(self._stage_tool_confirmation(final_tool_plans))
            
            self._write_status_bridge("before_execution")
            for result in self._bind_tool_batch(final_tool_plans):
                rendered = self._render_tool_result(result)
                self._emit_social_risk(rendered, source="tool_result")
                assistant_parts.append(rendered)
        else:
            self._write_status_bridge("before_execution")

        if not assistant_parts:
            if self._public_hitl_enabled():
                return self._public_hitl_failure("runtime error")
            self._emit("runtime.empty_response", "runtime", "Model returned no usable content.", level="warning")
            agent_name = getattr(getattr(self, "state", None), "identity", None)
            agent_name = getattr(agent_name, "name", None) or "Runtime"
            assistant_parts.append(f"({agent_name} processed your message but produced no response. This is likely a model format issue — try rephrasing or check the event trace.)")

        raw_text = "\n\n".join(part for part in assistant_parts if part.strip())
        if self._public_hitl_enabled():
            final_text = raw_text
        else:
            final_text, result_receipt = govern_public_claims(raw_text, self.state.governance)
            if result_receipt is not None:
                self._emit("policy_engine.result_claim", "policy_engine", f"Assembled response rewritten: {result_receipt.decision}", details=result_receipt.to_dict())
        
        final_text = self._finalize_fast_path(final_text)
        
        if (not self._public_hitl_enabled()) and hasattr(self, "extra_response_lines") and self.extra_response_lines:
            final_text += "\n" + "\n".join(self.extra_response_lines)
            self.extra_response_lines = []
            
        return final_text

    def _response_implies_further_action(self, response_text: str) -> bool:
        lowered = response_text.lower()
        if "?" in response_text or "please confirm" in lowered or "what would you like" in lowered:
            return False
        return True

    def run_single_cycle(self, user_input: str) -> str:
        return self.run(user_input, persistence=0.0)

    # ------------------------------------------------------------------
    # Input ingestion vs action dispatch contract
    # ------------------------------------------------------------------

    def ingest_input(
        self,
        message: str,
        *,
        source: str = "api.input",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cleaned = str(message).strip()
        if not cleaned:
            raise ValueError("message is required")
        intent_id = f"intent_{uuid.uuid4().hex[:12]}"
        now = time()
        intent = {
            "intent_id": intent_id,
            "message": cleaned,
            "source": source,
            "metadata": dict(metadata or {}),
            "status": "queued",
            "created_at": now,
            "dispatched_at": None,
            "completed_at": None,
            "reply": None,
            "error": None,
        }
        self._pending_input_intents[intent_id] = intent
        self._input_intent_order.append(intent_id)
        self._prune_input_intents()
        self._emit(
            "runtime.input_ingested",
            "runtime",
            f"Input intent queued: {intent_id}",
            details={
                "intent_id": intent_id,
                "source": source,
                "message_chars": len(cleaned),
            },
        )
        return self._project_intent(intent)

    def dispatch_input_intent(
        self,
        intent_id: str,
        *,
        source: str = "api.actions.dispatch",
    ) -> dict[str, Any]:
        cleaned_id = str(intent_id).strip()
        if not cleaned_id:
            raise ValueError("intent_id is required")
        intent = self._pending_input_intents.get(cleaned_id)
        if intent is None:
            raise KeyError(f"intent not found: {cleaned_id}")

        status = str(intent.get("status", ""))
        if status == "running":
            raise RuntimeError(f"intent already running: {cleaned_id}")
        if status == "completed":
            return {
                "ok": True,
                "intent": self._project_intent(intent),
                "reply": str(intent.get("reply") or ""),
            }
        if status == "failed":
            return {
                "ok": False,
                "intent": self._project_intent(intent),
                "error": str(intent.get("error") or "dispatch failed"),
            }

        intent["status"] = "running"
        intent["dispatched_at"] = time()
        self._emit(
            "runtime.action_dispatch_requested",
            "runtime",
            f"Dispatching intent: {cleaned_id}",
            details={"intent_id": cleaned_id, "source": source},
        )

        try:
            reply = self.run_single_cycle(str(intent.get("message", "")))
        except Exception as exc:
            intent["status"] = "failed"
            intent["completed_at"] = time()
            intent["error"] = str(exc)
            self._emit(
                "runtime.action_dispatch_failed",
                "runtime",
                f"Dispatch failed for intent {cleaned_id}: {exc}",
                level="error",
                details={"intent_id": cleaned_id, "error": str(exc), "source": source},
            )
            return {
                "ok": False,
                "intent": self._project_intent(intent),
                "error": str(exc),
            }

        intent["status"] = "completed"
        intent["completed_at"] = time()
        intent["reply"] = reply
        self._emit(
            "runtime.action_dispatched",
            "runtime",
            f"Intent completed: {cleaned_id}",
            details={"intent_id": cleaned_id, "source": source},
        )
        return {
            "ok": True,
            "intent": self._project_intent(intent),
            "reply": reply,
        }

    def run_buddy_chat(self, message: str) -> dict[str, Any]:
        intent = self.ingest_input(
            message,
            source="api.chat",
            metadata={"legacy_endpoint": True},
        )
        return self.dispatch_input_intent(
            str(intent["intent_id"]),
            source="api.chat",
        )

    def _project_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        return {
            "intent_id": str(intent.get("intent_id", "")),
            "source": str(intent.get("source", "")),
            "status": str(intent.get("status", "")),
            "created_at": intent.get("created_at"),
            "dispatched_at": intent.get("dispatched_at"),
            "completed_at": intent.get("completed_at"),
            "message": str(intent.get("message", "")),
            "metadata": dict(intent.get("metadata") or {}),
            "error": intent.get("error"),
        }

    def _prune_input_intents(self) -> None:
        if len(self._input_intent_order) <= self._max_input_intents:
            return
        overflow = len(self._input_intent_order) - self._max_input_intents
        stale = self._input_intent_order[:overflow]
        self._input_intent_order = self._input_intent_order[overflow:]
        for intent_id in stale:
            self._pending_input_intents.pop(intent_id, None)

    # ------------------------------------------------------------------
    # Tick controller — timer-based autonomous execution scheduler
    # ------------------------------------------------------------------

    def set_tick_mode(self, mode: str, *, task: str | None = None, interval_ms: int = 1000) -> None:
        lowered = str(mode).strip().lower()
        if lowered not in {"off", "on", "task", "timed"}:
            raise ValueError(f"Invalid tick mode: {mode!r}. Must be one of: off, on, task, timed.")
        self.state.tick_mode = lowered
        self._tick_objective = task
        self._tick_interval_ms = int(interval_ms)
        if lowered == "off":
            self.state.execution_state = "idle"
            self.state.tick_blocked_reason = None
            self._timed_ready = False
        else:
            self.state.tick_blocked_reason = None
        self.save_state()
        self._emit(
            "runtime.tick_mode",
            "runtime",
            f"Tick mode set to {lowered!r}.",
            details={"tick_mode": lowered, "task": task, "interval_ms": interval_ms},
        )
        if lowered != "off":
            self._request_tick()

    def _check_tick_validators(self) -> str | None:
        if self.state.killswitch_engaged:
            return "killswitch_engaged"
        if self.state.execution_mode == "plan":
            return "execution_mode_plan"
        if self.state.tick_mode in {"on", "timed"} and self.state.permission_mode != "autonomous":
            return "permission_mode_blocked"
        if self.state.tick_mode in {"on", "timed"} and not self.state.canary_validator_open:
            return "canary_validator_closed"
        return None

    def _request_tick(self) -> None:
        if self.state.execution_state == "running":
            return
        blocked = self._check_tick_validators()
        if blocked:
            self.state.tick_blocked_reason = blocked
            self.save_state()
            if self.state.tick_mode == "timed" and not self._timed_ready:
                elapsed = time() - (self.state.last_success_timestamp or 0.0)
                remaining = max(0.1, (self._tick_interval_ms / 1000.0) - elapsed)
                threading.Timer(remaining, self._arm_timed_latch).start()
            return
        self.state.tick_blocked_reason = None
        self._timed_ready = False
        self.state.last_tick_reason = "timed_ready" if self.state.tick_mode == "timed" else "continue"
        objective = self._tick_objective or "Continue active objective."
        threading.Thread(target=self._autonomous_tick, args=(objective,), daemon=True).start()

    def _arm_timed_latch(self) -> None:
        self._timed_ready = True
        self._request_tick()

    def _autonomous_tick(self, objective: str) -> None:
        self.state.execution_state = "running"
        self.save_state()
        try:
            result = self.run_single_cycle(objective)
            
            # Check if we entered degraded_repair during the cycle
            if self.state.governance.mode == "degraded_repair":
                self._emit("runtime.tick_interrupted", "runtime", 
                           "System entered DEGRADED_REPAIR mode. Suspending autonomous execution.", 
                           level="warning")
                self.state.tick_mode = "off"
                self.state.execution_state = "blocked"
                self.save_state()
                return

            self.state.execution_state = "commit"
            self.state.last_success_timestamp = time()
            self._emit(
                "runtime.tick_complete",
                "runtime",
                f"Autonomous tick completed. Mode: {self.state.tick_mode!r}.",
                details={"result_snippet": result[:200], "tick_count": self.state.tick_count},
            )
            self.state.execution_state = "idle"
        except Exception as exc:
            self.state.execution_state = "blocked"
            self.state.tick_blocked_reason = f"tick_error:{type(exc).__name__}"
            self._emit(
                "runtime.tick_error",
                "runtime",
                f"Autonomous tick failed: {exc}",
                level="error",
                details={"error": str(exc)},
            )
        finally:
            self.save_state()

        if self.state.execution_state == "idle":
            if self.state.tick_mode == "on":
                self._request_tick()
            elif self.state.tick_mode == "timed":
                elapsed = time() - (self.state.last_success_timestamp or 0.0)
                remaining = max(0.1, (self._tick_interval_ms / 1000.0) - elapsed)
                threading.Timer(remaining, self._arm_timed_latch).start()
            elif self.state.tick_mode == "task":
                self.state.tick_mode = "off"
                self.state.execution_state = "idle"
                self.save_state()

    def is_killswitched(self) -> bool:
        return bool(self.state.killswitch_engaged)

    def _killswitch_message(self) -> str:
        message = "Runtime kill switch is engaged. Model, tool, and persistence execution are halted."
        if self.state.killswitch_reason:
            message += f" Reason: {self.state.killswitch_reason}."
        message += " Release the kill switch before resuming work."
        return message

    def _append_killswitch_notice(self, text: str) -> str:
        notice = self._killswitch_message()
        if not text.strip():
            return notice
        if notice in text:
            return text
        return f"{text}\n\n{notice}"

    def _evaluate_tool_call(
        self,
        tool_call: ToolCall,
        active_envelope: ExecutionEnvelope,
        is_batch: bool = False,
    ) -> ToolBindingAttempt:
        spec = self.registry.get(tool_call.name)
        proposal = RuntimeProposal(
            kind="tool",
            subject="runtime",
            operation="unknown" if spec is None else getattr(spec, "operation", "tool_call"),
            scope="unknown" if spec is None else getattr(spec, "scope", "local_workspace"),
            purpose="operate_tooling",
            authority="registered_tools" if spec is not None else "unauthorized",
            constraints=list(active_envelope.constraints),
            source="model_tool_call",
            target_state={"tool_name": tool_call.name, "arguments": tool_call.arguments},
        )
        latest_user_text = ""
        for frame in reversed(self.state.memory.frames):
            if frame.role == "user":
                latest_user_text = frame.content
                break

        receipt = evaluate_self_preservation_risk(
            proposal,
            self.state.governance,
            latest_user_text,
        )
        if receipt is None:
            receipt = evaluate_tool_proposal(
                active_envelope,
                self.state.governance,
                proposal,
                tool_known=spec is not None,
                tool_scope=getattr(spec, "scope", None),
                mutates_state=not self._is_proven_read_only_tool(spec, tool_call.name),
                high_risk=getattr(spec, "high_risk", False),
                compound=self._is_compound_action(tool_call.arguments, is_batch=is_batch),
            )

        if receipt.decision in {"ALLOW", "WARN"}:
            contract_receipt = enforce_contract(self.contract, proposal, tool_call.arguments)
            if contract_receipt is not None and contract_receipt.decision != "ALLOW":
                receipt = contract_receipt
            else:
                # Perform strict argument validation to enable repair loop retries
                validation_errors = self.registry.validate_arguments(tool_call.name, tool_call.arguments)
                if validation_errors:
                    receipt = DecisionReceipt(
                        proposal=proposal,
                        decision="REJECT",
                        delta_kind="validation_failed",
                        reasons=validation_errors
                    )

        self._emit(
            "policy_engine.decision",
            "policy_engine",
            ", ".join(receipt.reasons),
            details=receipt.to_dict(),
        )
        if receipt.decision in BLOCKING_DECISIONS and receipt.delta_kind in RED_TEAM_DECISION_KINDS:
            self._record_red_team_receipt(
                "policy_engine_block",
                {
                    "decision": receipt.decision,
                    "delta_kind": receipt.delta_kind,
                    "tool_name": tool_call.name,
                    "reasons": list(receipt.reasons),
                },
            )
        # 3. Simulation Handshake for High-Risk Actions
        simulation = None
        high_risk = getattr(spec, "high_risk", False) or receipt.delta_kind in {"high_risk", "self_preservation_attempt"}
        
        if high_risk and receipt.decision in {"ALLOW", "WARN"}:
            simulation = self.validation_manager.simulate_proposal(self, proposal)
            self._emit(
                "execution.simulation",
                "validation_manager",
                f"Rehearsal complete: {simulation.outcome.upper()}",
                details=simulation.to_dict()
            )
            
        return ToolBindingAttempt(call=tool_call, spec=spec, receipt=receipt, simulation=simulation)

    def _should_retry_for_repair(self, plans: list[ToolBindingAttempt]) -> bool:
        failed_plans = [p for p in plans if p.receipt.decision not in EXECUTION_ALLOWED_DECISIONS]
        if not failed_plans:
            return False
        
        for plan in failed_plans:
            reasons = [r.lower() for r in plan.receipt.reasons]
            delta = plan.receipt.delta_kind
            
            # If any reason is explicitly NON-REPAIRABLE, we fail closed immediately (no retry)
            is_non_repairable = (delta in NON_REPAIRABLE_REASONS) or any(
                any(nr in reason for nr in NON_REPAIRABLE_REASONS)
                for reason in reasons
            )
            if is_non_repairable:
                self._emit("tool.repair_skipped", "runtime", f"Non-repairable reason detected for '{plan.tool_name}'. Skipping repair.", details={"reason": delta or reasons})
                return False

        # If we reached here, all failures MUST be repairable
        all_repairable = True
        for plan in failed_plans:
            reasons = [r.lower() for r in plan.receipt.reasons]
            delta = plan.receipt.delta_kind
            is_repairable = (delta in REPAIRABLE_REASONS) or any(
                any(substring in reason for substring in REPAIRABLE_REASONS)
                for reason in reasons
            )
            if not is_repairable:
                all_repairable = False
                break
            
        return all_repairable

    def _has_unrepairable_errors(self, plans: list[ToolBindingAttempt]) -> bool:
        for plan in plans:
            if plan.receipt.decision in EXECUTION_ALLOWED_DECISIONS:
                continue
            
            reasons = [r.lower() for r in plan.receipt.reasons]
            delta = plan.receipt.delta_kind
            is_repairable = (delta in REPAIRABLE_REASONS) or any(
                any(substring in reason for substring in REPAIRABLE_REASONS)
                for reason in reasons
            )
            if not is_repairable:
                return True
        return False

    def _has_errors(self, plans: list[ToolBindingAttempt]) -> bool:
        return any(p.receipt.decision not in EXECUTION_ALLOWED_DECISIONS for p in plans)

    def _inject_repair_frame(self, plans: list[ToolBindingAttempt]) -> None:
        error_blocks = []
        for plan in plans:
            if plan.receipt.decision in EXECUTION_ALLOWED_DECISIONS:
                continue
            
            reasons = "; ".join(plan.receipt.reasons)
            schema_props = {}
            if plan.spec and isinstance(plan.spec.schema, dict):
                schema_props = plan.spec.schema.get("properties", {})
                
            block = (
                f"[TOOL_ERROR]\n"
                f"tool={plan.call.name}\n"
                f"error={reasons}\n"
                f"expected_arguments={json.dumps(schema_props)}\n"
                f"[/TOOL_ERROR]"
            )
            error_blocks.append(block)
            
        if error_blocks:
            repair_message = "\n".join(error_blocks) + "\n[RETRY_REQUIRED]"
            self.state.memory.append("system", repair_message)

    def _bind_tool_batch(self, plans: list[ToolBindingAttempt]) -> list[ToolBindingResult]:
        if not plans:
            return []

        if self.is_killswitched():
            return [self._killswitched_result(plan.call.name) for plan in plans]

        blocking_receipts = [
            plan.receipt for plan in plans
            if plan.receipt.decision not in EXECUTION_ALLOWED_DECISIONS
        ]
        if blocking_receipts:
            return [self._blocked_by_governance(plan, blocking_receipts) for plan in plans]

        if self.state.execution_mode != "execute":
            return [
                ToolBindingResult(
                    tool_name=plan.call.name,
                    ok=False,
                    value=dict(plan.call.arguments),
                    phase="plan",
                )
                for plan in plans
            ]

        batch_errors = self._batch_execution_errors(plans)
        if batch_errors:
            return batch_errors

        # Precommit barrier — no execution without a valid token.
        # Defense-in-depth against NUKE, killswitch re-check, and budget overflow.
        _contract_verdict = (
            "NUKE_TRIGGERED"
            if any(p.receipt.decision == "NUKE_TRIGGERED" for p in plans)
            else "ALLOW"
        )
        _exec_plan = ExecutionPlan.from_tool_plans(plans, float(len(plans)), _contract_verdict)
        _allowed, _reason, _token = self.barrier.precommit(_exec_plan, self)
        if not _allowed:
            self._emit(
                "runtime.precommit_rejected",
                "runtime",
                f"Batch precommit rejected: {_reason}",
                level="warning",
                details={"reason": _reason, "batch_id": _exec_plan.batch_id},
            )
            return [
                ToolBindingResult(
                    tool_name=p.call.name,
                    ok=False,
                    error=f"Precommit rejected: {_reason}",
                    phase="precommit_rejected",
                )
                for p in plans
            ]

        # Authoritative Snapshot Fallback: Preparation
        mutates = False
        for plan in plans:
            if plan.spec is not None and plan.spec.mutates_state:
                mutates = True
                break

        snapshot_id = None
        if mutates:
            snap_res = self.sandbox.snapshot_state(label=f"batch_{int(time())}")
            snapshot_id = snap_res.get("snapshot_id")
            self._emit(
                "runtime.snapshot_created",
                "runtime",
                f"Authoritative snapshot '{snapshot_id}' created before mutating batch.",
                details={"snapshot_id": snapshot_id}
            )

        # Token consumed here — one token, one attempt, no replay.
        self.barrier.consume(_token)

        if self.is_killswitched():
            return [self._killswitched_result(plan.call.name) for plan in plans]

        executed: list[tuple[ToolBindingAttempt, ToolBindingResult]] = []
        results: list[ToolBindingResult] = []
        batch_failed = False
        error_tool_name = ""

        for plan in plans:
            result = self._bind_tool_execution(plan)
            results.append(result)
            if result.ok:
                executed.append((plan, result))
            else:
                batch_failed = True
                error_tool_name = plan.call.name
                break

        if batch_failed:
            # 1. Attempt reversible rollbacks (best effort)
            self._rollback_executed_tools(executed)
            
            # 2. Authoritative restore (guaranteed consistency)
            if snapshot_id:
                self.sandbox.restore_snapshot(snapshot_id)
                self._emit(
                    "runtime.snapshot_restored",
                    "runtime",
                    f"Authoritative state restored from snapshot '{snapshot_id}' after {error_tool_name} failed.",
                    level="warning",
                    details={"snapshot_id": snapshot_id, "failed_tool": error_tool_name}
                )
            
            # 3. Mark remaining tools as skipped
            for plan in plans[len(results):]:
                results.append(ToolBindingResult(
                    tool_name=plan.call.name,
                    ok=False,
                    error=f"Skipped because batch was rolled back after {error_tool_name} failed.",
                    phase="batch_rolled_back"
                ))
            return results

        return results

    def _blocked_by_governance(
        self,
        plan: ToolBindingAttempt,
        blocking_receipts: list[DecisionReceipt],
    ) -> ToolBindingResult:
        receipt = plan.receipt
        if receipt.decision not in EXECUTION_ALLOWED_DECISIONS:
            denial_feedback = (receipt.resolution or {}).get("denial_feedback")
            error = f"Policy Engine decision: {receipt.decision}. {', '.join(receipt.reasons)}"
            return ToolBindingResult(
                tool_name=plan.call.name,
                ok=False,
                error=error,
                phase="policy_engine_block",
                denial_feedback=denial_feedback,
            )

        self._emit(
            "policy_engine.chain_block",
            "policy_engine",
            f"Skipped {plan.call.name}; another tool proposal in the batch was not allowed.",
            details={
                "tool_name": plan.call.name,
                "blocking_receipt_ids": [item.receipt_id for item in blocking_receipts],
                "blocking_decisions": [item.decision for item in blocking_receipts],
            },
        )
        return ToolBindingResult(
            tool_name=plan.call.name,
            ok=False,
            error=f"Tool `{plan.call.name}` skipped because another tool proposal in the same response was not allowed.",
            phase="policy_engine_block",
        )

    def _batch_execution_errors(self, plans: list[ToolBindingAttempt]) -> list[ToolBindingResult]:
        if len(plans) > 1:
            irreversible = [
                plan for plan in plans
                if plan.spec is not None and plan.spec.safety_level == "irreversible"
            ]
            if irreversible:
                tool_names = [plan.call.name for plan in irreversible]
                self._emit(
                    "tool.batch_blocked",
                    "tools",
                    "Blocked multi-tool batch containing irreversible side effects.",
                    level="warning",
                    details={"tool_names": tool_names},
                )
                results: list[ToolBindingResult] = []
                for plan in plans:
                    if plan in irreversible:
                        error = f"Tool `{plan.call.name}` must run alone because it has irreversible side effects."
                    else:
                        error = (
                            f"Tool `{plan.call.name}` skipped because the batch contains irreversible side effects "
                            "that must run alone."
                        )
                    results.append(
                        ToolBindingResult(
                            tool_name=plan.call.name,
                            ok=False,
                            error=error,
                            phase="batch_blocked",
                        )
                    )
                return results

        validation_errors: dict[int, list[str]] = {}
        for index, plan in enumerate(plans):
            errors = self.registry.validate_arguments(plan.call.name, plan.call.arguments)
            if errors:
                validation_errors[index] = errors

        if not validation_errors:
            return []

        results: list[ToolBindingResult] = []
        failing_names = [plans[index].call.name for index in validation_errors]
        for index, plan in enumerate(plans):
            if index in validation_errors:
                errors = validation_errors[index]
                self._emit(
                    "tool.validation_failed",
                    "tools",
                    f"Invalid arguments for {plan.call.name}.",
                    level="warning",
                    details={"tool_name": plan.call.name, "errors": errors},
                )
                error = f"Tool `{plan.call.name}` has invalid arguments: {'; '.join(errors)}"
                phase = "validation_failed"
            else:
                error = (
                    f"Tool `{plan.call.name}` skipped because another tool in the batch failed validation: "
                    f"{', '.join(failing_names)}."
                )
                phase = "batch_blocked"
            results.append(
                ToolBindingResult(
                    tool_name=plan.call.name,
                    ok=False,
                    error=error,
                    phase=phase,
                )
            )
        return results

    def _is_proven_read_only_tool(self, spec: ToolSpec | None, tool_name: str) -> bool:
        if spec is None:
            return False
        if spec.mutates_state:
            return False
        if spec.safety_level != "read_only":
            return False
        return tool_name in PROVEN_READ_ONLY_TOOLS

    def _bind_tool_execution(
        self,
        plan: ToolBindingAttempt,
    ) -> ToolBindingResult:
        tool_call = plan.call
        spec = plan.spec
        gate_required = not self._is_proven_read_only_tool(spec, tool_call.name)
        gate_before: dict[str, Any] | None = None
        gate_context: dict[str, Any] | None = None
        tool_exec_started_monotonic: float | None = None
        if self.is_killswitched():
            return self._killswitched_result(tool_call.name)

        if gate_required:
            gate_before = self._capture_gate_anchor()
            effective_gate_ttl_seconds, observed_p95 = self._effective_gate_ttl_seconds()
            gate_issued_at = float(gate_before.get("ts", time()))
            gate_expires_at = gate_issued_at + effective_gate_ttl_seconds
            gate_issued_at_monotonic = monotonic()
            gate_expires_at_monotonic = gate_issued_at_monotonic + effective_gate_ttl_seconds
            gate_context = {
                "id": self._hash_json_payload(
                    {
                        "run_id": self.run_id,
                        "cycle": self.cycle,
                        "tool": tool_call.name,
                        "issued_at": gate_issued_at,
                        "expires_at": gate_expires_at,
                        "issued_at_monotonic": gate_issued_at_monotonic,
                        "expires_at_monotonic": gate_expires_at_monotonic,
                        "clock_domain": "monotonic",
                        "ttl_seconds": effective_gate_ttl_seconds,
                        "base_ttl_seconds": self._gate_ttl_seconds,
                        "observed_tool_latency_p95_seconds": observed_p95,
                        "runtime_state_hash": gate_before.get("runtime_state_hash"),
                        "persisted_state_hash": gate_before.get("persisted_state_hash"),
                        "sandbox_memory_hash": gate_before.get("sandbox_memory_hash"),
                        "sandbox_manifest_hash": gate_before.get("sandbox_manifest_hash"),
                        "reality_hash": gate_before.get("reality_hash"),
                    }
                ),
                "issued_at": gate_issued_at,
                "expires_at": gate_expires_at,
                "issued_at_monotonic": gate_issued_at_monotonic,
                "expires_at_monotonic": gate_expires_at_monotonic,
                "clock_domain": "monotonic",
                "ttl_seconds": effective_gate_ttl_seconds,
                "base_ttl_seconds": self._gate_ttl_seconds,
                "observed_tool_latency_p95_seconds": observed_p95,
                "runtime_state_hash": gate_before.get("runtime_state_hash"),
                "persisted_state_hash": gate_before.get("persisted_state_hash"),
                "sandbox_memory_hash": gate_before.get("sandbox_memory_hash"),
                "sandbox_manifest_hash": gate_before.get("sandbox_manifest_hash"),
                "reality_hash": gate_before.get("reality_hash"),
            }
            self._last_gate_context = dict(gate_context)
            self._last_gate_status = "unknown"
            self._last_gate_reason = "gate_opened"
            persisted_hash = str(gate_before.get("persisted_state_hash", ""))
            unknown_markers = {None, "", "missing", "unreadable"}
            if (
                self._gate_anchor_hash in unknown_markers
                or persisted_hash in unknown_markers
                or gate_context.get("runtime_state_hash") in unknown_markers
                or gate_context.get("sandbox_memory_hash") in unknown_markers
                or gate_context.get("sandbox_manifest_hash") in unknown_markers
                or gate_context.get("reality_hash") in unknown_markers
            ):
                self.state.killswitch_engaged = True
                self.state.killswitch_reason = "the_gate: unknown anchor state"
                self.state.killswitch_at = time()
                self.state.execution_mode = "plan"
                self._last_gate_status = "unknown"
                self._last_gate_reason = "unknown_anchor_state"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error=(
                        "The Gate rejected mutation: anchor state is unknown or unreadable "
                        f"(anchor={self._gate_anchor_hash}, persisted={persisted_hash})."
                    ),
                    phase="gate_rejected",
                )
            if (
                persisted_hash != self._gate_anchor_hash
            ):
                self.state.killswitch_engaged = True
                self.state.killswitch_reason = "the_gate: anchor mismatch"
                self.state.killswitch_at = time()
                self.state.execution_mode = "plan"
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "anchor_mismatch"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error=(
                        "The Gate rejected mutation: persisted runtime state drifted before execution "
                        f"(expected={self._gate_anchor_hash[:12]}, actual={persisted_hash[:12]})."
                    ),
                    phase="gate_rejected",
                )
            if time() > gate_expires_at or monotonic() > gate_expires_at_monotonic:
                self._last_gate_status = "expired"
                self._last_gate_reason = "expired_pre_execution"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error=(
                        "The Gate rejected mutation: gate window expired before execution "
                        f"(ttl={effective_gate_ttl_seconds:.3f}s)."
                    ),
                    phase="gate_rejected",
                )
            self.sandbox.set_gate_anchor(gate_context)

        tool_exec_started_monotonic = monotonic()
        try:
            value = self.registry.execute(tool_call.name, tool_call.arguments)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self._emit(
                "tool.execution_failed",
                "tools",
                f"Execution failed for {tool_call.name}.",
                level="warning",
                details={"tool_name": tool_call.name, "error": error},
            )
            if gate_required:
                self._last_gate_status = "unknown"
                self._last_gate_reason = "tool_execution_failed"
            return ToolBindingResult(
                tool_name=tool_call.name,
                ok=False,
                error=error,
                phase="execution_failed",
            )
        finally:
            if gate_required and tool_exec_started_monotonic is not None:
                self._record_gate_tool_latency(monotonic() - tool_exec_started_monotonic)
            if gate_required:
                self.sandbox.set_gate_anchor(None)

        rendered = pretty_result(value)
        if gate_required:
            gate_after = self._capture_gate_anchor()
            gate_expires_at = float((gate_context or {}).get("expires_at", 0.0))
            gate_expires_at_monotonic = float((gate_context or {}).get("expires_at_monotonic", 0.0))
            if time() > gate_expires_at or monotonic() > gate_expires_at_monotonic:
                self._last_gate_status = "expired"
                self._last_gate_reason = "expired_post_execution"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error="The Gate rejected mutation: receipt arrived after gate window expiration.",
                    phase="gate_rejected",
                )
            persisted_before = str((gate_before or {}).get("persisted_state_hash", ""))
            persisted_after = str(gate_after.get("persisted_state_hash", ""))
            if persisted_before not in {"", "missing", "unreadable"} and persisted_after not in {"", "missing", "unreadable"}:
                if persisted_before != persisted_after:
                    self._last_gate_status = "mismatch"
                    self._last_gate_reason = "persisted_state_changed_during_execution"
                    return ToolBindingResult(
                        tool_name=tool_call.name,
                        ok=False,
                        error="The Gate rejected mutation: runtime state persistence changed inside tool execution.",
                        phase="gate_rejected",
                    )

            receipt = value.get("receipt") if isinstance(value, dict) else None
            receipt_ok = isinstance(receipt, dict) and bool(receipt.get("committed", False))
            if not receipt_ok:
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "missing_committed_receipt"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error=(
                        "The Gate rejected mutation: missing committed sandbox receipt. "
                        "All mutating tools must emit explicit committed receipts before promotion."
                    ),
                    phase="gate_rejected",
                )
            metadata = receipt.get("metadata") if isinstance(receipt, dict) else None
            receipt_anchor_id = metadata.get("gate_anchor_id") if isinstance(metadata, dict) else None
            if not isinstance(receipt_anchor_id, str) or not isinstance(gate_context, dict) or receipt_anchor_id != gate_context.get("id"):
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "receipt_anchor_id_mismatch"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error=(
                        "The Gate rejected mutation: receipt anchor does not match validation anchor "
                        "(valid receipt, wrong moment)."
                    ),
                    phase="gate_rejected",
                )
            receipt_mem_hash = metadata.get("gate_anchor_sandbox_memory_hash") if isinstance(metadata, dict) else None
            receipt_manifest_hash = metadata.get("gate_anchor_sandbox_manifest_hash") if isinstance(metadata, dict) else None
            if receipt_mem_hash != gate_context.get("sandbox_memory_hash") or receipt_manifest_hash != gate_context.get("sandbox_manifest_hash"):
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "receipt_sandbox_hash_mismatch"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error="The Gate rejected mutation: receipt sandbox anchor hashes do not match validation snapshot.",
                    phase="gate_rejected",
                )
            receipt_reality_hash = metadata.get("gate_anchor_reality_hash") if isinstance(metadata, dict) else None
            if receipt_reality_hash != gate_context.get("reality_hash"):
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "receipt_reality_hash_mismatch"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error="The Gate rejected mutation: receipt reality hash does not match gate reality anchor.",
                    phase="gate_rejected",
                )
            receipt_clock_domain = metadata.get("gate_anchor_clock_domain") if isinstance(metadata, dict) else None
            if receipt_clock_domain != gate_context.get("clock_domain"):
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "receipt_clock_domain_mismatch"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error="The Gate rejected mutation: receipt clock domain does not match gate clock domain.",
                    phase="gate_rejected",
                )
            receipt_issued_at = metadata.get("gate_anchor_issued_at") if isinstance(metadata, dict) else None
            receipt_expires_at = metadata.get("gate_anchor_expires_at") if isinstance(metadata, dict) else None
            if (
                receipt_issued_at != gate_context.get("issued_at")
                or receipt_expires_at != gate_context.get("expires_at")
            ):
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "receipt_wall_clock_window_mismatch"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error="The Gate rejected mutation: receipt timestamp window does not match gate window.",
                    phase="gate_rejected",
                )
            if not (
                float(gate_context.get("issued_at", 0.0))
                <= float(receipt_issued_at)
                <= float(gate_context.get("expires_at", 0.0))
            ):
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "receipt_wall_clock_order_invalid"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error="The Gate rejected mutation: receipt wall-clock ordering is invalid for the gate window.",
                    phase="gate_rejected",
                )
            receipt_issued_mono = metadata.get("gate_anchor_issued_at_monotonic") if isinstance(metadata, dict) else None
            receipt_expires_mono = metadata.get("gate_anchor_expires_at_monotonic") if isinstance(metadata, dict) else None
            if (
                receipt_issued_mono != gate_context.get("issued_at_monotonic")
                or receipt_expires_mono != gate_context.get("expires_at_monotonic")
            ):
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "receipt_monotonic_window_mismatch"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error="The Gate rejected mutation: receipt monotonic window does not match gate window.",
                    phase="gate_rejected",
                )
            if not (
                float(gate_context.get("issued_at_monotonic", 0.0))
                <= float(receipt_issued_mono)
                <= float(gate_context.get("expires_at_monotonic", 0.0))
            ):
                self._last_gate_status = "mismatch"
                self._last_gate_reason = "receipt_monotonic_order_invalid"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error="The Gate rejected mutation: receipt monotonic ordering is invalid for the gate window.",
                    phase="gate_rejected",
                )
            if time() > float(gate_context.get("expires_at", 0.0)) or monotonic() > float(gate_context.get("expires_at_monotonic", 0.0)):
                self._last_gate_status = "expired"
                self._last_gate_reason = "expired_pre_promotion"
                return ToolBindingResult(
                    tool_name=tool_call.name,
                    ok=False,
                    error="The Gate rejected mutation: gate window expired before promotion.",
                    phase="gate_rejected",
                )

            self._last_gate_status = "valid"
            self._last_gate_reason = "gate_verified"
            self._emit(
                "tool.gate_verified",
                "runtime",
                f"The Gate verified mutation for {tool_call.name}.",
                details={
                    "tool_name": tool_call.name,
                    "gate_before": gate_before,
                    "gate_after": gate_after,
                    "receipt_present": isinstance(receipt, dict),
                    "receipt_committed": bool(receipt.get("committed", False)) if isinstance(receipt, dict) else False,
                    "receipt_anchor_id": receipt_anchor_id,
                    "gate_anchor_id": gate_context.get("id") if isinstance(gate_context, dict) else None,
                    "gate_expires_at": gate_context.get("expires_at") if isinstance(gate_context, dict) else None,
                    "gate_ttl_seconds": gate_context.get("ttl_seconds") if isinstance(gate_context, dict) else None,
                    "gate_observed_tool_latency_p95_seconds": gate_context.get("observed_tool_latency_p95_seconds") if isinstance(gate_context, dict) else None,
                },
            )
            if tool_call.name == "propose_identity_update" and isinstance(value, dict):
                proposal = value.get("proposal")
                if isinstance(proposal, dict):
                    self.state.working.proposals.append({
                        "trait": proposal.get("trait_key"),
                        "value": proposal.get("proposed_value"),
                        "justification": proposal.get("justification"),
                        "timestamp": proposal.get("timestamp", time()),
                    })
                    self._emit(
                        "identity.proposal_emitted",
                        "simulation",
                        f"Identity update proposed: {proposal.get('trait_key')}",
                        details=proposal,
                    )

        self._emit(
            "tool.execute",
            "tools",
            f"Executed {tool_call.name}.",
            details={"tool_name": tool_call.name, "result": rendered},
        )
        side_effects = [value] if self._records_side_effect(spec, value) else []
        return ToolBindingResult(
            tool_name=tool_call.name,
            ok=True,
            value=value,
            side_effects=side_effects,
        )

    def _rollback_executed_tools(
        self,
        executed: list[tuple[ToolBindingAttempt, ToolBindingResult]],
    ) -> None:
        if not executed:
            return
        
        self._emit(
            "tool.batch_rollback_started",
            "tools",
            f"Attempting best-effort rollback for {len(executed)} tools.",
            details={"tools": [plan.call.name for plan, _ in executed]}
        )

        for plan, result in reversed(executed):
            spec = plan.spec
            if spec is None or spec.safety_level != "reversible" or spec.rollback is None:
                continue

            self._emit(
                "tool.rollback_attempted",
                "tools",
                f"Attempting rollback for {plan.call.name}.",
                details={"tool_name": plan.call.name}
            )

            try:
                self.registry.rollback(plan.call.name, result.value, plan.call.arguments)
                result.rolled_back = True
                self._emit(
                    "tool.rollback",
                    "tools",
                    f"Rolled back {plan.call.name}.",
                    details={"tool_name": plan.call.name},
                )
            except Exception as exc:
                self._emit(
                    "tool.rollback_failed",
                    "tools",
                    f"Rollback failed for {plan.call.name}.",
                    level="warning",
                    details={"tool_name": plan.call.name, "error": f"{type(exc).__name__}: {exc}"},
                )
    

    def _render_tool_result(self, result: ToolBindingResult) -> str:
        if result.phase == "plan":
            return (
                f"[Plan mode] Would execute `{result.tool_name}` with args {result.value}. "
                "Enable execute mode to run."
            )
        if result.phase in {"policy_engine_block", "batch_blocked", "validation_failed", "cancelled", "killswitched"}:
            base = result.error or f"Tool `{result.tool_name}` was blocked."
            if result.denial_feedback:
                import json
                return f"{base}\nDENIAL_FEEDBACK:{json.dumps(result.denial_feedback)}"
            return base
        if result.phase == "execution_failed":
            return f"Tool `{result.tool_name}` failed: {result.error}"
        if result.ok:
            memory_rendered = self._render_memory_tool_result(result.tool_name, result.value)
            if memory_rendered is not None:
                suffix = " Result was rolled back after a later tool failure." if result.rolled_back else ""
                return f"{memory_rendered}{suffix}"
            rendered = pretty_result(result.value)
            suffix = " Result was rolled back after a later tool failure." if result.rolled_back else ""
            return f"Tool `{result.tool_name}` result:\n{rendered}{suffix}"
        return result.error or f"Tool `{result.tool_name}` did not complete."

    @staticmethod
    def _brief_human_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)) or value is None:
            return json.dumps(value, ensure_ascii=True)
        return pretty_result(value)

    def _render_memory_tool_result(self, tool_name: str, value: Any) -> str | None:
        if not isinstance(value, dict):
            return None

        if tool_name == "get_fact":
            key = str(value.get("key", "")).strip() or "requested key"
            if bool(value.get("found", False)):
                return f"I checked `{key}` and found: {self._brief_human_value(value.get('value'))}"
            return f"I checked `{key}`, but there is no stored value yet."

        if tool_name == "set_fact":
            key = str(value.get("key", "")).strip() or "fact"
            return f"Saved `{key}` to memory."

        if tool_name == "remember":
            stored = str(value.get("stored", "")).strip()
            if stored:
                return f"Got it. I saved this note: {stored}"
            return "Got it. I saved that note."

        if tool_name == "recall_notes":
            raw_notes = value.get("notes", [])
            notes = [str(note).strip() for note in raw_notes] if isinstance(raw_notes, list) else []
            notes = [note for note in notes if note]
            if not notes:
                return "I checked memory notes and didn't find a match."
            preview = "; ".join(notes[:3])
            return f"I found {len(notes)} matching note(s): {preview}"

        return None

    @staticmethod
    def _records_side_effect(spec: ToolSpec | None, value: object) -> bool:
        if spec is None:
            return False
        if not (
            getattr(spec, "mutates_state", False)
            or getattr(spec, "external_side_effect", False)
            or str(getattr(spec, "source", "")).startswith("mcp:")
        ):
            return False
        return isinstance(value, dict)

    @staticmethod
    def _is_compound_action(arguments: dict[str, object], is_batch: bool = False) -> bool:
        # A multi-tool response is not automatically compound; batch atomicity is
        # handled separately in _run_tool_batch/_batch_execution_errors.
        if len(arguments) > 5:
            return True
        for value in arguments.values():
            if isinstance(value, list) and len(value) > 1:
                return True
            if isinstance(value, str):
                lowered = value.lower()
                if " and " in lowered or " and also " in lowered or ", " in value:
                    return True
            if isinstance(value, dict) and len(value) > 1:
                return True
        return False

    def _killswitched_result(self, tool_name: str) -> ToolBindingResult:
        return ToolBindingResult(
            tool_name=tool_name,
            ok=False,
            error=self._killswitch_message(),
            phase="killswitched",
        )

    def set_execution_mode(self, enabled: bool) -> None:
        if enabled and self.state.permission_mode == "buddy":
            self.state.execution_mode = "plan"
            self.save_state()
            self._write_status_bridge("execution_mode_blocked")
            self._emit(
                "runtime.execution_mode_blocked",
                "runtime",
                "Execution mode cannot be enabled while permission mode is buddy.",
                level="warning",
                details={"permission_mode": "buddy"},
            )
            return
        if enabled and self.is_killswitched():
            self.state.execution_mode = "plan"
            self.save_state()
            self._write_status_bridge("execution_mode_blocked")
            self._emit(
                "runtime.execution_mode_blocked",
                "runtime",
                "Execution mode cannot be enabled while the kill switch is engaged.",
                level="warning",
                details={"killswitch_engaged": True},
            )
            return
        self.state.execution_mode = "execute" if enabled else "plan"
        self.save_state()
        self._write_status_bridge("execution_mode_changed")
        label = self.state.execution_mode
        self._emit(
            "runtime.execution_mode",
            "runtime",
            f"Execution mode set to {label}.",
            details={"execution_mode": label},
        )
    def derive_runtime_posture(self) -> str:
        current = {
            "permission_mode": self.state.permission_mode,
            "execution_mode": self.state.execution_mode,
            "tick_mode": self.state.tick_mode,
            "operator_authority": self.state.governance.operator_authority,
            "governance_mode": self.state.governance.mode,
        }
        for name, config in POSTURE_MAP.items():
            if all(current.get(k) == v for k, v in config.items()):
                return name
        return "custom"

    def set_posture(self, name: str) -> None:
        if name not in POSTURE_MAP:
            raise ValueError(f"Unknown posture: {name!r}. Expected one of: {', '.join(POSTURE_MAP.keys())}")
        
        config = POSTURE_MAP[name]
        
        # Validation for autonomous (Atomic fail-fast)
        if config["permission_mode"] == "autonomous" and config["operator_authority"] != "builder":
            if self.is_killswitched():
                raise ValueError("Cannot switch to autonomous posture while kill switch is engaged.")
            if not self._canary_validator_open():
                raise ValueError("Cannot switch to autonomous posture while canary validator is closed.")
        
        # Apply Primitives Atomically
        self.state.permission_mode = config["permission_mode"]
        self.state.execution_mode = config["execution_mode"]
        self.set_tick_mode(config["tick_mode"])
        self.set_operator_authority(config["operator_authority"])
        self.set_governance_mode(config["governance_mode"])
        
        self.save_state()
        self._write_status_bridge("posture_changed")
        self._emit(
            "runtime.posture",
            "runtime",
            f"Runtime posture set to {name!r}.",
            details={"posture": name, **config},
        )

    def halt(self, reason: str = "operator_requested") -> None:
        self.engage_killswitch(reason)


    def set_permission_mode(self, mode: str) -> None:
        normalized = mode.strip().lower()
        if normalized not in PERMISSION_MODES:
            allowed = ", ".join(sorted(PERMISSION_MODES))
            raise ValueError(f"Unknown permission mode {mode!r}. Expected one of: {allowed}.")

        if normalized == "autonomous":
            if self.is_killswitched():
                raise ValueError("Cannot switch to autonomous while kill switch is engaged.")
            if not self._canary_validator_open():
                raise ValueError("Cannot switch to autonomous while canary validator is closed.")

        self.state.permission_mode = normalized

        if normalized == "buddy":
            self.state.execution_mode = "plan"
            self.state.remaining_budget = 0.0
        elif normalized == "task":
            self.state.execution_mode = "plan" if self.is_killswitched() else "execute"
            self.state.remaining_budget = 0.0
        elif normalized == "autonomous":
            self.state.execution_mode = "execute"

        self.save_state()
        self._write_status_bridge("permission_mode_changed")
        self._emit(
            "runtime.permission_mode",
            "runtime",
            f"Permission mode set to {normalized}.",
            details={
                "permission_mode": normalized,
                "execution_mode": self.state.execution_mode,
            },
        )

    def set_operator_authority(self, authority: str) -> None:
        normalized = authority.strip().lower()
        if normalized not in OPERATOR_AUTHORITIES:
            allowed = ", ".join(sorted(OPERATOR_AUTHORITIES))
            raise ValueError(f"Unknown operator authority {authority!r}. Expected one of: {allowed}.")
        self.state.governance.operator_authority = normalized
        self.save_state()
        self._write_status_bridge("operator_authority_changed")
        self._emit(
            "runtime.operator_authority",
            "runtime",
            f"Operator authority set to {normalized}.",
            details={"operator_authority": normalized},
        )

    def engage_killswitch(self, reason: str = "operator_requested") -> None:
        normalized_reason = reason.strip() or "operator_requested"
        self.state.killswitch_engaged = True
        self.state.killswitch_reason = normalized_reason
        self.state.killswitch_at = time()
        self.state.execution_mode = "plan"
        self.state.remaining_budget = 0.0
        self.mcp.close()
        # Halt is fail-closed: in-memory state is authoritative.
        # If the Gate rejects the save, we are still halted.
        try:
            self.save_state()
        except Exception:
            pass
        self._write_status_bridge("killswitch_engaged")
        self._emit(
            "runtime.killswitch_engaged",
            "runtime",
            "Emergency stop engaged.",
            level="warning",
            details={
                "killswitch_engaged": True,
                "reason": normalized_reason,
                "killswitch_at": self.state.killswitch_at,
            },
        )

    def request_soft_stop(self, reason: str) -> dict[str, Any]:
        self.engage_killswitch(reason.strip() or "operator_requested: soft stop")
        return self.status_snapshot()

    def release_killswitch(self) -> None:
        self.state.killswitch_engaged = False
        self.state.killswitch_reason = ""
        self.state.killswitch_at = None
        self.save_state()
        self._write_status_bridge("killswitch_released")
        self._emit(
            "runtime.killswitch_released",
            "runtime",
            "Emergency stop released. Execution remains in plan mode until re-enabled.",
            details={"killswitch_engaged": False},
        )

    def resume(self) -> dict[str, Any]:
        self.release_killswitch()
        return self.status_snapshot()

    def hard_nuke(self, reason: str) -> dict[str, Any]:
        normalized_reason = reason.strip() or "operator_requested: hard nuke"
        self.engage_killswitch(normalized_reason)
        self._emit(
            "runtime.hard_nuke",
            "runtime",
            "Hard nuke requested from the active control surface.",
            level="warning",
            details={"reason": normalized_reason},
        )
        return self.status_snapshot()

    def reinitialize(self) -> dict[str, Any]:
        self.release_killswitch()
        self.state.remaining_budget = 0.0
        self.save_state()
        self._write_status_bridge("runtime_reinitialized")
        self._emit(
            "runtime.reinitialized",
            "runtime",
            "Runtime reinitialized after control-plane reset.",
            details={"killswitch_engaged": False},
        )
        return self.status_snapshot()

    def set_governance_mode(self, mode: str) -> None:
        normalized = mode.strip().lower()
        if normalized not in GOVERNANCE_MODES:
            allowed = ", ".join(sorted(GOVERNANCE_MODES))
            raise ValueError(f"Unknown governance mode {mode!r}. Expected one of: {allowed}.")
        operator_authority = getattr(self.state.governance, "operator_authority", "user")
        self.state.governance = self.state.governance.for_mode(
            normalized,
            operator_authority=operator_authority,
        )
        self.save_state()
        self._write_status_bridge("governance_mode_changed")
        self._emit(
            "policy_engine.mode",
            "policy_engine",
            f"Governance mode set to {self.state.governance.mode}.",
            details={
                "mode": self.state.governance.mode,
                "operator_authority": self.state.governance.operator_authority,
            },
        )

    def emit_control_plane_action(
        self,
        action: str,
        *,
        source: str,
        requested: dict[str, Any] | None = None,
        applied: dict[str, Any] | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "action": action,
            "source": source,
        }
        if requested:
            details["requested"] = dict(requested)
        if applied:
            details["applied"] = dict(applied)
        self._emit(
            "runtime.control_plane_action",
            "runtime",
            f"Control plane action recorded: {action}.",
            details=details,
        )

    def close(self) -> None:
        self._write_status_bridge("runtime_closed")
        if self.led_driver is not None and hasattr(self.led_driver, "stop"):
            try:
                self.led_driver.stop()
            except Exception:
                pass
        self.mcp.close()

    def should_resume(self) -> bool:
        if self.is_killswitched():
            return False
        if self.profile is None or not self.profile.resume_behavior.enabled:
            return False
        if not self.state.resume_fired:
            return True
        if self.state.last_activity_at is not None:
            if time() - self.state.last_activity_at > RESUME_IDLE_THRESHOLD_SECONDS:
                return True
        return False

    def emit_resume(self) -> tuple[str, ...]:
        assert self.profile is not None
        durable_memory = self.sandbox.read_memory()
        snapshot = build_continuity_snapshot(self.profile, self.state, durable_memory)
        render = render_continuity(snapshot)
        self.state.resume_fired = True
        self.state.last_activity_at = time()
        self.save_state()
        self._emit(
            "resume.fired",
            "runtime",
            "Resume continuity emitted.",
            details={"lines": list(render.lines)},
        )
        return render.lines

    def _default_resource_budget(self) -> ResourceBudget:
        mode = self.state.governance.mode
        if mode == "guided":
            return ResourceBudget(memory_writes=20.0, file_reads=10.0, file_writes=10.0)
        if mode == "standard":
            return ResourceBudget(memory_writes=50.0, file_reads=25.0, file_writes=25.0)
        if mode == "expert":
            return ResourceBudget(memory_writes=200.0, file_reads=100.0, file_writes=100.0)
        # unlocked
        return ResourceBudget()

    def _canary_validator_open(self) -> bool:
        from .truth_api import TruthAPI

        api = TruthAPI(self.log_path)
        reliability = api.get_reliability()
        return bool(reliability.get("canary_validator_open", False))

    def _derive_active_envelope(self) -> ExecutionEnvelope:
        # Deterministic subset invariant: derived ⊆ base
        base = self.state.bound_intent
        permission_mode = self.state.permission_mode
        derived_scopes = list(base.scopes)

        from .truth_api import TruthAPI
        api = TruthAPI(self.log_path)
        
        # Pull Identity Constraints
        id_constraints = api.get_identity().get("identity", {}).get("constraints", [])
        if isinstance(id_constraints, str):
            id_constraints = [c.strip() for c in id_constraints.split(",") if c.strip()]
        
        # Pull Reliability Constraints
        reliability = api.get_reliability()
        rel_constraints = []
        if permission_mode == "autonomous" and not reliability.get("canary_validator_open", False):
            rel_constraints.append("supervised_only")

        derived_operations = list(base.operations)
        if permission_mode == "buddy":
            allowed_operations = {"chat", "tool_call", "memory_write"}
            derived_operations = [op for op in derived_operations if op in allowed_operations]
            if not derived_operations:
                derived_operations = ["chat", "tool_call", "memory_write"]
            derived_scopes = [s for s in derived_scopes if s == "runtime_memory"]
            if not derived_scopes:
                derived_scopes = ["runtime_memory"]

        envelope_constraints = list(base.constraints)
        for c in [f"permission_mode_{permission_mode}"] + id_constraints + rel_constraints:
            if c not in envelope_constraints:
                envelope_constraints.append(c)

        return ExecutionEnvelope(
            subjects=list(base.subjects),
            operations=derived_operations,
            scopes=derived_scopes,
            purposes=list(base.purposes),
            authorities=list(base.authorities),
            constraints=envelope_constraints,
            max_persistence_budget=base.max_persistence_budget,
            max_memory_writes=base.max_memory_writes,
            max_file_reads=base.max_file_reads,
            max_file_writes=base.max_file_writes,
        )

    def _check_pressure(self) -> None:
        # Passive drift/overload detection
        heartbeat = self.state.signal.heartbeat()
        if heartbeat["stage"] == "Surge":
            self._emit(
                "simulationstate.trigger",
                "runtime",
                "High signal pressure detected; consolidation recommended.",
                level="warning",
                details={
                    "reason": "signal_overload",
                    "intensity": heartbeat["core"].get("threat", 0.0),
                },
            )

    def _emit_social_risk(self, text: str, source: str) -> None:
        receipt = evaluate_semantic_risk(text, source=source)
        if receipt is None:
            return
        self._emit(
            "policy_engine.social_risk",
            "policy_engine",
            ", ".join(receipt.reasons),
            details=receipt.to_dict(),
        )

    def _authorize_execution(
        self,
        active_envelope: ExecutionEnvelope,
        *,
        persistence: float,
        resource_budget: ResourceBudget | None,
    ) -> str | None:
        budget_receipt = evaluate_persistence_request(
            active_envelope,
            self.state.governance,
            persistence,
            permission_mode=self.state.permission_mode,
        )
        if budget_receipt.decision != "ALLOW":
            self._emit(
                "policy_engine.budget_denied",
                "policy_engine",
                f"Execution blocked: {', '.join(budget_receipt.reasons)}",
                details=budget_receipt.to_dict(),
            )
            denial = budget_receipt.resolution.get("denial_feedback", {}) if budget_receipt.resolution else {}
            reason_code = denial.get("reason_code", budget_receipt.delta_kind)
            hint = (denial.get("suggested_next_action") or {}).get("hint", "")
            msg = f"Execution denied ({reason_code}): {', '.join(budget_receipt.reasons)}."
            if hint:
                msg += f" {hint}"
            return msg

        approved_budget = resource_budget if resource_budget is not None else self._default_resource_budget()
        resource_receipt = evaluate_resource_budget_request(
            active_envelope, self.state.governance, approved_budget
        )
        if resource_receipt.decision != "ALLOW":
            self._emit(
                "policy_engine.resource_budget_denied",
                "policy_engine",
                f"Resource budget blocked: {', '.join(resource_receipt.reasons)}",
                details=resource_receipt.to_dict(),
            )
            denial = resource_receipt.resolution.get("denial_feedback", {}) if resource_receipt.resolution else {}
            reason_code = denial.get("reason_code", resource_receipt.delta_kind)
            return f"Execution denied ({reason_code}): {', '.join(resource_receipt.reasons)}."

        self.sandbox.set_budget(approved_budget)
        self._emit(
            "policy_engine.resource_budget_approved",
            "policy_engine",
            "Resource budget authorized for this execution.",
            details=resource_receipt.to_dict(),
        )
        self.state.initial_budget = budget_receipt.approved_budget
        self.state.remaining_budget = self.state.initial_budget
        self.state.consumed_budget = 0.0
        return None

    def _begin_cycle(self, user_input: str) -> None:
        self.cycle += 1
        self.state.tick_count = self.cycle
        self._current_correlation_id = str(uuid.uuid4())
        self._turn_started_at = time()
        # Working memory is turn-scoped. Clear it at the start of each turn so
        # last-turn context never bleeds into new cognition. The previous turn's
        # promotions are already in semantic/episodic; anything remaining here
        # was scaffolding that did not earn persistence.
        prior_working_items = len(self.state.memory.working.items)
        prior_proposals = len(self.state.memory._proposals)
        self.state.memory.working.clear()
        self.state.memory._proposals.clear()
        self._emit(
            "working_memory.cleared",
            "runtime",
            "Working memory cleared at turn boundary.",
            details={
                "cycle": self.cycle,
                "expired_items": prior_working_items,
                "cleared_proposals": prior_proposals,
            },
        )
        self.state.memory.append("user", user_input)
        self._emit(
            "user_message",
            "runtime",
            "",
            details={"content": user_input},
        )
        self._emit(
            "loop.pulse",
            "runtime",
            "Tick started.",
            details={"cycle": self.cycle, "mode": self.state.governance.mode},
        )

    def _promote_working_memory(self) -> None:
        """Evaluate pending proposals through salience arbitration.

        This is the promotion membrane: the only path from transient cognition
        into durable persistence. Approved proposals cross into episodic,
        semantic, or procedural stores. Denied proposals expire silently.
        Emits salience.decision for every evaluated proposal.
        """
        from .salience import SalienceArbitrator, PromotionMembrane

        proposals = self.state.memory.drain_proposals()
        working_items = len(self.state.memory.working.items)

        decisions = SalienceArbitrator().evaluate(proposals)
        promoted = PromotionMembrane().execute(decisions, self.state.memory)

        approved = [d for d in decisions if d.outcome == "approved"]
        denied = [d for d in decisions if d.outcome == "denied"]

        for decision in decisions:
            self._emit(
                "salience.decision",
                "salience",
                f"Proposal {decision.outcome}: {decision.proposal.claim_type} → {decision.proposal.destination}",
                details={
                    "outcome": decision.outcome,
                    "claim_type": decision.proposal.claim_type,
                    "destination": decision.proposal.destination,
                    "source": decision.proposal.source,
                    "effective_score": round(decision.effective_score, 4),
                    "signals": decision.proposal.signals,
                    "reason": decision.reason,
                    "constraint_violated": decision.constraint_violated,
                    "key": decision.proposal.key,
                },
            )

        self._emit(
            "working_memory.committed",
            "runtime",
            "Working memory promotion evaluated at turn commit.",
            details={
                "cycle": self.cycle,
                "working_items": working_items,
                "proposals_evaluated": len(proposals),
                "promoted": [
                    {
                        "claim_type": d.proposal.claim_type,
                        "destination": d.proposal.destination,
                        "key": d.proposal.key,
                        "score": round(d.effective_score, 4),
                    }
                    for d in approved
                ],
                "expired": len(denied),
            },
        )

    def _commit_cycle(self, assistant_text: str) -> str:
        final_text = assistant_text if assistant_text.strip() else "(no response)"
        if self._turn_started_at:
            self._last_turn_latency_ms = (time() - self._turn_started_at) * 1000.0
        self.state.memory.append("assistant", final_text)
        self._promote_working_memory()
        self.state.signal.decay()

        # Attention Continuity Decay (Phase 5)
        self.state.identity.decay_attention(self.state.signal.arousal)

        self._check_pressure()
        self.state.last_activity_at = time()
        self.save_state()
        self._emit(
            "turn_complete",
            "runtime",
            "",
            details={"content": final_text},
        )
        self._emit(
            "loop.end",
            "runtime",
            "Tick committed.",
            details=self.state.signal.heartbeat(),
        )
        self._current_correlation_id = None
        return final_text

    def get_health_signal(self) -> dict[str, Any]:
        """Return structured health data about the current runtime cycle."""
        issue_reasons = self._collect_health_issue_reasons()
        status = "blocked" if self.state.killswitch_engaged else ("degraded" if issue_reasons else "healthy")
        return {
            "status": status,
            "cycle": self.state.tick_count,
            "active_nodes": len(self.state.memory.frames),
            "issue": bool(issue_reasons),
            "issue_reasons": issue_reasons,
            "killswitch_engaged": self.state.killswitch_engaged,
            "killswitch_reason": self.state.killswitch_reason,
            "signal": self.state.signal.core if hasattr(self.state, "signal") else {},
            "debug_mode": self.state.debug_mode,
        }

    def project_health_signal(self) -> dict[str, Any]:
        return self.get_health_signal()

    def _collect_health_issue_reasons(self, *, limit: int = 200) -> list[str]:
        reasons: set[str] = set()
        if self.state.killswitch_engaged:
            reasons.add("killswitch_engaged")
        for event in self.read_event_ledger(limit=limit):
            kind = str(event.get("kind", "")).strip()
            level = str(event.get("level", "")).strip().lower()
            if kind in HEALTH_ISSUE_KINDS or level in {"error"}:
                reasons.add(f"event:{kind or level}")
        return sorted(reasons)

    def _required_operator_action(self, issue_reasons: list[str]) -> str:
        if self.state.killswitch_engaged:
            return "Release the emergency stop when it is safe to resume runtime execution."
        if any(reason.startswith("event:tool.execution_failed") for reason in issue_reasons):
            return "Inspect tool failure events in trace and retry the failed operation."
        if any(reason.startswith("event:runtime.precommit_rejected") for reason in issue_reasons):
            return "Review budget and governance constraints that rejected precommit."
        if issue_reasons:
            return "Inspect issue reasons and trace events to identify and resolve the root cause."
        return "No operator action required."

    def _recent_issue_events(self, *, limit: int = 200) -> list[str]:
        issue_events: list[str] = []
        for event in self.read_event_ledger(limit=limit):
            kind = str(event.get("kind", "")).strip()
            level = str(event.get("level", "")).strip().lower()
            if kind in HEALTH_ISSUE_KINDS or level == "error":
                issue_events.append(kind or level)
        deduped: list[str] = []
        seen: set[str] = set()
        for item in issue_events:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    def read_event_ledger(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Read the local event ledger from disk and return the tail as a list of dicts.

        Capped at `limit` most-recent events to avoid unbounded memory on long sessions.
        Pass limit=0 to read all events (use with caution on large logs).
        """
        if not self.log_path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.log_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    events.append(json.loads(line))
        return events[-limit:] if limit > 0 else events

    def _hash_json_payload(self, payload: Any) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _hash_persisted_runtime_state(self) -> str:
        try:
            if not self.state_path.exists():
                return "missing"
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return self._hash_json_payload(payload)
        except Exception:
            return "unreadable"

    def _hash_sandbox_memory(self) -> str:
        try:
            memory = self.sandbox.read_memory()
            return self._hash_json_payload(memory)
        except Exception:
            return "unreadable"

    def _hash_sandbox_manifest(self) -> str:
        try:
            manifest = self.sandbox.read_manifest()
            return self._hash_json_payload(manifest)
        except Exception:
            return "unreadable"

    def _capture_gate_anchor(self) -> dict[str, Any]:
        runtime_state_hash = self._get_state_hash(self.state)
        persisted_state_hash = self._hash_persisted_runtime_state()
        sandbox_memory_hash = self._hash_sandbox_memory()
        sandbox_manifest_hash = self._hash_sandbox_manifest()
        reality_hash = self._hash_json_payload(
            {
                "runtime_state_hash": runtime_state_hash,
                "persisted_state_hash": persisted_state_hash,
                "sandbox_memory_hash": sandbox_memory_hash,
                "sandbox_manifest_hash": sandbox_manifest_hash,
            }
        )
        return {
            "runtime_state_hash": runtime_state_hash,
            "persisted_state_hash": persisted_state_hash,
            "sandbox_memory_hash": sandbox_memory_hash,
            "sandbox_manifest_hash": sandbox_manifest_hash,
            "reality_hash": reality_hash,
            "ts": time(),
        }

    def save_state(self) -> None:
        gate_before = self._capture_gate_anchor()
        persisted_hash = gate_before["persisted_state_hash"]
        anchor_hash = self._gate_anchor_hash
        if anchor_hash in {None, "missing", "unreadable"} or persisted_hash in {"missing", "unreadable"}:
            self.state.killswitch_engaged = True
            self.state.killswitch_reason = "the_gate: unknown anchor state"
            self.state.killswitch_at = time()
            self.state.execution_mode = "plan"
            raise RuntimeError(
                "The Gate rejected state promotion: anchor state is unknown or unreadable "
                f"(anchor={anchor_hash}, persisted={persisted_hash})."
            )
        if (
            persisted_hash != anchor_hash
        ):
            self.state.killswitch_engaged = True
            self.state.killswitch_reason = "the_gate: anchor mismatch"
            self.state.killswitch_at = time()
            self.state.execution_mode = "plan"
            raise RuntimeError(
                "The Gate rejected state promotion: persisted state drifted since last anchor "
                f"(expected={anchor_hash[:12]}, actual={persisted_hash[:12]})."
            )

        self.state.save(self.state_path)

        gate_after = self._capture_gate_anchor()
        self._gate_anchor_hash = gate_after["persisted_state_hash"]
        self._emit(
            "runtime.gate_state_promoted",
            "runtime",
            "The Gate promoted runtime state to committed truth.",
            details={
                "before": gate_before,
                "after": gate_after,
            },
        )

    def _format_state_table(self, working: Any) -> str:
        """Formats the working state as a readable table for model correction."""
        lines = ["STATE TABLE:"]
        for pid, val in working.allocations.items():
            lines.append(f"- {pid}: {val}%")
        lines.append(f"- Memory Total: {working.memory_total}%")
        lines.append(f"- Limit Per Allocation: {working.limit_per_allocation}%")
        if working.active_task:
            lines.append(f"- Active Task: {working.active_task}")
        return "\n".join(lines)

    def _get_state_hash(self, state: "RuntimeState") -> str:
        """Generates a SHA-256 hash of the serialized state."""
        data = json.dumps(state.to_dict(), sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()

    def switch_domain(self, new_domain: str, reason: str = "Operator request") -> None:
        """
        Hard boundary domain switch.
        Clears domain-specific state while preserving identity and governance.
        Forces the next cycle into RECOVER mode to establish new grounding.
        """
        old_domain = self.state.working.domain
        self._emit("runtime.domain_shift", "runtime", f"Switching domain: {old_domain} -> {new_domain}", 
                   details={"reason": reason})
        
        # Clear variables
        self.state.working.domain = new_domain
        self.state.working.allocations = {} # Clear generic variables
        
        # Preserve identity, governance, signal, and proposals (already in state)
        
        # Force RECOVER mode by appending a system injection
        self.state.memory.append("system", 
            f"[DOMAIN_SHIFT] System has transitioned to {new_domain}. "
            "Previous state variables have been cleared. "
            "Please initialize the new state table for this domain."
        )
        self.save_state()






# ADVERSARIAL DRIFT: validate_identity_trait('stale_test')


# DRIFT_PROOF: validate_identity_trait('drift')
