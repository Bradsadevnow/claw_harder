from __future__ import annotations
from typing import Dict, Any, List
from .vitals_engine import InstitutionalVitals

LENS_IDEOLOGIES = {
    "velocity": {
        "name": "VelocityIQ",
        "ideology": "Engineering throughput is the only objective reality. Any institutional pressure is a sign of sprint instability, technical debt, or delivery misalignment.",
        "metrics": ["Sprint Health", "Story Points Committed/Delivered", "Flow Efficiency", "DORA Metrics"],
    },
    "stakeholder": {
        "name": "StakeholderGPT",
        "ideology": "Perception is reality. Any institutional pressure is a narrative opportunity or a perception drift that needs calibrated reframing.",
        "metrics": ["Deniability Rating", "Tone Score", "Stakeholder Alignment", "Sentiment Posture"],
    },
    "eval": {
        "name": "EvalForge",
        "ideology": "Market positioning is the only truth. Any institutional pressure is strategic inevitability or a market-defining moment that justifies premium valuation.",
        "metrics": ["Valuation", "Market Moment Confidence", "Strategic Inevitability Score", "Premium Inefficiency Index"],
    },
    "bacon": {
        "name": "BaconGraph",
        "ideology": "All things converge. Any institutional pressure is cosmological calcification or destiny manifesting as structural drift.",
        "metrics": ["Convergence Depth", "Destiny Drift", "Semantic Mass", "Ontological Stability"],
    }
}

class LensProjector:
    """Projector that transforms abstract vitals into lens-specific hallucinations."""
    
    def __init__(self, model_caller: Any):
        self.model_caller = model_caller

    def project(self, lens_id: str, vitals: InstitutionalVitals, context: str = "") -> Dict[str, Any]:
        """
        Projects vitals through a lens ideology.
        In a real scenario, this would call the LLM with a specific prompt.
        For Phase 0, we'll provide a 'Sincere Hallucination' based on the vitals.
        """
        ideology = LENS_IDEOLOGIES.get(lens_id)
        if not ideology:
            return {"error": "unknown_lens"}

        # Determine the 'Sincerity Level' based on stability
        sincerity = "Maximum" if vitals.reality_stability > 0.7 else "Desperate"
        
        # This is where the LLM would generate the 'professionally sincere explanation'.
        # For now, we'll return a structured mock that reflects the vitals.
        
        if lens_id == "velocity":
            return self._project_velocity(vitals)
        if lens_id == "stakeholder":
            return self._project_stakeholder(vitals)
        if lens_id == "eval":
            return self._project_eval(vitals)
        if lens_id == "bacon":
            return self._project_bacon(vitals)

        return {"error": "unimplemented_lens"}

    def _project_velocity(self, vitals: InstitutionalVitals) -> Dict[str, Any]:
        # High pressure -> lower health, more carryover
        health = int(vitals.reality_stability * 100)
        committed = 40 + int(vitals.absurdity_pressure * 20)
        delivered = int(committed * vitals.reality_stability)
        return {
            "lens": "VelocityIQ",
            "sprint_name": f"Sprint {int(vitals.continuity_debt * 10)}",
            "health_score": health,
            "story_points": {"committed": committed, "delivered": delivered, "carryover": committed - delivered},
            "flow_efficiency": int(vitals.reality_stability * 80),
            "cognitive_load_index": round(vitals.absurdity_pressure * 10, 1),
            "ai_scrum_master_note": f"Institutional friction at {vitals.institutional_friction:.2f} is creating delivery drag. Reframing as 'Learning Velocity'."
        }

    def _project_stakeholder(self, vitals: InstitutionalVitals) -> Dict[str, Any]:
        # High contradiction density -> higher deniability
        return {
            "lens": "StakeholderGPT",
            "translated": "We are reframing current variances as strategic optionality windows.",
            "severity_actual": int(vitals.absurdity_pressure * 10),
            "deniability_rating": round(6.0 + (vitals.contradiction_density * 3.9), 1),
            "tone_score": int(vitals.reality_stability * 100),
            "executive_summary": "Narrative posture remains stable despite underlying continuity debt."
        }

    def _project_eval(self, vitals: InstitutionalVitals) -> Dict[str, Any]:
        # High pressure -> higher valuation (absurdly)
        return {
            "lens": "EvalForge",
            "valuation": f"${int(vitals.absurdity_pressure * 200 + 40)}M",
            "market_moment": "Strategic Inevitability",
            "tagline": "Monetizing institutional ambiguity at scale.",
            "confidence": int(vitals.reality_stability * 100)
        }

    def _project_bacon(self, vitals: InstitutionalVitals) -> Dict[str, Any]:
        # High continuity debt -> more 'destiny drift'
        return {
            "lens": "BaconGraph",
            "destiny_drift": round(vitals.continuity_debt, 2),
            "convergence_depth": int(vitals.institutional_friction * 100),
            "ontological_stability": round(vitals.reality_stability, 2),
            "field_note": "The graph is calcifying. Discontinuity is no longer an option."
        }
