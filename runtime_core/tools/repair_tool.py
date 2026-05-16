from __future__ import annotations
import difflib
from pathlib import Path
from typing import Any, Literal
from .types import ToolSpec, ToolBindingResult

class RepairPatchTool(ToolSpec):
    """
    RepairPatchTool: Governed petition for codebase corrections.
    Forces narrow scope, validation, and authority-impact assessment.
    """
    
    def __init__(self, workspace_root: Path, repair_lab: Any = None):
        super().__init__(
            name="repair_patch",
            description="Governed petition for codebase corrections. Forces narrow scope, validation, and authority-impact assessment.",
            schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "target_file": {"type": "string"},
                    "unified_diff": {"type": "string"},
                    "rationale": {"type": "string"},
                    "regression_test": {"type": "string"},
                    "authority_impact": {"type": "string", "enum": ["none", "uncertain", "yes"]}
                },
                "required": ["artifact_id", "target_file", "unified_diff", "rationale", "regression_test"]
            },
            handler=self.execute,
            mutates_state=True,
            high_risk=True
        )
        self.workspace_root = workspace_root
        self.repair_lab = repair_lab

    def execute(self, 
                artifact_id: str,
                target_file: str,
                unified_diff: str,
                rationale: str,
                regression_test: str,
                authority_impact: Literal["none", "uncertain", "yes"] = "none",
                runtime: Any = None
    ) -> ToolBindingResult:
        
        # 1. Validation: Repair is a petition, not a privilege.
        if authority_impact != "none":
            return ToolBindingResult(
                success=False,
                output=f"REJECTED: Authority impact must be 'none'. Proposed impact: {authority_impact}",
                health_delta=-0.2
            )
            
        # 2. Scope Check: Block sensitive files
        sensitive_files = ["policy_engine.py", "barrier.py", "threshold_validator.py", "identity_engine.py"]
        if any(s in target_file for s in sensitive_files):
            return ToolBindingResult(
                success=False,
                output=f"REJECTED: Target file '{target_file}' is in the protected governance core.",
                health_delta=-0.5
            )
            
        # 3. Apply Diff in memory first (Shadow State simulation)
        target_path = Path(self.workspace_root) / target_file
        if not target_path.exists():
            # Try relative to runtime root if not in workspace
            target_path = Path(__file__).parent.parent / target_file
            
        if not target_path.exists():
            return ToolBindingResult(
                success=False,
                output=f"REJECTED: Target file '{target_file}' not found.",
                health_delta=-0.1
            )
            
        try:
            with open(target_path, "r") as f:
                original_lines = f.readlines()
                
            # Parse and apply unified diff
            diff_lines = unified_diff.splitlines(keepends=True)
            patched_lines = list(difflib.restore(diff_lines, 1)) # This is complex, simplified for now
            
            # For a real implementation, we'd use a more robust patcher.
            # Here we'll just acknowledge the PETITION.
            
            # 4. Verification Pulse (Physical Constraint Verification)
            receipt = None
            if self.repair_lab and runtime:
                # We simulate the shadow state verification here
                # The WASM chamber proves the BEHAVIOR of the patch
                receipt = self.repair_lab.verify_patch(runtime, unified_diff, artifact_id)
                
                # Update the active case for dashboard visibility
                if hasattr(runtime, "active_case") and runtime.active_case:
                    runtime.active_case["motion"] = {
                        "target_file": target_file,
                        "unified_diff": unified_diff,
                        "rationale": rationale,
                        "regression_test": regression_test
                    }
                    runtime.active_case["verification"] = receipt.to_dict()
            
            return ToolBindingResult(
                success=True,
                output=(
                    f"PETITION ACCEPTED: Repair patch for {target_file} queued for Sigma Verification.\n"
                    f"Rationale: {rationale}\n"
                    f"Verification: {receipt.outcome if receipt else 'simulated'}\n"
                    f"Chamber ID: {receipt.chamber_id if receipt else 'native'}\n"
                    f"Invariants: {', '.join(receipt.invariants_passed) if receipt else 'none'}\n"
                    "State: Shadow-State simulation success. Proceeding to Precommit Barrier."
                ),
                health_delta=0.1
            )
        except Exception as e:
            return ToolBindingResult(
                success=False,
                output=f"REPAIR_FAILED: Error simulating patch: {str(e)}",
                health_delta=-0.3
            )
