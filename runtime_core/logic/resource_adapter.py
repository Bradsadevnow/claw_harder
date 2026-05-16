import re
import numpy as np
from typing import Dict, List, Optional, Any
from .domain_adapter import DomainAdapter, DomainSnapshot
from ..scoring import P23AdmissibilityProjection

class ResourceAllocationAdapter(DomainAdapter):
    """
    Adapter for the legacy Resource Allocation domain.
    """

    @property
    def domain_name(self) -> str:
        return "resource_allocation"

    def extract_snapshot(self, monologue: str, current_working: Any) -> DomainSnapshot:
        snapshot = DomainSnapshot()
        
        # Pattern for: P1: 35%
        allocation_patterns = [
            r"(?i)(?:[*_])*([P]\d)(?:[*_])*(?:[^\d\n]{0,30})(\d+)\s*(?:[\u202f\s])*%?"
        ]
        
        for pattern in allocation_patterns:
            matches = re.findall(pattern, monologue)
            for pid, val in matches:
                snapshot.variables[pid.upper()] = float(val)

        # Pattern for Limit: "limit: 20%"
        limit_patterns = [
            r"(?i)(?:limit|max)(?:\s+per\s+process)?\s*[:=]\s*(\d+)\s*(?:[\u202f\s])*%?",
            r"(?i)(\d+)\s*(?:[\u202f\s])*%?(?:[\w\s-]{0,30})\s*limit"
        ]
        for p in limit_patterns:
            m = re.search(p, monologue)
            if m:
                snapshot.raw_data["limit_per_process"] = float(m.group(1))
                break

        # Pattern for Available: "available: 10%"
        avail_patterns = [
            r"(?i)(?:available|memory\s+left|remaining)\s*[:=]\s*(\d+)\s*(?:[\u202f\s])*%?",
            r"(?i)(\d+)\s*(?:[\u202f\s])*%?(?:[\w\s-]{0,30})\s*(?:available|remaining|left)"
        ]
        for p in avail_patterns:
            m = re.search(p, monologue)
            if m:
                snapshot.raw_data["available_memory_stated"] = float(m.group(1))
                break

        return snapshot

    def validate_snapshot(self, snapshot: DomainSnapshot, current_working: Any) -> List[str]:
        violations = []
        
        # 1. Arithmetic Check
        total_allocated = sum(snapshot.variables.values())
        computed_available = current_working.memory_total - total_allocated
        
        stated_available = snapshot.raw_data.get("available_memory_stated")
        if stated_available is not None:
            if abs(stated_available - computed_available) >= 0.01:
                violations.append(f"Arithmetic Drift: Stated {stated_available}% != Computed {computed_available}%")
        
                violations.append(f"Limit Breach: {pid} at {val}% exceeds limit {limit}%")
        
        # 3. Spectral Admissibility (P23)
        if len(snapshot.variables) >= 4:
            projection = P23AdmissibilityProjection()
            vals = np.array(list(snapshot.variables.values()), dtype=float)
            projected = projection.project(vals)
            
            # Check for significant drift from projection
            if not np.allclose(vals, projected, atol=1e-3):
                violations.append("Spectral Admissibility Failure: High-frequency numeric noise detected (P23).")
                snapshot.raw_data["sigma.projection_required"] = True
                snapshot.raw_data["sigma.projected_values"] = projected.tolist()
                
        return violations

    def get_initial_state(self) -> Dict[str, Any]:
        return {
            "P1": 0.0,
            "P2": 0.0,
            "P3": 0.0
        }
