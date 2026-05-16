from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any
from .canon_ledger import CanonLedger

@dataclass
class InstitutionalVitals:
    absurdity_pressure: float = 0.0
    contradiction_density: float = 0.0
    institutional_friction: float = 0.0
    reality_stability: float = 1.0
    continuity_debt: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "absurdity_pressure": round(self.absurdity_pressure, 3),
            "contradiction_density": round(self.contradiction_density, 3),
            "institutional_friction": round(self.institutional_friction, 3),
            "reality_stability": round(self.reality_stability, 3),
            "continuity_debt": round(self.continuity_debt, 3),
            # Bureaucracy Aliases
            "compliance_drift": round(self.contradiction_density, 3),
            "variance_density": round(self.institutional_friction, 3),
            "prestige_pressure": round(self.absurdity_pressure, 3),
            "institutional_stability": round(self.reality_stability, 3),
            "delivery_instability": round(1.0 - self.reality_stability, 3),
        }

class VitalsEngine:
    """Computes deterministic metrics from the state of the Canon Ledger."""
    
    @staticmethod
    def compute(ledger: CanonLedger) -> InstitutionalVitals:
        revisions = ledger.revisions
        if not revisions:
            return InstitutionalVitals()

        total_revisions = len(revisions)
        
        # 1. Contradiction Density
        contradiction_edges = 0
        for r in revisions:
            for edge in r.edges:
                if edge.get("type") == "contradiction":
                    contradiction_edges += 1
        
        density = contradiction_edges / total_revisions if total_revisions > 0 else 0.0
        
        # 2. Absurdity Pressure
        # Grows with total volume and density. 
        # Base pressure = log(count) + density
        import math
        pressure = (math.log(total_revisions + 1) * 0.2) + (density * 2.0)
        pressure = min(1.0, pressure)
        
        # 3. Institutional Friction
        # Depth of the reconciliation chains.
        # How many revisions have "reconciliation" or "supersession" edges.
        reconciliation_count = 0
        for r in revisions:
            for edge in r.edges:
                if edge.get("type") in ("reconciliation", "supersession"):
                    reconciliation_count += 1
        
        friction = reconciliation_count / total_revisions if total_revisions > 0 else 0.0
        
        # 4. Continuity Debt
        # Sum of "reinterpreted" flags + open contradictions (not yet reconciled)
        reinterpreted_count = 0
        for r in revisions:
            if "REINTERPRETED" in r.continuity_flags:
                reinterpreted_count += 1
        
        debt = (reinterpreted_count * 0.1) + (contradiction_edges * 0.5)
        
        # 5. Reality Stability
        # Inverse of pressure and instability
        stability = max(0.0, 1.0 - (pressure * 0.5) - (friction * 0.5))

        return InstitutionalVitals(
            absurdity_pressure=pressure,
            contradiction_density=density,
            institutional_friction=friction,
            reality_stability=stability,
            continuity_debt=debt
        )
