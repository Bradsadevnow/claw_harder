from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4

from .runtime import RuntimeRuntime, ToolBindingAttempt
from .model import ModelResponse, MemoryFrame
from .traits.validator import IDENTITY_TRAITS
from .replay import Replayer
from .sandbox import StateSandbox
from .state import DRIFT_THRESHOLD, MIN_SAMPLE


MEMORY_TYPES = tuple(sorted(list(IDENTITY_TRAITS) + ["contradiction"]))
DASHBOARD_ORDER = ("name", "purpose", "voice_style", "voice_avoid", "values", "constraints", "contradiction")
SELF_REFERENCE_RE = re.compile(
    r"\b(i|i'm|im|i’ve|i'd|i'll|ive|me|my|mine|myself)\b",
    re.IGNORECASE,
)
LOGISTICAL_RE = re.compile(
    r"^(what time is it|open\b|show\b|read\b|list\b|run\b|write\b|delete\b|that code didn't work\b|fix\b)",
    re.IGNORECASE,
)


@dataclass
class SeedCandidate:
    id: str
    type: str
    content: str
    confidence: float
    source_message_id: str
    created_at: float = field(default_factory=time)
    original_content: str | None = None
    conflict_with_memory_id: str | None = None
    conflict_with_content: str | None = None

    def normalized_key(self) -> tuple[str, str]:
        return (self.type, _normalize_text(self.content))


class SeedMemoryStore:
    def __init__(self, sandbox: StateSandbox) -> None:
        self.sandbox = sandbox

    def list_bucket(self, memory_type: str) -> list[dict[str, Any]]:
        _require_memory_type(memory_type)
        result = self.sandbox.get_fact(_memory_bucket_key(memory_type))
        if not result.get("found"):
            return []
        value = result.get("value")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def list_all(self) -> dict[str, list[dict[str, Any]]]:
        return {
            memory_type: self.list_bucket(memory_type)
            for memory_type in MEMORY_TYPES
            if self.list_bucket(memory_type)
        }

    def confirm_candidate(
        self,
        candidate: SeedCandidate,
        refined_text: str | None = None,
    ) -> dict[str, Any]:
        _require_memory_type(candidate.type)
        now = time()
        item = {
            "id": f"{candidate.type}_{uuid4().hex[:10]}",
            "type": candidate.type,
            "content": (refined_text or candidate.content).strip(),
            "confidence": candidate.confidence,
            "status": "confirmed",
            "confirmed_at": now,
            "updated_at": now,
            "source_refs": [candidate.source_message_id],
        }
        if candidate.conflict_with_memory_id:
            item["conflict_with_memory_id"] = candidate.conflict_with_memory_id
        if refined_text:
            item["lineage"] = [{
                "original": candidate.original_content or candidate.content,
                "refined": refined_text,
                "timestamp": now,
                "source_message_id": candidate.source_message_id,
            }]

        bucket = self.list_bucket(candidate.type)
        bucket.append(item)
        self.sandbox.set_fact(_memory_bucket_key(candidate.type), bucket)
        return item


