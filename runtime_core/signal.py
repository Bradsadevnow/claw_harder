from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SignalState:
    core: dict[str, float] = field(
        default_factory=lambda: {
            "joy": 0.4,
            "sadness": 0.0,
            "anger": 0.0,
            "fear": 0.0,
            "trust": 0.7,
            "surprise": 0.0,
            "anticipation": 0.0,
            "calm": 0.5,
            "curiosity": 0.8,
            "gratitude": 0.0,
            "wonder": 0.6,
            "resolve": 0.3,
            "focus": 0.5,
            "frustration": 0.0,
            "serenity": 0.0,
            "bond": 0.0,
            "anxiety": 0.0,
            "threat": 0.1,
            "grief": 0.1,
        }
    )
    valence: float = 0.5 # -1.0 to 1.0
    arousal: float = 0.3 # 0.0 to 1.0
    instability: float = 0.1 # 0.0 to 1.0
    trace: list[tuple[str, float]] = field(default_factory=list)

    def set(self, name: str, value: float) -> None:
        """Absolute setter for a trait."""
        clamped = max(0.0, min(1.0, value))
        self.core[name] = clamped
        self.trace.append((name, clamped))

    def shift(self, name: str, delta: float) -> None:
        """Relative shift for a trait."""
        old = self.core.get(name, 0.0)
        new = max(0.0, min(1.0, old + delta))
        self.core[name] = new
        self.trace.append((name, new))

    def decay(self, factor: float = 0.98) -> None:
        for key, value in list(self.core.items()):
            self.core[key] = round(value * factor, 4)

    @property
    def stage(self) -> str:
        # Surge if high energy/activation
        if self.core.get("threat", 0.0) > 0.7 or self.core.get("anger", 0.0) > 0.7:
            return "Surge"
        # Flow if wonder or curiosity is high
        if self.core.get("wonder", 0.0) > 0.7 or self.core.get("curiosity", 0.0) > 0.75:
            return "Flow"
        return "Calm"

    def heartbeat(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "core": dict(self.core),
            "trace_len": len(self.trace),
        }
