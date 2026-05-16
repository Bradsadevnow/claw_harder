from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable, Mapping

from .memory import MemoryFrame
from .model import BaseModel, ModelResponse
from .tools import ToolRegistry


FORBIDDEN_HINT_TERMS = frozenset(
    {
        "active",
        "allowed",
        "blocked",
        "builder",
        "confirmed",
        "denied",
        "executed",
        "policy_engine",
        "pending",
        "plan",
        "user",
    }
)

DEFAULT_MOOD = "steady"
DEFAULT_TTL = 2.0
MIN_TTL = 0.5
MAX_TTL = 8.0

DEFAULT_GLYPHS: dict[str, str] = {
    "bright": "^-^",
    "curious": "o_o",
    "guarded": "-_-",
    "intent": ">_<",
    "steady": ". .",
    "thinking": "...",
}


@dataclass(frozen=True)
class ExpressionPayload:
    mood: str
    glyph: str
    hint: str | None = None
    color: str | None = None
    presence: str | None = None
    ttl: float = DEFAULT_TTL


@dataclass
class ExpressionFrame:
    mood: str
    glyph: str
    hint: str | None = None
    color: str | None = None
    presence: str | None = None
    ttl: float = DEFAULT_TTL
    created_at: float = field(default_factory=monotonic)

    def expired(self, now: float) -> bool:
        return now > self.created_at + self.ttl


class MorphChannel:
    """Ephemeral expression carrier for the shell layer only.

    This channel must never be serialized, emitted as an event, or consulted by
    governance. It exists solely for transient operator-facing expression.
    """

    def __init__(self, *, now_fn: Callable[[], float] | None = None) -> None:
        self._now_fn = now_fn or monotonic
        self._frame: ExpressionFrame | None = None
        self._last_presence: str | None = None

    def publish(self, payload: ExpressionPayload | Mapping[str, Any] | None) -> ExpressionFrame | None:
        frame = sanitize_expression_payload(payload, now=self._now_fn())
        if frame:
            if frame.presence is not None:
                # Persistent presence update
                self._last_presence = frame.presence
            self._frame = frame
        return frame

    def publish_derived(self, response: ModelResponse) -> ExpressionFrame | None:
        if isinstance(response.expression, Mapping):
            return self.publish(response.expression)
        return self.publish(derive_expression_payload(response))

    def current(self) -> ExpressionFrame | None:
        if self._frame is None:
            return None
        if self._frame.expired(self._now_fn()):
            self._frame = None
            return None
        # Inject the last persistent presence into the ephemeral frame for rendering
        if self._frame and self._last_presence and self._frame.presence is None:
            return ExpressionFrame(
                mood=self._frame.mood,
                glyph=self._frame.glyph,
                hint=self._frame.hint,
                color=self._frame.color,
                presence=self._last_presence,
                ttl=self._frame.ttl,
                created_at=self._frame.created_at
            )
        return self._frame

    def clear(self) -> None:
        self._frame = None


class ExpressionTapModel(BaseModel):
    """View-layer model wrapper that siphons transient expression into MorphChannel.

    The wrapped runtime still receives a normal `ModelResponse`. If the underlying
    model ever produces an expression payload, it is stripped before the response
    leaves this wrapper so the runtime never stores or logs it.
    """

    def __init__(self, base: BaseModel, channel: MorphChannel) -> None:
        self._base = base
        self._channel = channel

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def descriptor(self) -> dict[str, str]:
        return self._base.descriptor()

    def is_available(self) -> bool:
        return self._base.is_available()

    def generate(
        self,
        system_prompt: str,
        history: list[MemoryFrame],
        registry: ToolRegistry,
        generation_controls: dict[str, Any] | None = None,
    ) -> ModelResponse:
        response = self._base.generate(
            system_prompt,
            history,
            registry,
            generation_controls=generation_controls,
        )
        self._channel.publish_derived(response)
        if response.expression is None:
            return response
        return ModelResponse(
            text=response.text,
            tool_calls=list(response.tool_calls),
            issues=list(response.issues),
            expression=None,
        )


