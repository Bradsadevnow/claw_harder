from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..memory import MemoryStore


@dataclass
class RecognitionDecision:
    recognized: bool
    intent: str
    confidence: float
    familiarity: float
    should_bypass_reasoning: bool
    response_text: str | None
    rationale: str
    normalized_input: str


class RecognitionValidator:
    """
    Deterministic recognition/classification layer.
    Runs after salience and before model reasoning.
    """

    _TEMPLATES: tuple[tuple[str, tuple[str, ...], str], ...] = (
        ("heartbeat_ping", ("ping", "heartbeat", "status_check_only"), "ack"),
        ("gratitude", ("thanks", "thank you", "thx", "ty"), "Anytime. Ready when you are."),
        ("greeting", ("hi", "hey", "hello", "yo"), "Here and listening."),
    )

    def evaluate(
        self,
        user_input: str,
        memory: MemoryStore | None,
        *,
        salience_posture: str,
    ) -> RecognitionDecision:
        normalized = self._normalize(user_input)
        familiarity = self._familiarity(normalized, memory)
        posture = (salience_posture or "").strip().lower()

        template = self._match_template(normalized)
        if template is not None:
            intent, response_text = template
            should_bypass = posture != "deliberative"
            return RecognitionDecision(
                recognized=True,
                intent=intent,
                confidence=1.0,
                familiarity=max(0.8, familiarity),
                should_bypass_reasoning=should_bypass,
                response_text=response_text,
                rationale="deterministic_template_match",
                normalized_input=normalized,
            )

        # Familiar repeat heuristic: only short, previously seen non-question prompts.
        is_question = "?" in user_input
        if familiarity >= 1.0 and len(normalized) <= 24 and not is_question and posture in {"latent", "responsive"}:
            return RecognitionDecision(
                recognized=True,
                intent="familiar_repeat",
                confidence=0.75,
                familiarity=familiarity,
                should_bypass_reasoning=True,
                response_text="Acknowledged.",
                rationale="repeat_familiarity_short_prompt",
                normalized_input=normalized,
            )

        return RecognitionDecision(
            recognized=False,
            intent="unknown",
            confidence=0.0,
            familiarity=familiarity,
            should_bypass_reasoning=False,
            response_text=None,
            rationale="no_deterministic_match",
            normalized_input=normalized,
        )

    def _match_template(self, normalized_input: str) -> tuple[str, str] | None:
        for intent, variants, response in self._TEMPLATES:
            if normalized_input in variants:
                return intent, response
        return None

    def _familiarity(self, normalized_input: str, memory: MemoryStore | None) -> float:
        if memory is None:
            return 0.0
        prior_user_inputs = [self._normalize(frame.content) for frame in memory.frames if frame.role == "user"]
        if prior_user_inputs and prior_user_inputs[-1] == normalized_input:
            prior_user_inputs = prior_user_inputs[:-1]
        if not prior_user_inputs:
            return 0.0
        matches = sum(1 for value in prior_user_inputs if value == normalized_input)
        return min(1.0, matches / 2.0)

    @staticmethod
    def _normalize(text: Any) -> str:
        lowered = str(text or "").strip().lower()
        collapsed = re.sub(r"[^a-z0-9\s]", "", lowered)
        collapsed = re.sub(r"\s+", " ", collapsed).strip()
        return collapsed
