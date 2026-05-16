from typing import Dict, Any, List, Optional, Literal
from dataclasses import dataclass
from ..model import ModelResponse

@dataclass
class SalienceAdjudication:
    posture: Literal["latent", "responsive", "focused", "deliberative"]
    priority: float
    reasoning_budget: float # 0.0 to 1.0
    deliberation_required: bool
    justification: str
    # Telemetry
    raw_posture: str = ""
    attention_pressure: float = 0.0
    decay_rate: float = 0.0
    hysteresis_applied: bool = False

class SalienceValidator:
    """
    The Pre-Cognitive Attention Filter.
    Now stateful: attention persists and decays based on arousal.
    """

    def __init__(self, model: Any = None):
        self.model = model

    def evaluate(self, user_input: str, identity_state: Any, arousal: float = 0.5) -> SalienceAdjudication:
        """
        Adjudicates 'Decision Pressure' based on input and current posture.
        """
        input_lower = user_input.lower()
        
        # 1. Calculate Heuristic Score (0.0 to 1.0)
        heuristic_score = 0.2 # Base baseline
        
        # Priority Keywords
        priority_keywords = ["urgent", "error", "security", "why", "homie", "soul", "architect", "logic", "verify", "repair"]
        if any(k in input_lower for k in priority_keywords):
            heuristic_score = max(heuristic_score, 0.8)
        elif "status" in input_lower:
            heuristic_score = max(heuristic_score, 0.5)
            
        # Complexity/Length
        if len(user_input) >= 150:
            heuristic_score = max(heuristic_score, 0.9)
        elif len(user_input) >= 50:
            heuristic_score = max(heuristic_score, 0.5)
            
        # Noise/Telemetry Reduction
        noise_keywords = ["ping", "telemetry", "heartbeat", "status_check_only"]
        if any(k == input_lower.strip() for k in noise_keywords):
            heuristic_score = 0.05

        # 2. Update Attention Pressure (Spike Logic)
        # attention_pressure = max(current_pressure, min(1.0, heuristic_score))
        current_pressure = getattr(identity_state, "attention_pressure", 0.0) if identity_state is not None else 0.0
        new_pressure = max(current_pressure, min(1.0, heuristic_score))
        
        # 3. Determine Raw Posture (Stateless)
        if heuristic_score >= 0.8:
            raw_posture = "deliberative"
        elif heuristic_score >= 0.5:
            raw_posture = "focused"
        elif heuristic_score >= 0.15:
            raw_posture = "responsive"
        else:
            raw_posture = "latent"
            
        # 4. Apply Posture Floor Rules (Hysteresis)
        final_posture = raw_posture
        hysteresis_applied = False
        
        if new_pressure >= 0.75:
            if final_posture in ["latent", "responsive", "focused"]:
                final_posture = "deliberative"
                hysteresis_applied = True
        elif new_pressure >= 0.50:
            if final_posture in ["latent", "responsive"]:
                final_posture = "focused"
                hysteresis_applied = True
        elif new_pressure >= 0.25:
            if final_posture == "latent":
                final_posture = "responsive"
                hysteresis_applied = True
                
        # 5. Determine Budget based on Effective Posture
        budget_map = {
            "deliberative": 1.0,
            "focused": 0.6,
            "responsive": 0.2,
            "latent": 0.0
        }
        
        # Update the identity state object directly (it will be saved by runtime)
        if identity_state is not None:
            identity_state.attention_pressure = new_pressure
            identity_state.posture = final_posture.upper()
        
        # Calculate decay rate for telemetry (actual decay happens in runtime cycle end)
        base_decay = 0.18
        min_decay = 0.05
        effective_decay = max(min_decay, base_decay * (1.0 - 0.5 * arousal))

        return SalienceAdjudication(
            posture=final_posture,
            priority=heuristic_score,
            reasoning_budget=budget_map[final_posture],
            deliberation_required=(final_posture in ["focused", "deliberative"]),
            justification=f"Adjudicated {final_posture} (Score: {heuristic_score:.2f}, Pressure: {new_pressure:.2f})",
            raw_posture=raw_posture,
            attention_pressure=new_pressure,
            decay_rate=effective_decay,
            hysteresis_applied=hysteresis_applied
        )
