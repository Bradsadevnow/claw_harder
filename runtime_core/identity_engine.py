from __future__ import annotations
import re
from typing import Any

class IdentityEngine:
    """
    The 'New Thalamus' — a pure proposal engine for Soulform identity.
    It analyzes cognition (monologue) and proposes signal/symbol events.
    """

    # Antagonist pairs from legacy Amygdala research
    ANTAGONISTS = {
        "joy": "sadness", "sadness": "joy",
        "anger": "calm", "calm": "anger",
        "fear": "resolve", "resolve": "fear",
        "anxiety": "serenity", "serenity": "anxiety",
        "trust": "surprise", "surprise": "trust",
        "frustration": "gratitude", "gratitude": "frustration",
        "focus": "curiosity", "curiosity": "focus",
        "bond": "anticipation", "anticipation": "bond",
        "wonder": "focus"
    }

    # Keywords that trigger emotional shifts
    EMOTION_KEYWORDS = {
        "wonder": ["wonder", "amaze", "incredible", "astound"],
        "curiosity": ["curious", "wonder why", "explore", "interest"],
        "resolve": ["resolve", "determined", "will", "must"],
        "anxiety": ["anxious", "worry", "uncertain", "fear"],
        "joy": ["joy", "happy", "delight", "wonderful"],
        "gratitude": ["thank", "grateful", "appreciate"],
        "frustration": ["frustrating", "annoy", "stuck", "fail"],
    }

    def analyze_monologue(self, monologue: str) -> dict[str, Any]:
        """
        Analyzes internal monologue to extract signal shifts and symbols.
        Identity updates are NO LONGER extracted here; they must use the tool.
        """
        proposal = {
            "signal_shifts": {},
            "symbols": [],
            "events": [],
            "paradox_detected": False,
            "paradox_reason": None,
            "raw_monologue": monologue
        }
        lowered = monologue.lower()

        # 0. Paradox Detection (Cognitive Dissonance)
        paradox_markers = ["contradiction", "confused", "paradox", "identity crisis"]
        for marker in paradox_markers:
            if marker in lowered:
                proposal["paradox_detected"] = True
                proposal["paradox_reason"] = f"Cognitive dissonance detected: '{marker}'"

        # 1. Emotional Analysis (Signal Shifts)
        shifts = {}
        for emotion, keywords in self.EMOTION_KEYWORDS.items():
            for kw in keywords:
                if kw in lowered:
                    shifts[emotion] = shifts.get(emotion, 0.0) + 0.05
                    opp = self.ANTAGONISTS.get(emotion)
                    if opp:
                        shifts[opp] = shifts.get(opp, 0.0) - 0.03
        
        proposal["signal_shifts"] = shifts
        return proposal

    def get_decay_proposal(self) -> dict[str, Any]:
        """Proposes a standard emotional decay event."""
        return {
            "kind": "signal.decay",
            "module": "identity_engine",
            "details": {"factor": 0.98}
        }