class SeedRuntime(RuntimeRuntime):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Compatibility shim only. Identity authority is the event log projection.
        self.store = SeedMemoryStore(self.sandbox)
        self.pending_candidates: list[SeedCandidate] = []
        self.active_confirmation: SeedCandidate | None = None
        self._replayer = Replayer()

    def projected_identity_buckets(self) -> dict[str, list[dict[str, Any]]]:
        state = self._replayer.replay(self.log_path)
        buckets: dict[str, list[dict[str, Any]]] = {}
        for key, value in state.identity.memory_buckets.items():
            if not isinstance(value, list):
                continue
            rows = [dict(item) for item in value if isinstance(item, dict)]
            if rows:
                buckets[str(key)] = rows
        return buckets

    def projected_identity_bucket(self, memory_type: str) -> list[dict[str, Any]]:
        _require_memory_type(memory_type)
        return list(self.projected_identity_buckets().get(memory_type, []))

    def get_health_signal(self) -> dict[str, Any]:
        """Return structured health data including projected identity node counts."""
        base_health = super().get_health_signal()
        confirmed_buckets = self.projected_identity_buckets()
        confirmed_count = sum(len(bucket) for bucket in confirmed_buckets.values())
        provisional_count = len(self.pending_candidates)
        total = confirmed_count + provisional_count
        drift_ratio = (provisional_count / total) if total > 0 else 0.0

        drift = False
        if total >= MIN_SAMPLE:
            drift = drift_ratio > DRIFT_THRESHOLD

        issue_reasons = [
            str(reason)
            for reason in base_health.get("issue_reasons", [])
            if str(reason).strip()
        ]
        if drift:
            issue_reasons.append("identity_drift_threshold_exceeded")
            issue_reasons.append("unconsolidated_identity_proposals")

        return {
            "cycle": self.state.tick_count,
            "active_nodes": confirmed_count,
            "provisional_nodes": provisional_count,
            "drift": drift,
            "drift_ratio": drift_ratio,
            "drift_threshold": DRIFT_THRESHOLD,
            "issue": bool(base_health.get("issue", False) or drift),
            "issue_reasons": sorted(set(issue_reasons)),
            "status": "degraded" if issue_reasons else "healthy",
            "killswitch_engaged": bool(base_health.get("killswitch_engaged", False)),
            "killswitch_reason": str(base_health.get("killswitch_reason", "")),
            "signal": self.state.signal.core if hasattr(self.state, "signal") else {},
        }

    def before_pulse(self, user_input: str) -> None:
        """Preprocessing: Handle identity confirmation if active, or extract new candidates."""
        if self.active_confirmation and user_input.lower() in ("y", "yes", "confirm", "approve"):
            self.emit_identity_proposed(self.active_confirmation)
            if self.pending_candidates and self.pending_candidates[0].normalized_key() == self.active_confirmation.normalized_key():
                self.pending_candidates.pop(0)
            self.active_confirmation = None
            return

        if self.active_confirmation and user_input.lower() in ("n", "no", "deny", "reject"):
            self._emit(
                "seed.identity_rejected", 
                "seed", 
                f"Identity candidate rejected: {self.active_confirmation.content}"
            )
            if self.pending_candidates and self.pending_candidates[0].normalized_key() == self.active_confirmation.normalized_key():
                self.pending_candidates.pop(0)
            self.active_confirmation = None
            return

        message_id = f"msg_{int(time())}"
        candidates = self._extract_candidates(user_input, message_id)
        for candidate in candidates:
            self._queue_candidate(candidate)

    def emit_calibration_updated(self, key: str, value: str) -> None:
        self._emit(
            "seed.calibration_updated",
            "seed",
            f"Calibration updated: {key}",
            details={"key": key, "value": value}
        )

    def emit_identity_proposed(self, candidate: SeedCandidate, refined_text: str | None = None) -> dict[str, Any]:
        now = time()
        item = {
            "id": f"{candidate.type}_{uuid4().hex[:10]}",
            "type": candidate.type,
            "content": (refined_text or candidate.content).strip(),
            "confidence": candidate.confidence,
            "status": "proposed",
            "confirmed_at": now,
            "updated_at": now,
            "source_refs": [candidate.source_message_id],
        }
        if candidate.conflict_with_memory_id:
            item["conflict_with_memory_id"] = candidate.conflict_with_memory_id
        if refined_text:
            item["lineage"] = [{
                "original": candidate.original_content or candidate.content,
                "refined": refined_text,
                "timestamp": now,
                "source_message_id": candidate.source_message_id
            }]
            
        self._emit(
            "seed.identity_proposed",
            "seed",
            f"Identity memory proposed: {item['content']}",
            details={"type": candidate.type, "item": item}
        )
        return item

    def emit_identity_edited(self, memory_type: str, item_id: str, new_content: str) -> dict[str, Any]:
        item = {
            "type": memory_type,
            "id": item_id,
            "content": new_content.strip(),
            "updated_at": time()
        }
        self._emit(
            "seed.identity_edited",
            "seed",
            f"Identity memory {item_id} edited",
            details=item
        )
        return item

    def emit_identity_deleted(self, memory_type: str, item_id: str) -> dict[str, Any]:
        item = {"type": memory_type, "id": item_id}
        self._emit(
            "seed.identity_deleted",
            "seed",
            f"Identity memory {item_id} deleted",
            details=item
        )
        return item

    def after_model_proposal(self, response: ModelResponse) -> None:
        pass

    def after_governance(self, tool_plans: list[ToolBindingAttempt]) -> None:
        pass

    def after_pulse(self, assistant_response: str) -> None:
        """Postprocessing: Queue identity confirmation prompt if needed."""
        if self.active_confirmation:
            return
            
        if self.pending_candidates:
            candidate = self.pending_candidates[0]
            
            operator_authority = getattr(self.state.governance, "operator_authority", "user")
            if operator_authority == "builder":
                self._emit(
                    "seed.identity_auto_confirmed",
                    "seed",
                    f"Identity auto-confirmed (builder): {candidate.content}",
                    details={"candidate": asdict(candidate)}
                )
                self.emit_identity_proposed(candidate)
                self.pending_candidates.pop(0)
                return

            self.active_confirmation = candidate
            self._emit(
                "seed.identity_confirmation_started",
                "seed",
                f"Starting confirmation for: {self.active_confirmation.content}",
                details={"type": self.active_confirmation.type}
            )
            prompt = f"\n[IDENTITY_CONFIRMATION] I noticed something about you: \"{self.active_confirmation.content}\". Is this accurate? (y/n)"
            self.extra_response_lines.append(prompt)

    def _extract_candidates(self, user_input: str, message_id: str) -> list[SeedCandidate]:
        if looks_like_non_self_referential(user_input):
            return []
            
        heuristic = heuristic_candidates_from_text(user_input, source_message_id=message_id)
        if heuristic:
            return [self._apply_contradiction_detection(candidate) for candidate in heuristic]
            
        system_prompt = (
            "Analyze the user's statement and extract up to 2 candidate identity memories.\n\n"
            "Allowed types:\n"
            "- name\n"
            "- purpose\n"
            "- belief\n"
            "- preference\n"
            "- pattern\n"
            "- goal\n"
            "- contradiction\n\n"
            "Rules:\n"
            "- Be conservative. If unsure, return nothing.\n"
            "- Do not invent traits.\n"
            "- If the message is purely informational, logistical, or non-self-referential, return [].\n"
            "- Use the user's own language when possible.\n"
            "- Prefer specific over general.\n"
            "- Return JSON only.\n"
        )
        
        response_text = self._call_extraction_model(system_prompt, user_input)
        raw_items = parse_json_array(response_text)
        candidates = sanitize_candidate_payloads(raw_items, source_message_id=message_id)
        return [self._apply_contradiction_detection(candidate) for candidate in candidates]

    def _call_extraction_model(self, system_prompt: str, user_input: str) -> str:
        response = self.model.generate(
            system_prompt, 
            [MemoryFrame(role="user", content=user_input)],
            self.registry
        )
        self._emit(
            "seed.extraction_response",
            "seed",
            "Identity extraction model call completed.",
            details={"output_text": response.text}
        )
        return response.text

    def _apply_contradiction_detection(self, candidate: SeedCandidate) -> SeedCandidate:
        if candidate.type == "contradiction":
            return candidate

        projected_buckets = self.projected_identity_buckets()
        for memory_type in ("belief", "preference", "pattern", "goal"):
            bucket = projected_buckets.get(memory_type, [])
            for confirmed in bucket:
                verdict = self._detect_contradiction(candidate.content, str(confirmed.get("content", "")))
                if not verdict:
                    continue
                self._emit(
                    "seed.identity_contradiction_detected",
                    "seed",
                    f"Contradiction detected for candidate: {candidate.content}",
                    level="warning",
                    details={"original": candidate.content, "conflicts_with": confirmed.get("content", "")}
                )
                return SeedCandidate(
                    id=candidate.id,
                    type="contradiction",
                    content=f"{candidate.content} conflicts with confirmed memory '{confirmed.get('content', '')}'",
                    confidence=max(candidate.confidence, float(verdict.get("similarity", 0.0))),
                    source_message_id=candidate.source_message_id,
                    created_at=candidate.created_at,
                    original_content=candidate.content,
                    conflict_with_memory_id=str(confirmed.get("id", "")),
                    conflict_with_content=str(confirmed.get("content", "")),
                )
        return candidate

    def _detect_contradiction(self, candidate_text: str, confirmed_text: str) -> dict[str, Any] | None:
        if not confirmed_text.strip():
            return None
        system_prompt = (
            "Determine whether a new identity statement directly contradicts a confirmed memory.\n"
            "Return JSON only with keys: conflicts, similarity, opposite_polarity.\n"
            "Set conflicts true only when both statements are about the same subject and point in opposite directions.\n"
            "If uncertain, return conflicts false."
        )
        user_prompt = (
            f"Confirmed memory: {confirmed_text}\n"
            f"New candidate: {candidate_text}\n"
        )
        response = self.model.generate(
            system_prompt,
            [MemoryFrame(role="user", content=user_prompt)],
            self.registry
        )
        payload = parse_json_object(response.text)
        if not isinstance(payload, dict): return None
        try:
            conflicts = bool(payload.get("conflicts", False))
            similarity = float(payload.get("similarity", 0.0))
            opposite = bool(payload.get("opposite_polarity", False))
            if conflicts and similarity > 0.8 and opposite:
                return {"conflicts": True, "similarity": similarity, "opposite_polarity": True}
        except (ValueError, TypeError):
            pass
        return None

    def _queue_candidate(self, candidate: SeedCandidate) -> None:
        if self._has_confirmed_match(candidate):
            return
        pending_keys = {item.normalized_key() for item in self.pending_candidates}
        if candidate.normalized_key() in pending_keys:
            return
        self.pending_candidates.append(candidate)
        self._emit(
            "seed.identity_extracted",
            "seed",
            f"New identity candidate extracted: {candidate.content}",
            details={"type": candidate.type, "confidence": candidate.confidence}
        )

    def _has_confirmed_match(self, candidate: SeedCandidate) -> bool:
        for item in self.projected_identity_bucket(candidate.type):
            if _normalize_text(str(item.get("content", ""))) == _normalize_text(candidate.content):
                return True
        return False


