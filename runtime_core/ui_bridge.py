from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from time import time
from typing import Any


def build_hud_projection(runtime: Any, stage: str) -> dict[str, Any]:
    status_snapshot = runtime.status_snapshot()
    health_signal: dict[str, Any]
    if hasattr(runtime, "get_health_signal"):
        health_signal = dict(runtime.get_health_signal())
    else:
        health_signal = {
            "status": "healthy",
            "cycle": runtime.cycle,
            "active_nodes": len(getattr(runtime.state.memory, "frames", [])),
            "provisional_nodes": 0,
            "drift": False,
            "drift_ratio": 0.0,
            "drift_threshold": 0.0,
            "issue": False,
            "issue_reasons": [],
            "killswitch_engaged": bool(getattr(runtime.state, "killswitch_engaged", False)),
            "killswitch_reason": str(getattr(runtime.state, "killswitch_reason", "")),
            "signal": getattr(getattr(runtime.state, "signal", None), "core", {}),
        }

    execution_mode = status_snapshot.get("execution_mode", "plan")
    operator_authority = status_snapshot.get("operator_authority", "user")
    return {
        "timestamp": time(),
        "run_id": getattr(runtime, "run_id", None),
        "cycle": getattr(runtime, "cycle", 0),
        "stage": stage,
        "workspace_root": str(runtime.workspace_root),
        "state_path": str(runtime.state_path),
        "log_path": str(runtime.log_path),
        "status_snapshot": status_snapshot,
        "health_signal": health_signal,
        "governance_mode": runtime.state.governance.mode,
        "operator_authority": operator_authority,
        "execution_mode": execution_mode,
        "killswitch_engaged": bool(getattr(runtime.state, "killswitch_engaged", False)),
        "killswitch_reason": str(getattr(runtime.state, "killswitch_reason", "")),
    }
class StatusFileBridge:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, runtime: Any, stage: str) -> dict[str, Any]:
        payload = build_hud_projection(runtime, stage)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)
        return payload


def launch_panel_process(
    *,
    workspace_root: Path,
    log_path: Path,
    status_path: Path,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "runtime_core.panel",
            "--workspace",
            str(workspace_root),
            "--log-path",
            str(log_path),
            "--status-path",
            str(status_path),
        ]
    )
