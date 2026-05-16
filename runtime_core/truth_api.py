import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .diff import diff_runs
from .replay import Replayer
from .replay import _load_events, _load_events_with_diagnostics
from .signal import SignalState


DEFAULT_TRAJECTORY_WINDOW = 32
DEFAULT_STM_WINDOW = 24


class TruthAPI:
    """
    The canonical read-only interface for Runtime state.
    Serves as the exclusive read interface for web, CLI, and roadmap servers.
    All state is projected from the immutable event log. Disk state is not trusted.
    """

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.replayer = Replayer()

    def get_identity(self) -> dict[str, Any]:
        """Provides deterministic identity and organism projection derived from the event log."""
        if not self.log_path.exists():
            signal = SignalState()
            return {
                "identity": {},
                "signal": signal.heartbeat(),
                "organism": self._empty_organism(signal),
                "derived_from_events": True,
                "replay_version": "deterministic-v3",
            }

        state = self.replayer.replay(self.log_path)
        identity_payload = self._identity_truth_payload(state)
        organism = self._organism_projection(
            state,
            trajectory_window=DEFAULT_TRAJECTORY_WINDOW,
            stm_window=DEFAULT_STM_WINDOW,
        )
        return {
            "identity": identity_payload,
            "signal": state.signal.heartbeat(),
            "organism": organism,
            "derived_from_events": True,
            "replay_version": "deterministic-v3",
        }

    def get_organism(
        self,
        *,
        trajectory_window: int = DEFAULT_TRAJECTORY_WINDOW,
        stm_window: int = DEFAULT_STM_WINDOW,
    ) -> dict[str, Any]:
        """Returns the canonical organism model projected from replayed state + event trajectory."""
        if not self.log_path.exists():
            signal = SignalState()
            return {
                "organism": self._empty_organism(signal),
                "derived_from_events": True,
                "replay_version": "deterministic-v3",
            }

        state = self.replayer.replay(self.log_path)
        return {
            "organism": self._organism_projection(
                state,
                trajectory_window=trajectory_window,
                stm_window=stm_window,
            ),
            "derived_from_events": True,
            "replay_version": "deterministic-v3",
        }

    def get_reliability(self) -> dict[str, Any]:
        """Projects the system's reliability/autonomy readiness from canary events."""
        events = self.get_events(type_filter="runtime.canary_validator_updated")
        if not events:
            return {
                "canary_validator_open": False,
                "level": "supervised",
                "last_metrics": {},
                "derived_from_events": True,
            }

        # Latest canary update is the current reliability state
        latest = events[-1].get("details", {})
        validator_open = latest.get("validator_open", False)
        return {
            "canary_validator_open": validator_open,
            "level": "autonomous" if validator_open else "supervised",
            "last_metrics": latest.get("metrics", {}),
            "derived_from_events": True,
        }

    def get_state(self) -> dict[str, Any]:
        """Returns the full replayed state, safe for external auditing."""
        if not self.log_path.exists():
            return {}
        state = self.replayer.replay(self.log_path)
        return state.to_dict()

    def get_events(self, type_filter: str | None = None) -> list[dict[str, Any]]:
        """Returns the raw or filtered event stream directly from the log."""
        if not self.log_path.exists():
            return []

        events = []
        with self.log_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if type_filter and event.get("kind") != type_filter:
                        continue
                    events.append(event)
                except json.JSONDecodeError:
                    continue
        return events

    def get_log_integrity(self) -> dict[str, Any]:
        _, diagnostics = _load_events_with_diagnostics(self.log_path)
        malformed = int(diagnostics.get("malformed_lines", 0))
        return {
            **diagnostics,
            "log_path": str(self.log_path.resolve()),
            "status": "degraded" if malformed > 0 else "healthy",
            "derived_from_events": True,
        }

    def diff_against(self, other_log_path: str | Path) -> dict[str, Any]:
        other = Path(other_log_path).expanduser().resolve()
        if not other.exists():
            return {
                "ok": False,
                "error": f"other_log_path does not exist: {other}",
            }
        result = diff_runs(self.log_path.resolve(), other)
        payload = asdict(result)
        first = self._first_divergence(payload.get("cycles", []))
        payload["first_divergence"] = first
        payload["ok"] = True
        payload["identical"] = bool(payload.get("identical", result.identical))
        return payload

    def _identity_truth_payload(self, state: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        # Truth flows from confirmed trait buckets in replayed state.
        for trait in sorted(state.identity.memory_buckets.keys()):
            items = state.identity.memory_buckets.get(trait, [])
            if items:
                payload[trait] = items[-1].get("content", "")
        return payload

    def _organism_projection(
        self,
        state: Any,
        *,
        trajectory_window: int,
        stm_window: int,
    ) -> dict[str, Any]:
        bounded_stm_window = max(1, int(stm_window))
        bounded_trajectory_window = max(1, int(trajectory_window))

        stm_frames = [
            {"role": frame.role, "content": frame.content, "ts": float(frame.ts)}
            for frame in state.memory.frames[-bounded_stm_window:]
        ]
        stm_notes = [str(note) for note in state.memory.notes[-bounded_stm_window:]]
        semantic_items = [
            {
                "key": item.key,
                "content": item.content,
                "ts": float(item.ts),
                "retention": item.retention,
            }
            for item in state.memory.semantic.all()[-bounded_stm_window:]
        ]

        trajectory = self._affective_trajectory_window(window=bounded_trajectory_window)

        return {
            "identity": {
                "name": str(state.identity.name),
                "mode": str(state.identity.mode),
                "posture": str(state.identity.posture),
                "attention_pressure": float(state.identity.attention_pressure),
                "seed_hash": str(state.identity.seed_hash),
                "traits": self._sorted_dict(state.identity.traits),
                "calibrations": self._sorted_dict(state.identity.calibrations),
                "confirmed_traits": self._identity_truth_payload(state),
            },
            "affective_state": self._signal_snapshot(state.signal),
            "stm": {
                "frames": stm_frames,
                "notes": stm_notes,
                "semantic_items": semantic_items,
                "window_size": bounded_stm_window,
            },
            "trajectory": {
                "window": trajectory,
                "window_size": bounded_trajectory_window,
                "derived_from": "event_log",
            },
        }

    def _empty_organism(self, signal: SignalState) -> dict[str, Any]:
        return {
            "identity": {
                "name": "Runtime",
                "mode": "taskbot",
                "posture": "RESPONSIVE",
                "attention_pressure": 0.0,
                "seed_hash": "",
                "traits": {},
                "calibrations": {},
                "confirmed_traits": {},
            },
            "affective_state": self._signal_snapshot(signal),
            "stm": {
                "frames": [],
                "notes": [],
                "semantic_items": [],
                "window_size": DEFAULT_STM_WINDOW,
            },
            "trajectory": {
                "window": [],
                "window_size": DEFAULT_TRAJECTORY_WINDOW,
                "derived_from": "event_log",
            },
        }

    def _affective_trajectory_window(self, *, window: int) -> list[dict[str, Any]]:
        events = _load_events(self.log_path)
        if not events:
            return []

        seq_to_event_id: dict[int, str] = {}
        for event in events:
            seq = event.get("seq")
            event_id = event.get("event_id")
            if isinstance(seq, int) and isinstance(event_id, str) and event_id:
                seq_to_event_id[seq] = event_id

        signal = SignalState()
        points: list[dict[str, Any]] = []

        for event in events:
            kind = str(event.get("kind", ""))
            details = event.get("details", {})
            if not isinstance(details, dict):
                continue

            delta: dict[str, Any] | None = None
            if kind == "signal.shift":
                shift_delta: dict[str, float] = {}
                for name in sorted(details.keys()):
                    if name not in signal.core:
                        continue
                    raw = details.get(name)
                    if isinstance(raw, bool):
                        continue
                    if isinstance(raw, (int, float)):
                        value = float(raw)
                    else:
                        try:
                            value = float(raw)
                        except (TypeError, ValueError):
                            continue
                    signal.shift(name, value)
                    shift_delta[name] = value
                if shift_delta:
                    delta = shift_delta
            elif kind == "signal.decay":
                raw_factor = details.get("factor", 0.98)
                factor = float(raw_factor) if isinstance(raw_factor, (int, float)) else 0.98
                signal.decay(factor)
                delta = {"factor": factor}

            if delta is None:
                continue

            seq = event.get("seq")
            parent_seq = event.get("parent_seq")
            points.append(
                {
                    "t": float(event.get("timestamp", 0.0) or 0.0),
                    "seq": int(seq) if isinstance(seq, int) else -1,
                    "kind": kind,
                    "module": str(event.get("module", "")),
                    "cause": self._classify_trajectory_cause(event),
                    "source_operation": self._source_operation(event),
                    "source_parent_operation": seq_to_event_id.get(parent_seq) if isinstance(parent_seq, int) else None,
                    "delta": delta,
                    "state": self._signal_snapshot(signal),
                }
            )

        return points[-window:]

    @staticmethod
    def _sorted_dict(payload: dict[str, Any]) -> dict[str, Any]:
        return {str(key): payload[key] for key in sorted(payload.keys(), key=str)}

    @staticmethod
    def _signal_snapshot(signal: SignalState) -> dict[str, Any]:
        return {
            "core": {name: float(signal.core[name]) for name in sorted(signal.core.keys())},
            "valence": float(signal.valence),
            "arousal": float(signal.arousal),
            "instability": float(signal.instability),
            "stage": str(signal.stage),
            "trace_len": len(signal.trace),
        }

    @staticmethod
    def _classify_trajectory_cause(event: dict[str, Any]) -> str:
        kind = str(event.get("kind", ""))
        module = str(event.get("module", ""))
        if kind == "signal.decay":
            return "self_regulation"
        if kind == "signal.shift" and module in {"identity_engine", "policy_engine"}:
            return "governance_modulation"
        if kind == "signal.shift":
            return "interaction_modulation"
        return "unknown"

    @staticmethod
    def _source_operation(event: dict[str, Any]) -> str:
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id:
            return event_id
        seq = event.get("seq")
        if isinstance(seq, int):
            return f"seq:{seq}"
        return "unknown"

    @staticmethod
    def _first_divergence(cycles: list[dict[str, Any]]) -> dict[str, Any] | None:
        for cycle in cycles:
            if cycle.get("only_in_a"):
                return {"cycle": cycle.get("cycle"), "type": "only_in_a"}
            if cycle.get("only_in_b"):
                return {"cycle": cycle.get("cycle"), "type": "only_in_b"}
            deltas = cycle.get("deltas", [])
            if deltas:
                return {
                    "cycle": cycle.get("cycle"),
                    "type": "field_delta",
                    "fields": [str(delta.get("field", "")) for delta in deltas],
                }
        return None