def looks_like_non_self_referential(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if SELF_REFERENCE_RE.search(stripped):
        return False
    if LOGISTICAL_RE.match(lowered):
        return True
    if lowered.endswith("?") and len(lowered.split()) <= 6:
        return True
    return False


def sanitize_candidate_payloads(raw_items: Any, *, source_message_id: str) -> list[SeedCandidate]:
    if not isinstance(raw_items, list):
        return []
    candidates: list[SeedCandidate] = []
    for raw in raw_items:
        if len(candidates) >= 2:
            break
        if not isinstance(raw, dict):
            continue
        memory_type = str(raw.get("type", "")).strip().lower()
        if memory_type not in MEMORY_TYPES:
            continue
        content = str(raw.get("content", "")).strip()
        if not content:
            continue
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < 0.6:
            continue
        candidates.append(
            SeedCandidate(
                id=f"candidate_{uuid4().hex[:10]}",
                type=memory_type,
                content=content,
                confidence=max(0.0, min(1.0, confidence)),
                source_message_id=source_message_id,
            )
        )
    return candidates


def heuristic_candidates_from_text(text: str, *, source_message_id: str) -> list[SeedCandidate]:
    stripped = text.strip().rstrip(".!?")
    if not stripped or looks_like_non_self_referential(stripped):
        return []
    lowered = stripped.lower()
    memory_type = ""
    confidence = 0.72
    if re.search(r"\b(i keep|i always|i often|i usually|i tend to|i never)\b", lowered):
        memory_type = "constraints"
    elif re.search(r"\b(i want|i need|i'm trying to|im trying to|my goal is)\b", lowered):
        memory_type = "purpose"
    elif re.search(r"\b(i prefer|i like|i dislike|i hate)\b", lowered):
        memory_type = "values"
    elif re.search(r"\b(i believe|i think|i value)\b", lowered):
        memory_type = "values"
    if not memory_type:
        return []
    content = re.sub(r"^\s*i\s+", "", stripped, flags=re.IGNORECASE).strip()
    if not content:
        return []
    return [
        SeedCandidate(
            id=f"candidate_{uuid4().hex[:10]}",
            type=memory_type,
            content=content,
            confidence=confidence,
            source_message_id=source_message_id,
        )
    ]


def parse_json_array(text: str) -> list[Any]:
    payload = parse_json_value(text)
    return payload if isinstance(payload, list) else []


def parse_json_object(text: str) -> dict[str, Any] | None:
    payload = parse_json_value(text)
    return payload if isinstance(payload, dict) else None


def parse_json_value(text: str) -> Any:
    stripped = strip_markdown_fences(text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    extracted = _extract_balanced_json_value(stripped)
    if extracted is None:
        return None
    try:
        return json.loads(extracted)
    except json.JSONDecodeError:
        return None


def strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _extract_balanced_json_value(text: str) -> str | None:
    start_indices = [text.find("["), text.find("{")]
    valid_starts = [i for i in start_indices if i != -1]
    if not valid_starts:
        return None
    start = min(valid_starts)
    opening = text[start]
    closing = "]" if opening == "[" else "}"
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _memory_bucket_key(memory_type: str) -> str:
    return f"seed.identity.{memory_type}"


def _require_memory_type(memory_type: str) -> None:
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"unknown memory type: {memory_type}")


def _normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def _format_timestamp(raw_value: Any) -> str:
    if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
        return ""
    return f"@ {time_to_iso(raw_value)}"


def time_to_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def cap_sentences(text: str, max_sentences: int = 3) -> str:
    parts = [s.strip() for s in text.split(".") if s.strip()]
    if not parts:
        return ""
    res = ". ".join(parts[:max_sentences])
    if not res.endswith("."):
        res += "."
    return res


def finalize_response_text(response: ModelResponse) -> str:
    text = (response.text or "").strip()
    if not text:
        return "Tell me a little more about that."
    text = strip_markdown_fences(text)
    return cap_sentences(text, 3) or "Tell me a little more about that."
