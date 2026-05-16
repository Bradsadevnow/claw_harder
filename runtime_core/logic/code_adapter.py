import re
from typing import Dict, List, Optional, Any
from .domain_adapter import DomainAdapter, DomainSnapshot

class CodeExecutionAdapter(DomainAdapter):
    """
    Adapter for the Code Execution domain.
    Grounds claims in actual test results.
    """

    @property
    def domain_name(self) -> str:
        return "code_execution"

    def extract_snapshot(self, monologue: str, current_working: Any) -> DomainSnapshot:
        snapshot = DomainSnapshot()
        
        # Extract variables: TestPassRate: 80%, Complexity: 15
        patterns = {
            "TestPassRate": r"(?i)TestPassRate\s*[:=]\s*(\d+)\s*(?:[\u202f\s])*%?",
            "Complexity": r"(?i)Complexity\s*[:=]\s*(\d+)",
            "LintErrors": r"(?i)LintErrors\s*[:=]\s*(\d+)"
        }
        
        for var, pattern in patterns.items():
            m = re.search(pattern, monologue)
            if m:
                snapshot.variables[var] = float(m.group(1))

        return snapshot

    def validate_snapshot(self, snapshot: DomainSnapshot, current_working: Any) -> List[str]:
        violations = []
        
        # HARD GROUNDING: Verify against "Ground Truth" from previous tool executions
        # In a real system, we'd pass the event ledger or a state summary here.
        # For now, we look for a 'last_test_result' injected into working state 
        # or mock the retrieval.
        
        ground_truth = getattr(current_working, "ground_truth", {})
        actual_pass_rate = ground_truth.get("TestPassRate")
        
        if actual_pass_rate is not None:
            claimed_pass_rate = snapshot.variables.get("TestPassRate")
            if claimed_pass_rate is not None:
                if abs(claimed_pass_rate - actual_pass_rate) > 0.01:
                    violations.append(
                        f"Truth Integrity Failure: Model claims {claimed_pass_rate}% pass rate, "
                        f"but actual execution result was {actual_pass_rate}%."
                    )
        
        return violations

    def get_initial_state(self) -> Dict[str, Any]:
        return {
            "TestPassRate": 0.0,
            "Complexity": 0.0,
            "LintErrors": 0.0
        }
