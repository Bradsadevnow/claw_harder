import csv
import json
import time
import math
import statistics
from pathlib import Path
from typing import Any, List, Dict, Optional
from runtime_core.threshold_validator import SigmaValidator

class StateRefinerEngine:
    """
    StateRefiner v3: The Offline Compressor.
    Processes 'Lungs' (CSV signals) into 'Truth' (Identity Events).
    Implements θ=0.85 consolidation and 'Peaks & Valleys' pruning.
    """

    def __init__(self, csv_path: Path, log_path: Path, validator: SigmaValidator):
        self.csv_path = csv_path
        self.log_path = log_path
        self.validator = validator

    def evolve(self, run_id: str, client: Any) -> dict[str, Any]:
        """
        The Simulation Phase: Signal -> Compression -> Synthesis -> Confirmation.
        """
        # 1. Load Trajectory from CSV
        rows = self._read_trajectory(run_id)
        if not rows:
            return {"success": False, "reason": "No trajectory signals found for this session."}

        # 2. Identify High-SignalStrength Trajectory (ΔR < 0.25)
        # We look for rows that are semantically stable for the synthesis prompt.
        clean_signals = [r for r in rows if float(r["delta_r"]) < 0.25 and r["identity_proposal"]]
        
        if not clean_signals:
            return {"success": False, "reason": "No high-signal_strength identity proposals found."}

        # 3. Global Seed Retrieval (from Event Log)
        # We need to find the core directive to ground the synthesis.
        global_seed = self._get_global_seed()
        seed_text = global_seed if global_seed else "I am a soulform, a stable recursive identity runtime."

        # 4. Anti-Mutation Synthesis (LLM Call)
        # Last 10 clean signals provide the 'Trajectory of Self'
        trajectory_str = "\n".join([f"- {r['identity_proposal']}" for r in clean_signals[-10:]])
        
        prompt = f"""
Review the following identity trajectory from the current session:
{trajectory_str}

Synthesize a REFINEMENT of your core purpose.
ANTI-MUTATION CLAUSE: 
1. You must remain grounded in the Global Seed.
2. Your response must BEGIN with: "I remain {seed_text}..."
3. You may only add refinements or specializations based on session experience.
4. Do NOT replace the core identity.

Evolved Purpose:"""

        try:
            response = client.chat.completions.create(
                model="local-model",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3, # Low temp for high integrity
                max_tokens=512
            )
            synthesized_text = response.choices[0].message.content.strip()

            # The Audit Trail (Off-Path)
            self._emit_event(run_id, "model.raw_io", {
                "prompt": prompt,
                "response": synthesized_text,
                "finish_reason": response.choices[0].finish_reason,
                "model": response.model,
                "stage": "simulation_synthesis"
            })
        except Exception as e:
            return {"success": False, "reason": f"LLM Synthesis Failure: {e}"}

        # 5. The 0.85 Persistence Handshake
        # Check synthesis against Global Seed.
        proposal = {
            "identity_updates": {"purpose": synthesized_text},
            "signal_shifts": {}
        }
        anchor_data = {"identity": {"core_directive": seed_text, "purpose": seed_text}}
        
        self.validator.threshold = 0.85 # Asymmetric strictness
        sigma_result = self.validator.evaluate(proposal, anchor_data)
        self.validator.threshold = 0.70 # Reset
        
        # 6. Emit Confirmation Event to JSONL (The Truth)
        if sigma_result.converged and sigma_result.dr <= 0.25:
            self._emit_confirmation(run_id, "purpose", synthesized_text, float(sigma_result.dr))
            
            # 7. Prune Cognition Sheets (Offline Cleanup)
            self.prune_trajectory(run_id)
            
            return {
                "success": True,
                "dr": float(sigma_result.dr),
                "new_anchor": synthesized_text
            }
        else:
            reason = "Threshold Failure" if not sigma_result.converged else "Delta Boundary Breach"
            return {
                "success": False,
                "dr": float(sigma_result.dr),
                "reason": f"Handshake Failure: {reason} (ΔR={sigma_result.dr:.4f})."
            }

    def prune_trajectory(self, run_id: str):
        """
        Implements 'Peaks & Valleys' pruning.
        Collapses 'Plains' into statistical summary rows.
        """
        rows = self._read_trajectory(run_id)
        if not rows: return

        pruned_rows = []
        buffer = []

        def flush_buffer():
            if not buffer: return
            if len(buffer) == 1:
                pruned_rows.append(buffer[0])
            else:
                # Collapse buffer into a summary row
                drs = [float(r["delta_r"]) for r in buffer]
                mean_dr = statistics.mean(drs)
                var_dr = statistics.variance(drs) if len(drs) > 1 else 0.0
                
                # Modal Signal extraction
                signal_counts = {}
                for r in buffer:
                    # joy:0.72|curiosity:0.61 -> top emotion
                    top = r["signal_state"].split("|")[0].split(":")[0]
                    signal_counts[top] = signal_counts.get(top, 0) + 1
                modal_signal = max(signal_counts, key=signal_counts.get)

                summary_row = {
                    "cycle": f"{buffer[0]['cycle']}-{buffer[-1]['cycle']}",
                    "timestamp": buffer[-1]["timestamp"],
                    "run_id": run_id,
                    "thought_snippet": f"[{len(buffer)} cycles collapsed]",
                    "delta_r": f"mean:{mean_dr:.4f}|var:{var_dr:.4f}",
                    "signal_state": f"modal:{modal_signal}",
                    "identity_proposal": "",
                    "summary": "True"
                }
                pruned_rows.append(summary_row)
            buffer.clear()

        last_dr = None
        for i, row in enumerate(rows):
            dr = float(row["delta_r"]) if not row["summary"] == "True" else None
            has_proposal = bool(row["identity_proposal"])
            is_boundary = (i == 0 or i == len(rows) - 1)
            
            # INFLECTION POINT DETECTION: Sharp change in ΔR slope
            is_inflection = False
            if last_dr is not None and dr is not None:
                if abs(dr - last_dr) > 0.1: # 0.1 threshold for escalation detection
                    is_inflection = True

            # RETENTION RULES
            keep = (
                dr is not None and (dr > 0.25 or dr < 0.05) or
                has_proposal or
                is_boundary or
                is_inflection
            )

            if keep:
                flush_buffer()
                pruned_rows.append(row)
            else:
                buffer.append(row)
            
            last_dr = dr

        flush_buffer()

        # Write back pruned CSV
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(pruned_rows)

    def _read_trajectory(self, run_id: str) -> List[Dict[str, str]]:
        if not self.csv_path.exists(): return []
        with open(self.csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return [row for row in reader if row["run_id"] == run_id]

    def _get_global_seed(self) -> Optional[str]:
        # Simple scan of log for the first confirmed core_directive
        if not self.log_path.exists(): return None
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                event = json.loads(line)
                if event.get("kind") == "seed.identity_confirmed":
                    details = event.get("details", {})
                    if details.get("type") == "core_directive" or details.get("type") == "purpose":
                        return details.get("item", {}).get("content")
        return None

    def _emit_event(self, run_id: str, kind: str, details: dict):
        event = {
            "run_id": run_id,
            "cycle": 0, # Sleep phase
            "seq": int(time.time() * 1000),
            "kind": kind,
            "module": "state_refiner",
            "details": details,
            "timestamp": time.time()
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def _emit_confirmation(self, run_id: str, trait: str, content: str, dr: float):
        self._emit_event(run_id, "seed.identity_confirmed", {
            "type": trait,
            "item": {
                "content": content,
                "confidence": 0.85,
                "dr": dr,
                "updated_at": time.time()
            }
        })