def derive_expression_payload(response: ModelResponse) -> ExpressionPayload:
    lowered = response.text.lower()
    tool_count = len(response.tool_calls)
    issue_count = len(response.issues)

    if tool_count > 0:
        return ExpressionPayload(
            mood="intent",
            glyph=DEFAULT_GLYPHS["intent"],
            hint="lining up the next move",
            color="cyan",
            ttl=2.8,
        )
    if any(token in lowered for token in ("can't", "cannot", "unable", "blocked", "deny", "denied")):
        return ExpressionPayload(
            mood="guarded",
            glyph=DEFAULT_GLYPHS["guarded"],
            hint="staying inside the rails",
            color="yellow",
            ttl=3.0,
        )
    if "?" in response.text:
        return ExpressionPayload(
            mood="curious",
            glyph=DEFAULT_GLYPHS["curious"],
            hint="holding open a question",
            color="cyan",
            ttl=2.4,
        )
    if "!" in response.text:
        return ExpressionPayload(
            mood="bright",
            glyph=DEFAULT_GLYPHS["bright"],
            hint="leaning into it",
            color="green",
            ttl=2.0,
        )
    if issue_count > 0:
        return ExpressionPayload(
            mood="thinking",
            glyph=DEFAULT_GLYPHS["thinking"],
            hint="working through it",
            color="purple",
            ttl=2.5,
        )
    return ExpressionPayload(
        mood="steady",
        glyph=DEFAULT_GLYPHS["steady"],
        hint="staying with it",
        color="purple-dim",
        ttl=1.8,
    )


def sanitize_expression_payload(
    payload: ExpressionPayload | Mapping[str, Any] | None,
    *,
    now: float,
) -> ExpressionFrame | None:
    if payload is None:
        return None
    if isinstance(payload, ExpressionPayload):
        raw_mood = payload.mood
        raw_glyph = payload.glyph
        raw_hint = payload.hint
        raw_ttl = payload.ttl
    else:
        raw_mood = str(payload.get("mood", DEFAULT_MOOD))
        raw_glyph = str(payload.get("glyph", DEFAULT_GLYPHS[DEFAULT_MOOD]))
        raw_hint = payload.get("hint")
        raw_ttl = payload.get("ttl", DEFAULT_TTL)

    mood = _normalize_mood(raw_mood)
    glyph = _normalize_glyph(raw_glyph) or DEFAULT_GLYPHS.get(mood, DEFAULT_GLYPHS[DEFAULT_MOOD])
    hint = _normalize_hint(raw_hint)
    color = str(payload.get("color", "")) if isinstance(payload, dict) else (payload.color if isinstance(payload, ExpressionPayload) else None)
    presence = _normalize_presence(payload.get("presence") if isinstance(payload, dict) else (payload.presence if isinstance(payload, ExpressionPayload) else None))
    ttl = _normalize_ttl(raw_ttl)
    return ExpressionFrame(mood=mood, glyph=glyph, hint=hint, color=color, presence=presence, ttl=ttl, created_at=now)


def _normalize_presence(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    # 50x50 constraint (approximate)
    lines = [line.rstrip()[:50] for line in value.splitlines()[:50]]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return None
    return "\n".join(lines)


def _normalize_mood(value: str) -> str:
    cleaned = "".join(ch for ch in value.strip().lower() if ch.isalnum() or ch in {"_", "-"})
    if not cleaned:
        return DEFAULT_MOOD
    return cleaned[:24]


def _normalize_glyph(value: str) -> str:
    lines = [line.rstrip()[:18] for line in str(value).splitlines()[:5]]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines)


def _normalize_hint(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    hint = " ".join(value.split()).strip()
    if not hint:
        return None
    lowered = hint.lower()
    tokens = {
        "".join(ch for ch in word if ch.isalnum())
        for word in lowered.split()
    }
    if any(term in tokens for term in FORBIDDEN_HINT_TERMS):
        return None
    return hint[:80]


def _normalize_ttl(value: Any) -> float:
    try:
        ttl = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TTL
    if ttl < MIN_TTL:
        return MIN_TTL
    if ttl > MAX_TTL:
        return MAX_TTL
    return ttl
