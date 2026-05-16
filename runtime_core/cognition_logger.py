import csv
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

class CognitionLogger:
    """
    The 'Lungs' of the system.
    A thin, non-blocking CSV appender for high-frequency execution signals.
    CSV is considered LOSSY and is pruned during the Simulation Phase.
    """

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self.fieldnames = [
            "cycle", 
            "run_id",
            "timestamp", 
            "thought_snippet", 
            "delta_r", 
            "signal_state", 
            "identity_proposal",
            "seq_ref", # Link to the Event Log
            "summary" # Boolean flag for collapsed rows
        ]
        self._ensure_header()

    def _ensure_header(self):
        if not self.csv_path.exists():
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                writer.writeheader()

    def log_cycle(
        self, 
        cycle: int, 
        run_id: str, 
        thought: str, 
        delta_r: float, 
        signal_core: Dict[str, float], 
        proposal: Optional[str] = None,
        seq_ref: Optional[int] = None
    ):
        """
        Appends a execution signal row to the CSV.
        """
        # Format Absolute Signal: joy:0.72|curiosity:0.61|... (Sorted DESC)
        sorted_signal = sorted(signal_core.items(), key=lambda x: x[1], reverse=True)
        signal_str = "|".join([f"{k}:{v:.4f}" for k, v in sorted_signal if v > 0.0001])

        # Snippet thought to keep CSV manageable (Full thought is in Event Log)
        thought_snippet = (thought[:200].replace("\n", " ") + "...") if len(thought) > 200 else thought.replace("\n", " ")

        row = {
            "cycle": cycle,
            "run_id": run_id,
            "timestamp": time.time(),
            "thought_snippet": thought_snippet,
            "delta_r": round(delta_r, 4),
            "signal_state": signal_str,
            "identity_proposal": proposal or "",
            "seq_ref": seq_ref,
            "summary": False
        }

        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.fieldnames)
            writer.writerow(row)

    def log_summary(self, cycle_range: str, run_id: str, mean_dr: float, var_dr: float, modal_signal: str):
        """
        Logs a collapsed 'Plain' row during pruning.
        """
        row = {
            "cycle": cycle_range,
            "timestamp": time.time(),
            "run_id": run_id,
            "thought_snippet": f"[Collapsed Cycles]",
            "delta_r": f"mean:{mean_dr:.4f}|var:{var_dr:.4f}",
            "signal_state": f"modal:{modal_signal}",
            "identity_proposal": "",
            "summary": True
        }

        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.fieldnames)
            writer.writerow(row)
