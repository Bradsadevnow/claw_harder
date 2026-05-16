from __future__ import annotations

import argparse
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import time
import uuid
from urllib.parse import parse_qs, urlparse
import re
from threading import Lock, Thread
from typing import Any

from pathlib import Path
from .model import load_model
from .runtime import RuntimeRuntime, POSTURE_MAP
from .truth_api import TruthAPI
from runtime.contract.adapters.runtime_core_v1 import (
    adapt_status,
    adapt_events,
    adapt_memory,
    adapt_schema,
    adapt_claims,
    adapt_claim_conflicts,
    adapt_claim_lifecycle,
    adapt_claim_provenance,
    adapt_claim_migration_latest,
)
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$")
OPERATION_LIFECYCLE_STATUSES = frozenset(
    {
        "proposed",
        "approved",
        "denied",
        "applied",
        "executed",
        "failed",
        "rolled_back",
        "superseded",
        "reconciled",
    }
)
REQUEST_TYPE_VALUES = frozenset({"patch", "tool"})
REQUEST_RISK_VALUES = frozenset({"low", "medium", "high"})
REQUEST_TERMINAL_STATUSES = frozenset(OPERATION_LIFECYCLE_STATUSES - {"proposed"})
PATCH_EXECUTION_FAILURE_REASONS = frozenset(
    {
        "request_not_found",
        "request_not_approved",
        "patch_not_found",
        "file_not_found",
        "file_already_exists",
        "range_conflict",
        "invalid_patch",
        "io_error",
    }
)
TOOL_EXECUTION_FAILURE_REASONS = frozenset(
    {
        "request_not_found",
        "request_not_approved",
        "tool_not_found",
        "invalid_tool_call",
        "tool_execution_failed",
    }
)


def create_handler(runtime: RuntimeRuntime, truth_api: TruthAPI, assets_dir: Path) -> type[BaseHTTPRequestHandler]:
    operations_path = runtime.log_path.parent / "operations.jsonl"
    operations_path.parent.mkdir(parents=True, exist_ok=True)
    op_lock = Lock()

    def _load_operation_counter() -> int:
        if not operations_path.exists():
            return 0
        max_seq = 0
        with suppress(Exception):
            for raw in operations_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                with suppress(Exception):
                    obj = json.loads(line)
                    operation_id = str(obj.get("operation_id", ""))
                    if operation_id.startswith("OP_"):
                        seq = int(operation_id[3:])
                        if seq > max_seq:
                            max_seq = seq
        return max_seq

    op_counter = _load_operation_counter()
    decision_counter = 0
    with suppress(Exception):
        rows = operations_path.read_text(encoding="utf-8").splitlines() if operations_path.exists() else []
        for raw in rows:
            line = raw.strip()
            if not line:
                continue
            with suppress(Exception):
                obj = json.loads(line)
                decision_id = str(obj.get("decision_id", ""))
                if decision_id.startswith("DEC_"):
                    seq = int(decision_id[4:])
                    if seq > decision_counter:
                        decision_counter = seq

    def _next_operation_id() -> str:
        nonlocal op_counter
        with op_lock:
            op_counter += 1
            return f"OP_{op_counter:06d}"

    def _next_decision_id() -> str:
        nonlocal decision_counter
        with op_lock:
            decision_counter += 1
            return f"DEC_{decision_counter:03d}"

    def _append_operation(record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True)
        with op_lock:
            with operations_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _read_operations(*, limit: int = 200) -> list[dict[str, Any]]:
        if not operations_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with suppress(Exception):
            for raw in operations_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                with suppress(Exception):
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        rows.append(payload)
        if limit <= 0:
            return []
        return rows[-limit:]

    def _read_all_operations() -> list[dict[str, Any]]:
        return _read_operations(limit=1_000_000)

    def _as_tool_calls(raw_value: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_value):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            arguments = item.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            call_id = str(item.get("call_id", "")).strip() or f"tool_call_{idx + 1}"
            normalized.append(
                {
                    "name": name,
                    "arguments": dict(arguments),
                    "call_id": call_id,
                }
            )
        return normalized

    def _default_patch_projection(request_id: str) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "request_type": "patch",
            "intent_id": "",
            "patch_ids": [],
            "reason": "",
            "risk": "",
            "requires_approval": True,
            "status": "proposed",
            "proposed_operation_id": None,
            "parent_operation_id": None,
            "patch_request_operation_id": None,
            "timestamp": None,
            "approved": False,
            "approved_by": None,
            "approved_at": None,
            "approved_operation_id": None,
            "reconciled": False,
            "reconciliation_operation_id": None,
            "closed_status": None,
            "closed_operation_id": None,
            "seen_request": False,
        }

    def _default_tool_projection(request_id: str) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "request_type": "tool",
            "intent_id": "",
            "tools": [],
            "tool_count": 0,
            "tool_calls": [],
            "reason": "",
            "risk": "",
            "requires_approval": True,
            "status": "proposed",
            "proposed_operation_id": None,
            "parent_operation_id": None,
            "tool_request_operation_id": None,
            "timestamp": None,
            "approved": False,
            "approved_by": None,
            "approved_at": None,
            "approved_operation_id": None,
            "reconciled": False,
            "reconciliation_operation_id": None,
            "closed_status": None,
            "closed_operation_id": None,
            "seen_request": False,
        }

    def _rebuild_request_projections(operations: list[dict[str, Any]]) -> dict[str, Any]:
        warnings: list[dict[str, Any]] = []
        seen_operation_ids: set[str] = set()
        patch_state: dict[str, dict[str, Any]] = {}
        tool_state: dict[str, dict[str, Any]] = {}
        ledger_last_operation_id: str | None = None

        for index, row in enumerate(operations):
            if not isinstance(row, dict):
                warnings.append(
                    {
                        "code": "invalid_operation_record",
                        "message": "Operation row is not an object.",
                        "index": index,
                    }
                )
                continue

            operation_id = str(row.get("operation_id", "")).strip()
            if operation_id:
                ledger_last_operation_id = operation_id
                if operation_id in seen_operation_ids:
                    warnings.append(
                        {
                            "code": "duplicate_operation_id",
                            "message": "Duplicate operation_id detected in ledger.",
                            "operation_id": operation_id,
                        }
                    )
                else:
                    seen_operation_ids.add(operation_id)

            op_type = str(row.get("operation_type", "")).strip()
            status = str(row.get("status", "")).strip().lower()
            refs = row.get("references")
            if not isinstance(refs, dict):
                refs = {}
            details = row.get("details")
            if not isinstance(details, dict):
                details = {}

            if op_type == "patch_request":
                request_id = str(refs.get("patch_request_id", "")).strip()
                patch_ids_raw = refs.get("patch_ids")
                patch_ids = [
                    str(item).strip()
                    for item in list(patch_ids_raw or [])
                    if str(item).strip()
                ] if isinstance(patch_ids_raw, list) else []
                if not request_id or not patch_ids:
                    warnings.append(
                        {
                            "code": "invalid_patch_request_reference",
                            "message": "Patch request missing request_id or patch_ids.",
                            "operation_id": operation_id,
                        }
                    )
                    continue
                state = patch_state.setdefault(request_id, _default_patch_projection(request_id))
                state["seen_request"] = True
                state["intent_id"] = str(refs.get("intent_id", "")).strip()
                state["patch_ids"] = patch_ids
                state["reason"] = str(details.get("reason", "")).strip()
                state["risk"] = str(details.get("risk", "")).strip().lower()
                state["requires_approval"] = bool(details.get("requires_approval", True))
                state["proposed_operation_id"] = operation_id or state.get("proposed_operation_id")
                state["patch_request_operation_id"] = operation_id or state.get("patch_request_operation_id")
                state["parent_operation_id"] = row.get("parent_operation_id")
                state["timestamp"] = row.get("timestamp")
                if status in REQUEST_TERMINAL_STATUSES and status != "approved":
                    state["closed_status"] = status
                    state["closed_operation_id"] = operation_id or state.get("closed_operation_id")
                continue

            if op_type == "tool_request":
                request_id = str(refs.get("tool_request_id", "")).strip()
                tool_calls = _as_tool_calls(details.get("tool_calls"))
                tools = [
                    str(item.get("name", "")).strip()
                    for item in tool_calls
                    if str(item.get("name", "")).strip()
                ] or [
                    str(item).strip()
                    for item in list(details.get("tools", []))
                    if str(item).strip()
                ]
                if not request_id or (not tool_calls and not tools):
                    warnings.append(
                        {
                            "code": "invalid_tool_request_reference",
                            "message": "Tool request missing request_id or callable tools.",
                            "operation_id": operation_id,
                        }
                    )
                    continue
                state = tool_state.setdefault(request_id, _default_tool_projection(request_id))
                state["seen_request"] = True
                state["intent_id"] = str(refs.get("intent_id", "")).strip()
                state["tools"] = tools
                state["tool_count"] = int(details.get("tool_count", len(tools)) or 0)
                state["tool_calls"] = tool_calls
                state["reason"] = str(details.get("reason", "")).strip()
                state["risk"] = str(details.get("risk", "")).strip().lower()
                state["requires_approval"] = bool(details.get("requires_approval", True))
                state["proposed_operation_id"] = operation_id or state.get("proposed_operation_id")
                state["tool_request_operation_id"] = operation_id or state.get("tool_request_operation_id")
                state["parent_operation_id"] = row.get("parent_operation_id")
                state["timestamp"] = row.get("timestamp")
                if status in REQUEST_TERMINAL_STATUSES and status != "approved":
                    state["closed_status"] = status
                    state["closed_operation_id"] = operation_id or state.get("closed_operation_id")
                continue

            if op_type == "patch_decision":
                request_id = str(row.get("request_id", "")).strip() or str(refs.get("patch_request_id", "")).strip()
                if not request_id:
                    warnings.append(
                        {
                            "code": "invalid_patch_decision_reference",
                            "message": "Patch decision missing request_id.",
                            "operation_id": operation_id,
                        }
                    )
                    continue
                state = patch_state.get(request_id)
                if state is None or not bool(state.get("seen_request")):
                    warnings.append(
                        {
                            "code": "orphan_decision",
                            "message": "Patch decision references unknown request.",
                            "operation_id": operation_id,
                            "request_id": request_id,
                            "request_type": "patch",
                        }
                    )
                    continue
                decision = str(row.get("decision", "")).strip().lower()
                if decision == "approved":
                    state["approved"] = True
                    state["approved_by"] = str(row.get("actor", "")).strip() or None
                    state["approved_at"] = row.get("timestamp")
                    state["approved_operation_id"] = operation_id or state.get("approved_operation_id")
                    state["closed_status"] = None
                    state["closed_operation_id"] = None
                elif decision == "denied":
                    state["approved"] = False
                    state["approved_by"] = None
                    state["approved_at"] = None
                    state["approved_operation_id"] = None
                    state["closed_status"] = "denied"
                    state["closed_operation_id"] = operation_id or state.get("closed_operation_id")
                continue

            if op_type == "tool_decision":
                request_id = str(row.get("request_id", "")).strip() or str(refs.get("tool_request_id", "")).strip()
                if not request_id:
                    warnings.append(
                        {
                            "code": "invalid_tool_decision_reference",
                            "message": "Tool decision missing request_id.",
                            "operation_id": operation_id,
                        }
                    )
                    continue
                state = tool_state.get(request_id)
                if state is None or not bool(state.get("seen_request")):
                    warnings.append(
                        {
                            "code": "orphan_decision",
                            "message": "Tool decision references unknown request.",
                            "operation_id": operation_id,
                            "request_id": request_id,
                            "request_type": "tool",
                        }
                    )
                    continue
                decision = str(row.get("decision", "")).strip().lower()
                if decision == "approved":
                    state["approved"] = True
                    state["approved_by"] = str(row.get("actor", "")).strip() or None
                    state["approved_at"] = row.get("timestamp")
                    state["approved_operation_id"] = operation_id or state.get("approved_operation_id")
                    state["closed_status"] = None
                    state["closed_operation_id"] = None
                elif decision == "denied":
                    state["approved"] = False
                    state["approved_by"] = None
                    state["approved_at"] = None
                    state["approved_operation_id"] = None
                    state["closed_status"] = "denied"
                    state["closed_operation_id"] = operation_id or state.get("closed_operation_id")
                continue

            if op_type == "execution_reconciliation":
                patch_request_id = str(refs.get("patch_request_id", "")).strip()
                tool_request_id = str(refs.get("tool_request_id", "")).strip()
                recognized = False
                if patch_request_id:
                    state = patch_state.get(patch_request_id)
                    if state is not None and bool(state.get("seen_request")):
                        state["reconciled"] = True
                        state["reconciliation_operation_id"] = operation_id or state.get("reconciliation_operation_id")
                        state["closed_status"] = "reconciled"
                        state["closed_operation_id"] = operation_id or state.get("closed_operation_id")
                        recognized = True
                if tool_request_id:
                    state = tool_state.get(tool_request_id)
                    if state is not None and bool(state.get("seen_request")):
                        state["reconciled"] = True
                        state["reconciliation_operation_id"] = operation_id or state.get("reconciliation_operation_id")
                        state["closed_status"] = "reconciled"
                        state["closed_operation_id"] = operation_id or state.get("closed_operation_id")
                        recognized = True
                if not recognized:
                    warnings.append(
                        {
                            "code": "orphan_reconciliation",
                            "message": "Reconciliation references unknown request.",
                            "operation_id": operation_id,
                            "patch_request_id": patch_request_id or None,
                            "tool_request_id": tool_request_id or None,
                        }
                    )
                continue

            patch_request_id = str(refs.get("patch_request_id", "")).strip()
            if patch_request_id:
                state = patch_state.get(patch_request_id)
                if state is not None and bool(state.get("seen_request")) and status in REQUEST_TERMINAL_STATUSES:
                    state["closed_status"] = status
                    state["closed_operation_id"] = operation_id or state.get("closed_operation_id")
            tool_request_id = str(refs.get("tool_request_id", "")).strip()
            if tool_request_id:
                state = tool_state.get(tool_request_id)
                if state is not None and bool(state.get("seen_request")) and status in REQUEST_TERMINAL_STATUSES:
                    state["closed_status"] = status
                    state["closed_operation_id"] = operation_id or state.get("closed_operation_id")

        pending_patch = [
            {
                "request_id": str(item.get("request_id", "")),
                "request_type": "patch",
                "intent_id": str(item.get("intent_id", "")),
                "patch_ids": list(item.get("patch_ids", [])),
                "reason": str(item.get("reason", "")),
                "risk": str(item.get("risk", "")),
                "requires_approval": bool(item.get("requires_approval", True)),
                "status": "proposed",
                "proposed_operation_id": item.get("proposed_operation_id"),
                "parent_operation_id": item.get("parent_operation_id"),
                "timestamp": item.get("timestamp"),
            }
            for item in patch_state.values()
            if bool(item.get("seen_request")) and not bool(item.get("closed_status")) and not bool(item.get("approved"))
        ]
        pending_tool = [
            {
                "request_id": str(item.get("request_id", "")),
                "request_type": "tool",
                "intent_id": str(item.get("intent_id", "")),
                "tools": list(item.get("tools", [])),
                "tool_count": int(item.get("tool_count", 0) or 0),
                "tool_calls": list(item.get("tool_calls", [])),
                "reason": str(item.get("reason", "")),
                "risk": str(item.get("risk", "")),
                "requires_approval": bool(item.get("requires_approval", True)),
                "status": "proposed",
                "proposed_operation_id": item.get("proposed_operation_id"),
                "parent_operation_id": item.get("parent_operation_id"),
                "timestamp": item.get("timestamp"),
            }
            for item in tool_state.values()
            if bool(item.get("seen_request")) and not bool(item.get("closed_status")) and not bool(item.get("approved"))
        ]

        ready_patch = [
            {
                "request_id": str(item.get("request_id", "")),
                "intent_id": str(item.get("intent_id", "")),
                "risk": str(item.get("risk", "")),
                "patch_ids": list(item.get("patch_ids", [])),
                "reason": str(item.get("reason", "")),
                "requires_approval": bool(item.get("requires_approval", True)),
                "approved_by": item.get("approved_by"),
                "approved_at": item.get("approved_at"),
                "approved_operation_id": item.get("approved_operation_id"),
                "patch_request_operation_id": item.get("patch_request_operation_id"),
                "status": "approved",
                "timestamp": item.get("timestamp"),
            }
            for item in patch_state.values()
            if bool(item.get("seen_request")) and bool(item.get("approved")) and not bool(item.get("reconciled"))
        ]
        ready_tool = [
            {
                "request_id": str(item.get("request_id", "")),
                "intent_id": str(item.get("intent_id", "")),
                "risk": str(item.get("risk", "")),
                "reason": str(item.get("reason", "")),
                "requires_approval": bool(item.get("requires_approval", True)),
                "tools": list(item.get("tools", [])),
                "tool_count": int(item.get("tool_count", 0) or 0),
                "tool_calls": list(item.get("tool_calls", [])),
                "approved_by": item.get("approved_by"),
                "approved_at": item.get("approved_at"),
                "approved_operation_id": item.get("approved_operation_id"),
                "tool_request_operation_id": item.get("tool_request_operation_id"),
                "status": "approved",
                "timestamp": item.get("timestamp"),
            }
            for item in tool_state.values()
            if bool(item.get("seen_request")) and bool(item.get("approved")) and not bool(item.get("reconciled"))
        ]

        closed_patch = [
            {
                "request_id": str(item.get("request_id", "")),
                "request_type": "patch",
                "intent_id": str(item.get("intent_id", "")),
                "status": str(item.get("closed_status", "")),
                "closed_operation_id": item.get("closed_operation_id"),
                "timestamp": item.get("timestamp"),
            }
            for item in patch_state.values()
            if bool(item.get("seen_request")) and bool(item.get("closed_status"))
        ]
        closed_tool = [
            {
                "request_id": str(item.get("request_id", "")),
                "request_type": "tool",
                "intent_id": str(item.get("intent_id", "")),
                "status": str(item.get("closed_status", "")),
                "closed_operation_id": item.get("closed_operation_id"),
                "timestamp": item.get("timestamp"),
            }
            for item in tool_state.values()
            if bool(item.get("seen_request")) and bool(item.get("closed_status"))
        ]

        return {
            "ledger_last_operation_id": ledger_last_operation_id,
            "pending_patch_requests": pending_patch,
            "pending_tool_requests": pending_tool,
            "ready_patch_requests": ready_patch,
            "ready_tool_requests": ready_tool,
            "closed_patch_requests": closed_patch,
            "closed_tool_requests": closed_tool,
            "recovery_warnings": warnings,
        }

    def _normalize_lifecycle_status(raw: Any) -> str | None:
        value = str(raw or "").strip().lower()
        if not value:
            return None
        if value not in OPERATION_LIFECYCLE_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(sorted(OPERATION_LIFECYCLE_STATUSES))}"
            )
        return value

    def _filter_operations(
        rows: list[dict[str, Any]],
        *,
        status: str | None = None,
        operation_type: str | None = None,
    ) -> list[dict[str, Any]]:
        filtered = rows
        if status:
            filtered = [
                item
                for item in filtered
                if isinstance(item, dict) and str(item.get("status", "")).strip().lower() == status
            ]
        if operation_type:
            filtered = [
                item
                for item in filtered
                if isinstance(item, dict) and str(item.get("operation_type", "")).strip() == operation_type
            ]
        return filtered

    def _timeline_for_intent(
        intent_id: str,
        *,
        limit: int = 200,
        status: str | None = None,
        operation_type: str | None = None,
    ) -> list[dict[str, Any]]:
        cleaned = str(intent_id).strip()
        if not cleaned:
            return []
        scoped = [
            item for item in _read_operations(limit=1000)
            if isinstance(item, dict)
            and isinstance(item.get("references"), dict)
            and str(item.get("references", {}).get("intent_id", "")).strip() == cleaned
        ]
        scoped = _filter_operations(scoped, status=status, operation_type=operation_type)
        if limit <= 0:
            return []
        return scoped[-limit:]

    _recovery_boot_timestamp = time.time()
    _recovery_boot_projection = _rebuild_request_projections(_read_all_operations())

    class CommsHandler(BaseHTTPRequestHandler):
        def __init__(self, *args, **kwargs) -> None:
            self.assets_dir = assets_dir
            super().__init__(*args, **kwargs)
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._write_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            # Static Frontend Router
            assets_dir = self.assets_dir
            static_target = None
            if path == "/":
                static_target = "comms.html"
            elif path == "/ops":
                static_target = "index.html"
            elif path.startswith("/") and not path.startswith("/api/"):
                static_target = path[1:]
                
            if static_target is not None:
                target = (assets_dir / static_target).resolve()
                if assets_dir in target.parents and target.exists() and target.is_file():
                    content_type = "text/html; charset=utf-8"
                    if target.suffix == ".css":
                        content_type = "text/css"
                    elif target.suffix == ".js":
                        content_type = "application/javascript"
                        
                    raw = target.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self._write_cors_headers()
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(raw)
                    return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self._write_cors_headers()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/api/health":
                if hasattr(runtime, "project_runtime_status"):
                    snapshot = runtime.project_runtime_status()
                elif hasattr(runtime, "status_snapshot"):
                    snapshot = runtime.status_snapshot()
                else:
                    snapshot = {"state": "idle"}
                self._json_response(HTTPStatus.OK, {"ok": True, **snapshot})
                return
            if path == "/api/config":
                if hasattr(runtime, "config"):
                    self._json_response(HTTPStatus.OK, runtime.config.to_dict())
                else:
                    self._json_response(HTTPStatus.OK, runtime.contract.roadmap.to_dict())
                return
            if path == "/api/validation_manager/artifacts":
                self._handle_validation_manager_artifacts()
                return
            if path == "/api/validation_manager/motions":
                self._handle_validation_manager_motions()
                return
            if path == "/api/monitor/active_case":
                active = getattr(runtime, "active_case", None)
                if active:
                    # Tighten contract
                    self._json_response(HTTPStatus.OK, {
                        "active_case": {
                            "case_id": active["artifact"]["artifact_id"],
                            "status": "awaiting_verdict",
                            "severity": active["artifact"].get("severity", "medium"),
                            "charge": f"{active['artifact']['exception_type']}: {active['artifact']['exception_message']}",
                            "artifact": active["artifact"],
                            "motion": active["motion"],
                            "verification": active["verification"],
                            "authority_impact": "none"
                        }
                    })
                else:
                    self._json_response(HTTPStatus.OK, {"active_case": None})
                return
            if path == "/api/status":
                self._json_response(HTTPStatus.OK, adapt_status(runtime))
                return
            if path == "/api/identity":
                self._json_response(HTTPStatus.OK, truth_api.get_identity())
                return
            if path == "/api/events":
                after_seq_raw = (query.get("after_seq") or ["0"])[0]
                try:
                    after_seq = int(after_seq_raw)
                except (TypeError, ValueError):
                    after_seq = 0
                self._json_response(HTTPStatus.OK, {"events": adapt_events(runtime, after_seq)})
                return
            if path == "/api/memory":
                self._json_response(HTTPStatus.OK, adapt_memory(runtime))
                return
            if path == "/api/schema":
                self._json_response(HTTPStatus.OK, adapt_schema())
                return
            if path == "/api/turns/current":
                snap = runtime.status_snapshot()
                cycle = int(snap.get("cycle", 0))
                gate = snap.get("gate") or {}
                self._json_response(HTTPStatus.OK, {
                    "turn_id": f"turn-{runtime.run_id}-{cycle}",
                    "barrier_active": bool(gate.get("barrier_active", False)),
                })
                return
            if path == "/api/claims":
                self._json_response(HTTPStatus.OK, adapt_claims(runtime))
                return
            if path == "/api/claims/conflicts":
                self._json_response(HTTPStatus.OK, adapt_claim_conflicts(runtime))
                return
            if path == "/api/claims/lifecycle":
                self._json_response(HTTPStatus.OK, adapt_claim_lifecycle(runtime))
                return
            if path == "/api/claims/provenance":
                self._json_response(HTTPStatus.OK, adapt_claim_provenance(runtime))
                return
            if path == "/api/claims/migrations/latest":
                self._json_response(HTTPStatus.OK, adapt_claim_migration_latest(runtime))
                return
            if path == "/api/log-integrity":
                self._json_response(HTTPStatus.OK, truth_api.get_log_integrity())
                return
            if path == "/api/diff":
                other = (query.get("other_log_path") or [""])[0]
                if not other:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "other_log_path query param is required"})
                    return
                result = truth_api.diff_against(other)
                status = HTTPStatus.OK if bool(result.get("ok")) else HTTPStatus.BAD_REQUEST
                self._json_response(status, result)
                return
            if path == "/api/control/posture":
                self._json_response(HTTPStatus.OK, {
                    "current": runtime.derive_runtime_posture(),
                    "map": POSTURE_MAP
                })
                return
            if path == "/api/metrics":
                mem = runtime.state.memory
                ingestion = getattr(runtime, "_ingestion", None)
                from .ingestion import RECURRENCE_THRESHOLD as _RT
                recurrence_above = 0
                if ingestion:
                    recurrence_above = sum(
                        1 for c in ingestion._entity_counts.values()
                        if c >= _RT
                    )
                self._json_response(HTTPStatus.OK, {
                    "schema_version": "agent.v1",
                    "cycle": runtime.cycle,
                    "memory": {
                        "working_items": len(mem.working.items),
                        "episodic_frames": len(mem.episodic.frames),
                        "semantic_items": len(mem.semantic.all()),
                        "procedural_items": len(mem.procedural.all()) if hasattr(mem.procedural, "all") else 0,
                        "pending_proposals": len(mem._proposals),
                    },
                    "ingestion": {
                        "tracked_entities": len(ingestion._entity_counts) if ingestion else 0,
                        "recurrence_above_threshold": recurrence_above,
                    },
                    "events": {
                        "buffer_depth": len(runtime._event_buffer),
                        "buffer_limit": runtime._event_buffer_limit,
                    },
                    "governance": {
                        "posture": getattr(runtime.state, "posture", None),
                        "killswitch_engaged": bool(runtime.state.killswitch_engaged),
                        "governance_mode": getattr(runtime.state.governance, "mode", None),
                    },
                    "performance": {
                        "last_turn_latency_ms": round(getattr(runtime, "_last_turn_latency_ms", 0.0), 2),
                    },
                })
                return
            if path == "/api/operations":
                limit_raw = (query.get("limit") or ["200"])[0]
                try:
                    limit = int(limit_raw)
                except (TypeError, ValueError):
                    limit = 200
                limit = max(1, min(limit, 1000))
                status_raw = (query.get("status") or [""])[0]
                operation_type = str((query.get("operation_type") or [""])[0]).strip() or None
                try:
                    status_filter = _normalize_lifecycle_status(status_raw)
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                rows = _read_operations(limit=1000)
                filtered = _filter_operations(rows, status=status_filter, operation_type=operation_type)
                self._json_response(HTTPStatus.OK, {"operations": filtered[-limit:]})
                return
            if path == "/api/operations/timeline":
                intent_id = str((query.get("intent_id") or [""])[0]).strip()
                if not intent_id:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "intent_id query param is required"})
                    return
                limit_raw = (query.get("limit") or ["200"])[0]
                try:
                    limit = int(limit_raw)
                except (TypeError, ValueError):
                    limit = 200
                limit = max(1, min(limit, 1000))
                status_raw = (query.get("status") or [""])[0]
                operation_type = str((query.get("operation_type") or [""])[0]).strip() or None
                try:
                    status_filter = _normalize_lifecycle_status(status_raw)
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._json_response(
                    HTTPStatus.OK,
                    {
                        "intent_id": intent_id,
                        "operations": _timeline_for_intent(
                            intent_id,
                            limit=limit,
                            status=status_filter,
                            operation_type=operation_type,
                        ),
                    },
                )
                return
            if path == "/api/runtime/recovery":
                self._json_response(HTTPStatus.OK, self._recovery_payload())
                return
            if path == "/api/requests/pending":
                limit_raw = (query.get("limit") or ["200"])[0]
                try:
                    limit = int(limit_raw)
                except (TypeError, ValueError):
                    limit = 200
                limit = max(1, min(limit, 1000))

                request_type_raw = (query.get("request_type") or [""])[0]
                risk_raw = (query.get("risk") or [""])[0]
                intent_id = str((query.get("intent_id") or [""])[0]).strip() or None
                try:
                    request_type = self._normalize_request_type(request_type_raw)
                    risk = self._normalize_risk(risk_raw)
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return

                payload = self._collect_pending_requests(
                    limit=limit,
                    request_type=request_type,
                    risk=risk,
                    intent_id=intent_id,
                )
                self._json_response(HTTPStatus.OK, payload)
                return
            if path == "/api/requests/ready":
                limit_raw = (query.get("limit") or ["200"])[0]
                try:
                    limit = int(limit_raw)
                except (TypeError, ValueError):
                    limit = 200
                limit = max(1, min(limit, 1000))

                request_type_raw = (query.get("request_type") or [""])[0]
                risk_raw = (query.get("risk") or [""])[0]
                intent_id = str((query.get("intent_id") or [""])[0]).strip() or None
                try:
                    request_type = self._normalize_request_type(request_type_raw)
                    risk = self._normalize_risk(risk_raw)
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return

                payload = self._collect_ready_requests(
                    limit=limit,
                    request_type=request_type,
                    risk=risk,
                    intent_id=intent_id,
                )
                self._json_response(HTTPStatus.OK, payload)
                return
            if path == "/api/workspace/context":
                items = {
                    k: v for k, v in runtime.state.memory.working.items.items()
                    if k.startswith("ws:")
                }
                recurrence = {}
                if hasattr(runtime, "_ingestion"):
                    recurrence = dict(runtime._ingestion._entity_counts)
                self._json_response(HTTPStatus.OK, {
                    "working_items": items,
                    "recurrence": recurrence,
                })
                return
            if path == "/api/systems":
                if hasattr(runtime, "list_systems"):
                    self._json_response(HTTPStatus.OK, runtime.list_systems())
                else:
                    self._json_response(HTTPStatus.OK, {"systems": []})
                return
            if path == "/api/stream":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self._write_cors_headers()
                self.end_headers()
                try:
                    import time
                    after_seq_raw = (query.get("after_seq") or ["0"])[0]
                    try:
                        last_seq = int(after_seq_raw)
                    except (TypeError, ValueError):
                        last_seq = 0
                    while True:
                        mem_frames = []
                        if hasattr(runtime, "state") and getattr(runtime.state, "memory", None):
                            mem_frames = [f.to_dict() if hasattr(f, "to_dict") else {"role": getattr(f, "role", "unknown"), "content": getattr(f, "content", "")} for f in getattr(runtime.state.memory, "frames", [])]
                        else:
                            mem_frames = truth_api.get_state().get("memory", {}).get("frames", [])

                        morph_frame = None
                        if hasattr(runtime, "model") and hasattr(runtime.model, "_channel"):
                            cf = runtime.model._channel.current()
                            if cf:
                                morph_frame = {
                                    "mood": cf.mood, "glyph": cf.glyph, "hint": cf.hint,
                                    "color": getattr(cf, "color", None), "presence": getattr(cf, "presence", None), "ttl": cf.ttl
                                }

                        new_events = adapt_events(runtime, last_seq)
                        if new_events:
                            last_seq = max(e["seq"] for e in new_events)

                        payload = {
                            "schema_version": "agent.v1",
                            "status": adapt_status(runtime),
                            "events": new_events,
                            "memory": mem_frames,
                            "identity": truth_api.get_identity(),
                            "morph": morph_frame,
                            "monitor": {"active_case": getattr(runtime, "active_case", None)}
                        }
                        encoded = json.dumps(payload).encode("utf-8")
                        self.wfile.write(b"data: " + encoded + b"\n\n")
                        self.wfile.flush()
                        time.sleep(1.0)
                except Exception:
                    pass
                return
            self._json_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            is_control = self.path.startswith("/api/control/")
            if is_control:
                control_error = self._validate_control_request()
                if control_error is not None:
                    status, message = control_error
                    self._json_response(status, {"error": message})
                    return

            body = self._read_json_body(strict=is_control)
            if body is None:
                self._json_response(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
                return
            if self.path == "/api/control/posture":
                posture = body.get("posture")
                try:
                    runtime.set_posture(posture)
                    snap = runtime.status_snapshot()
                    self._record_control_plane_action(
                        "posture",
                        requested={"posture": posture},
                        applied={"posture": snap.get("posture")},
                    )
                    self._json_response(HTTPStatus.OK, snap)
                except ValueError as e:
                    self._emit_policy_denial("posture", str(e))
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return
            if self.path == "/api/control/halt":
                reason = body.get("reason", "operator_requested")
                runtime.halt(reason)
                self._record_control_plane_action(
                    "halt",
                    requested={"reason": reason},
                    applied={"killswitch_engaged": True},
                )
                self._json_response(HTTPStatus.OK, runtime.status_snapshot())
                return
            if self.path == "/api/control/resume":
                runtime.resume()
                self._record_control_plane_action(
                    "resume",
                    requested={},
                    applied={"killswitch_engaged": False},
                )
                self._json_response(HTTPStatus.OK, runtime.status_snapshot())
                return
            if self.path == "/api/input":
                message = str(body.get("message", "")).strip()
                if not message:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "message is required"})
                    return
                try:
                    metadata = body.get("metadata", {})
                    if metadata is None:
                        metadata = {}
                    if not isinstance(metadata, dict):
                        self._json_response(HTTPStatus.BAD_REQUEST, {"error": "metadata must be an object"})
                        return
                    if hasattr(runtime, "ingest_input"):
                        source = str(body.get("source", "api.input")).strip() or "api.input"
                        result = runtime.ingest_input(message, source=source, metadata=metadata)
                        self._json_response(HTTPStatus.ACCEPTED, {"ok": True, "intent": result})
                    else:
                        self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                except Exception as exc:
                    self._json_response(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if self.path == "/api/actions/dispatch":
                intent_id = str(body.get("intent_id", "")).strip()
                if not intent_id:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "intent_id is required"})
                    return
                try:
                    if hasattr(runtime, "dispatch_input_intent"):
                        result = runtime.dispatch_input_intent(intent_id, source="api.actions.dispatch")
                        status = HTTPStatus.OK if bool(result.get("ok")) else HTTPStatus.CONFLICT
                        self._json_response(status, result)
                    else:
                        self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                except KeyError:
                    self._json_response(HTTPStatus.NOT_FOUND, {"error": "intent not found"})
                except Exception as exc:
                    self._json_response(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if self.path == "/api/chat":
                message = str(body.get("message", "")).strip()
                if not message:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "message is required"})
                    return
                execute = body.get("execute", True)
                if not isinstance(execute, bool):
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "execute must be a boolean"})
                    return
                try:
                    if hasattr(runtime, "ingest_input") and hasattr(runtime, "dispatch_input_intent"):
                        intent = runtime.ingest_input(
                            message,
                            source="api.chat",
                            metadata={"compat_wrapper": True},
                        )
                        if not execute:
                            self._json_response(HTTPStatus.ACCEPTED, {"ok": True, "queued": True, "intent": intent})
                            return
                        result = runtime.dispatch_input_intent(str(intent.get("intent_id", "")), source="api.chat")
                        status = HTTPStatus.OK if bool(result.get("ok")) else HTTPStatus.CONFLICT
                        self._json_response(status, result)
                    elif hasattr(runtime, "run_buddy_chat"):
                        result = runtime.run_buddy_chat(message=message)
                        self._json_response(HTTPStatus.OK, result)
                    elif hasattr(runtime, "run_single_cycle"):
                        reply = runtime.run_single_cycle(message)
                        self._json_response(HTTPStatus.OK, {"ok": True, "reply": reply})
                    else:
                        self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                except Exception as exc:
                    self._json_response(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if self.path == "/api/ide/execute":
                instruction = str(body.get("instruction", body.get("message", ""))).strip()
                if not instruction:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "instruction is required"})
                    return
                if not hasattr(runtime, "ingest_input") or not hasattr(runtime, "dispatch_input_intent"):
                    self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                    return
                execute = body.get("execute", True)
                if not isinstance(execute, bool):
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "execute must be a boolean"})
                    return
                metadata = body.get("metadata") or {}
                if not isinstance(metadata, dict):
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "metadata must be an object"})
                    return

                raw_selected = body.get("selected_files") or []
                if not isinstance(raw_selected, list):
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "selected_files must be a list"})
                    return
                selected_files = [str(item) for item in raw_selected if str(item).strip()]

                raw_allowed_tools = body.get("allowed_tools") or []
                if not isinstance(raw_allowed_tools, list):
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "allowed_tools must be a list"})
                    return
                allowed_tools = [str(item) for item in raw_allowed_tools if str(item).strip()]

                cursor_context = body.get("cursor_context") or {}
                if not isinstance(cursor_context, dict):
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "cursor_context must be an object"})
                    return
                try:
                    patches = self._validate_patch_ops(body.get("patches"))
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                try:
                    patch_requests = self._validate_patch_requests(
                        body.get("patch_requests"),
                        [str(patch.get("patch_id", "")) for patch in patches],
                        instruction=instruction,
                    )
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return

                project_root = str(body.get("project_root", "")).strip()
                bridge_metadata = dict(metadata)
                bridge_metadata["ide_bridge"] = {
                    "project_root": project_root,
                    "selected_files": selected_files,
                    "cursor_context": cursor_context,
                    "allowed_tools": allowed_tools,
                }
                start_seq = int(getattr(runtime, "_seq", 0))
                try:
                    intent = runtime.ingest_input(
                        instruction,
                        source="api.ide.execute",
                        metadata=bridge_metadata,
                    )
                    intent_id = str(intent.get("intent_id", ""))
                    ide_operation = self._record_operation(
                        "ide_execute",
                        status="proposed",
                        references={"intent_id": intent_id},
                        details={
                            "execute": execute,
                            "project_root": project_root,
                            "selected_files_count": len(selected_files),
                            "allowed_tools_count": len(allowed_tools),
                            "patches_count": len(patches),
                            "patch_requests_count": len(patch_requests),
                        },
                    )
                    if patch_requests:
                        patch_index = {
                            str(item.get("patch_id", "")).strip(): dict(item)
                            for item in patches
                            if isinstance(item, dict) and str(item.get("patch_id", "")).strip()
                        }
                        for request in patch_requests:
                            request_patch_ids = [
                                str(candidate).strip()
                                for candidate in list(request.get("patch_ids", []))
                                if str(candidate).strip()
                            ]
                            request_patches = [
                                dict(patch_index[patch_id])
                                for patch_id in request_patch_ids
                                if patch_id in patch_index
                            ]
                            self._record_operation(
                                "patch_request",
                                status="proposed",
                                references={
                                    "intent_id": intent_id,
                                    "patch_request_id": request.get("request_id"),
                                    "patch_ids": request_patch_ids,
                                },
                                details={
                                    "risk": request.get("risk"),
                                    "requires_approval": request.get("requires_approval"),
                                    "reason": request.get("reason"),
                                    "patches": request_patches,
                                },
                                parent_operation_id=ide_operation["operation_id"],
                            )
                    if not execute:
                        tool_requests = self._collect_tool_call_requests()
                        if tool_requests:
                            self._record_tool_request_operations(
                                tool_requests,
                                intent_id=intent_id,
                                parent_operation_id=ide_operation["operation_id"],
                            )
                        self._json_response(
                            HTTPStatus.ACCEPTED,
                            {
                                "ok": True,
                                "queued": True,
                                "task_status": "queued",
                                "task": intent,
                                "explanation": "Intent queued.",
                                "patches": patches,
                                "patch_requests": patch_requests,
                                "proposed_patch": None,
                                "tool_call_requests": tool_requests,
                                "memory_mutations": [],
                                "status": adapt_status(runtime),
                            },
                        )
                        return

                    result = runtime.dispatch_input_intent(
                        str(intent.get("intent_id", "")),
                        source="api.ide.execute",
                    )
                    ok = bool(result.get("ok"))
                    reply = str(result.get("reply", ""))
                    error = str(result.get("error", ""))
                    explanation = reply if ok else error
                    status_code = HTTPStatus.OK if ok else HTTPStatus.CONFLICT
                    compatibility_patch = self._extract_first_diff_patch(reply)
                    tool_requests = self._collect_tool_call_requests()
                    if tool_requests:
                        self._record_tool_request_operations(
                            tool_requests,
                            intent_id=str((result.get("intent") or {}).get("intent_id", intent_id)),
                            parent_operation_id=ide_operation["operation_id"],
                        )
                    execution_attempt = self._record_operation(
                        "execution_attempt",
                        status="executed" if ok else "failed",
                        references={
                            "intent_id": str((result.get("intent") or {}).get("intent_id", intent_id)),
                        },
                        details={
                            "execute_requested": True,
                            "dispatch_ok": ok,
                            "patches_count": len(patches),
                            "patch_requests_count": len(patch_requests),
                            "tool_requests_count": len(tool_requests),
                            "no_op": len(patches) == 0 and len(tool_requests) == 0,
                        },
                        parent_operation_id=ide_operation["operation_id"],
                    )
                    self._record_operation(
                        "execution_reconciliation",
                        status="reconciled",
                        references={
                            "intent_id": str((result.get("intent") or {}).get("intent_id", intent_id)),
                        },
                        details={
                            "final_status": "executed" if ok else "failed",
                            "reconciled_reason": "post_dispatch_outcome",
                            "no_op": len(patches) == 0 and len(tool_requests) == 0,
                        },
                        parent_operation_id=execution_attempt["operation_id"],
                        reconciles_operation_id=execution_attempt["operation_id"],
                    )
                    self._json_response(
                        status_code,
                        {
                            "ok": ok,
                            "queued": False,
                            "task_status": str((result.get("intent") or {}).get("status", "")),
                            "task": result.get("intent") or intent,
                            "explanation": explanation,
                            # patches[] is authoritative; proposed_patch is compatibility-only.
                            "patches": patches,
                            "patch_requests": patch_requests,
                            "proposed_patch": compatibility_patch,
                            "tool_call_requests": tool_requests,
                            "memory_mutations": self._collect_memory_mutations(start_seq),
                            "status": adapt_status(runtime),
                        },
                    )
                except Exception as exc:
                    self._json_response(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                return
            if self.path == "/api/decisions/patch":
                self._handle_decision_submission("patch", body)
                return
            if self.path == "/api/decisions/tool":
                self._handle_decision_submission("tool", body)
                return
            if self.path == "/api/executions/patch":
                self._handle_patch_execution(body)
                return
            if self.path == "/api/executions/tool":
                self._handle_tool_execution(body)
                return
            if self.path == "/api/ingest":
                name = str(body.get("name", ""))
                content = str(body.get("content", ""))
                if not name.strip():
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "name is required"})
                    return
                if hasattr(runtime, "ingest_system"):
                    result = runtime.ingest_system(name=name, content=content)
                    self._json_response(HTTPStatus.CREATED, result)
                else:
                    self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                return
            if self.path == "/api/run":
                system_name = str(body.get("system_name", ""))
                task = str(body.get("task", ""))
                if not system_name or not task.strip():
                    self._json_response(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "system_name and task are required"},
                    )
                    return
                try:
                    if hasattr(runtime, "run_task"):
                        result = runtime.run_task(system_name=system_name, task=task)
                        self._json_response(HTTPStatus.OK, result)
                    elif hasattr(runtime, "run_single_cycle"):
                        reply = runtime.run_single_cycle(task)
                        self._json_response(HTTPStatus.OK, {"ok": True, "reply": reply})
                    else:
                        self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                except Exception as exc:
                    self._json_response(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if self.path == "/api/control/execution_mode":
                execute = body.get("execute")
                if not isinstance(execute, bool):
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "execute must be a boolean"})
                    return
                if not hasattr(runtime, "set_execution_mode"):
                    self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                    return
                runtime.set_execution_mode(execute)
                payload = runtime.status_snapshot()
                self._record_control_plane_action(
                    "execution_mode",
                    requested={"execute": execute},
                    applied={
                        "execution_mode": payload.get("execution_mode"),
                    },
                )
                self._json_response(HTTPStatus.OK, payload)
                return
            if self.path == "/api/control/permission_mode":
                permission_mode = str(body.get("permission_mode", body.get("mode", ""))).strip().lower()
                if permission_mode not in {"buddy", "task", "autonomous"}:
                    self._json_response(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "permission_mode must be one of: buddy, task, autonomous"},
                    )
                    return
                if not hasattr(runtime, "set_permission_mode"):
                    self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                    return
                try:
                    runtime.set_permission_mode(permission_mode)
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                payload = runtime.status_snapshot()
                self._record_control_plane_action(
                    "permission_mode",
                    requested={"permission_mode": permission_mode},
                    applied={
                        "permission_mode": payload.get("permission_mode"),
                        "execution_mode": payload.get("execution_mode"),
                    },
                )
                self._json_response(HTTPStatus.OK, payload)
                return
            if self.path == "/api/control/authority":
                authority = str(body.get("authority", "")).strip().lower()
                if authority not in {"builder", "user"}:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "authority must be builder or user"})
                    return
                if not hasattr(runtime, "set_operator_authority"):
                    self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                    return
                try:
                    runtime.set_operator_authority(authority)
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                payload = runtime.status_snapshot()
                self._record_control_plane_action(
                    "operator_authority",
                    requested={"authority": authority},
                    applied={
                        "operator_authority": payload.get("operator_authority"),
                    },
                )
                self._json_response(HTTPStatus.OK, payload)
                return
            if self.path == "/api/control/governance":
                mode = str(body.get("mode", "")).strip().lower()
                if not mode:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "mode is required"})
                    return
                if not hasattr(runtime, "set_governance_mode"):
                    self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                    return
                try:
                    runtime.set_governance_mode(mode)
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                payload = runtime.status_snapshot()
                self._record_control_plane_action(
                    "governance_mode",
                    requested={"mode": mode},
                    applied={"governance_mode": payload.get("governance_mode")},
                )
                self._json_response(HTTPStatus.OK, payload)
                return
            if self.path == "/api/control/tick_mode":
                mode = str(body.get("mode", "off")).strip().lower()
                task = body.get("task")
                interval_ms = body.get("interval_ms", 1000)
                if not hasattr(runtime, "set_tick_mode"):
                    self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                    return
                try:
                    runtime.set_tick_mode(mode, task=task, interval_ms=interval_ms)
                except ValueError as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                payload = runtime.status_snapshot()
                self._record_control_plane_action(
                    "tick_mode",
                    requested={"mode": mode, "task": task, "interval_ms": interval_ms},
                    applied={
                        "tick_mode": payload.get("tick_mode"),
                        "tick_count": payload.get("tick_count"),
                    },
                )
                self._json_response(HTTPStatus.OK, payload)
                return
            if self.path == "/api/control/selected_system":
                system_name = str(body.get("system_name", "")).strip()
                if not system_name:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "system_name_required"})
                    return
                if hasattr(runtime, "_ensure_system_layout"):
                    try:
                        runtime._ensure_system_layout(system_name)
                    except Exception as exc:
                        self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                runtime._selected_system = system_name
                runtime.save_state()
                self._json_response(HTTPStatus.OK, {"ok": True, "selected_system": system_name})
                return
            if self.path == "/api/control/auto_resume":
                enabled = bool(body.get("enabled", False))
                runtime.auto_resume_on_restart = enabled
                runtime.save_state()
                self._json_response(HTTPStatus.OK, {"ok": True, "auto_resume_on_restart": enabled})
                return
            if self.path == "/api/control/soft-stop":
                reason = str(body.get("reason", "operator_requested: soft stop"))
                if hasattr(runtime, "request_soft_stop"):
                    payload = runtime.request_soft_stop(reason)
                    self._record_control_plane_action(
                        "soft_stop",
                        requested={"reason": reason},
                        applied={
                            "killswitch_engaged": payload.get("killswitch_engaged"),
                        },
                    )
                    self._json_response(HTTPStatus.OK, payload)
                else:
                    runtime.state.killswitch_engaged = True
                    runtime.state.killswitch_reason = "operator_requested: soft stop"
                    self._json_response(HTTPStatus.OK, runtime.status_snapshot())
                return
            if self.path == "/api/control/resume":
                if hasattr(runtime, "resume"):
                    payload = runtime.resume()
                    self._record_control_plane_action(
                        "resume",
                        requested={},
                        applied={
                            "killswitch_engaged": payload.get("killswitch_engaged"),
                        },
                    )
                    self._json_response(HTTPStatus.OK, payload)
                else:
                    runtime.state.killswitch_engaged = False
                    runtime.state.killswitch_reason = ""
                    self._json_response(HTTPStatus.OK, runtime.status_snapshot())
                return
            if self.path == "/api/control/hard-nuke":
                reason = str(body.get("reason", "operator_requested: hard nuke"))
                if hasattr(runtime, "hard_nuke"):
                    payload = runtime.hard_nuke(reason)
                    self._record_control_plane_action(
                        "hard_nuke",
                        requested={"reason": reason},
                        applied={
                            "killswitch_engaged": payload.get("killswitch_engaged"),
                        },
                    )
                    self._json_response(HTTPStatus.OK, payload)
                else:
                    runtime.state.killswitch_engaged = True
                    runtime.state.killswitch_reason = "operator_requested: hard nuke"
                    self._json_response(HTTPStatus.OK, runtime.status_snapshot())
                return
            if self.path == "/api/control/reinitialize":
                if hasattr(runtime, "reinitialize"):
                    payload = runtime.reinitialize()
                    self._record_control_plane_action(
                        "reinitialize",
                        requested={},
                        applied={
                            "killswitch_engaged": payload.get("killswitch_engaged"),
                        },
                    )
                    self._json_response(HTTPStatus.OK, payload)
                else:
                    self._json_response(HTTPStatus.OK, runtime.status_snapshot())
                return
            if self.path == "/api/control/debug":
                debug = body.get("debug")
                if not isinstance(debug, bool):
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "debug must be a boolean"})
                    return
                runtime.state.debug_mode = debug
                runtime.save_state()
                self._record_control_plane_action(
                    "debug_mode",
                    requested={"debug": debug},
                    applied={"debug_mode": runtime.state.debug_mode},
                )
                self._json_response(HTTPStatus.OK, {"ok": True, "debug_mode": runtime.state.debug_mode})
                return
            if self.path == "/api/control/reset":
                if not hasattr(runtime, "reset_memory"):
                    self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
                    return
                res = runtime.reset_memory()
                self._record_control_plane_action("reset_memory", requested={}, applied=res)
                self._json_response(HTTPStatus.OK, res)
                return
            if self.path == "/api/control/simulation":
                from .simulation import SimulationProcessor
                processor = SimulationProcessor(runtime.log_path)
                try:
                    results = processor.run()
                    summary = [{"trait": r.trait, "value": r.resolved_value} for r in results]
                    self._record_control_plane_action("simulation_cycle", requested={}, applied={"consolidated": len(results)})
                    self._json_response(HTTPStatus.OK, {"ok": True, "consolidated": summary})
                except Exception as exc:
                    self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            if self.path == "/api/workspace/signal":
                kind = str(body.get("kind", "")).strip()
                if not kind:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "kind is required"})
                    return
                if not hasattr(runtime, "_ingestion"):
                    self._json_response(HTTPStatus.NOT_IMPLEMENTED, {"error": "ingestion_not_available"})
                    return
                from .ingestion import WorkspaceEvent
                event = WorkspaceEvent(
                    kind=kind,
                    path=body.get("path") or None,
                    symbol=body.get("symbol") or None,
                    command=body.get("command") or None,
                    content=body.get("content") or None,
                    metadata=body.get("metadata") or {},
                )
                result = runtime._ingestion.ingest(event, runtime.state.memory, runtime.cycle)
                if hasattr(runtime, "_emit"):
                    runtime._emit(
                        "workspace.signal",
                        "ingestion",
                        f"Workspace signal: {kind!r} → {result.get('signal', '?')}",
                        details={
                            "kind": kind,
                            "path": event.path,
                            "symbol": event.symbol,
                            "signal": result.get("signal"),
                            "proposed": result.get("proposed", False),
                            "claim_type": result.get("claim_type"),
                            "destination": result.get("destination"),
                            "signals": result.get("signals"),
                            "recurrence_count": result.get("recurrence_count"),
                        },
                    )
                status_code = HTTPStatus.OK if result.get("ok") else HTTPStatus.UNPROCESSABLE_ENTITY
                self._json_response(status_code, result)
                return
            if self.path == "/api/monitor/verdict":
                decision = str(body.get("decision", "")).strip().lower()
                artifact_id = str(body.get("artifact_id", "")).strip()
                reason = str(body.get("reason", "")).strip()
                
                if not decision or not artifact_id:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "decision and artifact_id are required"})
                    return
                
                if decision == "reject" and not reason:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": "reason is required for rejection"})
                    return
                
                # Check if the artifact matches the active case
                active = getattr(runtime, "active_case", None)
                if not active or active.get("artifact", {}).get("artifact_id") != artifact_id:
                    self._json_response(HTTPStatus.NOT_FOUND, {"error": "no active case matches that artifact_id"})
                    return
                
                # Process verdict
                # For now, we clear the active_case. 
                runtime.active_case = None
                
                # 2. Persist to Case Law (Precedent)
                precedent_path = runtime.workspace_root / ".runtime_core" / "case_law.jsonl"
                precedent_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Capture high-fidelity context for precedent weighting
                # We hash the offending traceback and context to identify similar future faults
                tb_text = "".join([f"{t['file']}:{t['line']}:{t['name']}" for t in active.get("artifact", {}).get("traceback_summary", [])])
                context_hash = hashlib.sha256(tb_text.encode()).hexdigest()

                with open(precedent_path, "a", encoding="utf-8") as f:
                    precedent = {
                        "timestamp": time.time(),
                        "artifact_id": artifact_id,
                        "decision": decision,
                        "reason": reason,
                        "charge": active.get("artifact", {}).get("exception_type"),
                        "repair_class": active.get("artifact", {}).get("repair_class"),
                        "file": active.get("artifact", {}).get("offending_file"),
                        "context_hash": context_hash,
                        "failure_mode": body.get("failure_mode", "unknown"), # Allow UI to pass specific modes
                        "alternative_pattern": body.get("alternative_pattern", "none")
                    }
                    f.write(json.dumps(precedent) + "\n")

                self._record_control_plane_action(
                    "monitor_verdict",
                    requested={"artifact_id": artifact_id, "decision": decision, "reason": reason},
                    applied={"outcome": "committed" if decision == "approve" else "discarded", "case_closed": True}
                )
                self._json_response(HTTPStatus.OK, {
                    "ok": True, 
                    "status": "committed" if decision == "approve" else "discarded",
                    "case_closed": True
                })
                return
            self._json_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def _handle_validation_manager_artifacts(self) -> None:
            try:
                artifact_dir = runtime.workspace_root / ".runtime_core" / "validation_manager"
                artifacts = []
                if artifact_dir.exists():
                    for p in sorted(artifact_dir.glob("diag_*.json"), reverse=True):
                        try:
                            with open(p, "r") as f:
                                artifacts.append(json.load(f))
                        except Exception:
                            continue
                self._json_response(HTTPStatus.OK, {"artifacts": artifacts})
            except Exception as e:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

        def _handle_validation_manager_motions(self) -> None:
            # For now, we derive motions from artifacts with repair_allowed=True
            try:
                artifact_dir = runtime.workspace_root / ".runtime_core" / "validation_manager"
                motions = []
                if artifact_dir.exists():
                    for p in sorted(artifact_dir.glob("diag_*.json"), reverse=True):
                        try:
                            with open(p, "r") as f:
                                data = json.load(f)
                                if data.get("repair_allowed"):
                                    motions.append(data)
                        except Exception:
                            continue
                self._json_response(HTTPStatus.OK, {"motions": motions})
            except Exception as e:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _validate_control_request(self) -> tuple[HTTPStatus, str] | None:
            origin = self.headers.get("Origin", "").strip()
            if origin and not _LOCAL_ORIGIN_RE.match(origin):
                return (HTTPStatus.FORBIDDEN, "origin not allowed for control endpoint")

            content_type = self.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return (HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content-type must be application/json")
            return None

        def _read_json_body(self, *, strict: bool = False) -> dict[str, Any] | None:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = 0
            raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
            if not raw:
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return None if strict else {}
            if isinstance(payload, dict):
                return payload
            return None if strict else {}

        def _json_response(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self._write_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _html_response(self, status: HTTPStatus, html: str) -> None:
            encoded = html.encode("utf-8")
            self.send_response(status)
            self._write_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _record_control_plane_action(
            self,
            action: str,
            *,
            requested: dict[str, Any],
            applied: dict[str, Any],
        ) -> None:
            if hasattr(runtime, "emit_control_plane_action"):
                runtime.emit_control_plane_action(
                    action,
                    source="dashboard",
                    requested=requested,
                    applied=applied,
                )

        def _emit_policy_denial(self, action: str, reason: str) -> None:
            if hasattr(runtime, "_emit"):
                runtime._emit(
                    "policy_engine.decision",
                    "policy_engine",
                    f"Control action {action!r} denied by policy.",
                    level="warning",
                    details={"action": action, "decision": "denied", "reason": reason},
                )

        def _collect_tool_call_requests(self) -> list[dict[str, Any]]:
            pending: Any = None
            if hasattr(runtime, "state"):
                pending = getattr(runtime.state, "pending_tool_confirmation", None)
            if not isinstance(pending, dict):
                snap = runtime.status_snapshot()
                pending = snap.get("pending_tool_confirmation")
            if isinstance(pending, dict):
                tool_calls_raw = pending.get("tool_calls")
                tool_calls: list[dict[str, Any]] = []
                if isinstance(tool_calls_raw, list):
                    for item in tool_calls_raw:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name", "")).strip()
                        arguments = item.get("arguments")
                        call_id = str(item.get("call_id", "")).strip() or None
                        if not isinstance(arguments, dict):
                            arguments = {}
                        if not name:
                            continue
                        tool_calls.append(
                            {
                                "name": name,
                                "arguments": dict(arguments),
                                "call_id": call_id,
                            }
                        )
                tool_names = [
                    str(item.get("name", "")).strip()
                    for item in tool_calls
                    if str(item.get("name", "")).strip()
                ]
                return [
                    {
                        "request_id": str(pending.get("request_id", "")).strip(),
                        "created_at": pending.get("created_at"),
                        "cycle": pending.get("cycle"),
                        "tool_count": len(tool_calls),
                        "tools": tool_names,
                        "tool_calls": tool_calls,
                    }
                ]
            return []

        def _collect_memory_mutations(self, after_seq: int) -> list[dict[str, Any]]:
            events = adapt_events(runtime, after_seq)
            mutated: list[dict[str, Any]] = []
            for event in events:
                kind = str(event.get("kind", ""))
                if kind.startswith("memory.") or kind.startswith("working_memory.") or kind.startswith("salience."):
                    mutated.append(
                        {
                            "event_id": event.get("event_id"),
                            "seq": event.get("seq"),
                            "kind": kind,
                            "details": event.get("details", {}),
                        }
                    )
            return mutated

        @staticmethod
        def _normalize_request_type(raw: Any) -> str | None:
            value = str(raw or "").strip().lower()
            if not value:
                return None
            if value not in REQUEST_TYPE_VALUES:
                raise ValueError("request_type must be one of: patch, tool")
            return value

        @staticmethod
        def _normalize_risk(raw: Any) -> str | None:
            value = str(raw or "").strip().lower()
            if not value:
                return None
            if value not in REQUEST_RISK_VALUES:
                raise ValueError("risk must be one of: low, medium, high")
            return value

        def _recovery_payload(self) -> dict[str, Any]:
            projection = _recovery_boot_projection
            pending_patch = list(projection.get("pending_patch_requests", []))
            pending_tool = list(projection.get("pending_tool_requests", []))
            ready_patch = list(projection.get("ready_patch_requests", []))
            ready_tool = list(projection.get("ready_tool_requests", []))
            warnings = list(projection.get("recovery_warnings", []))
            return {
                "recovered_at": _recovery_boot_timestamp,
                "ledger_last_operation_id": projection.get("ledger_last_operation_id"),
                "open_patch_requests": [str(item.get("request_id", "")).strip() for item in pending_patch if str(item.get("request_id", "")).strip()],
                "open_tool_requests": [str(item.get("request_id", "")).strip() for item in pending_tool if str(item.get("request_id", "")).strip()],
                "ready_patch_requests": [str(item.get("request_id", "")).strip() for item in ready_patch if str(item.get("request_id", "")).strip()],
                "ready_tool_requests": [str(item.get("request_id", "")).strip() for item in ready_tool if str(item.get("request_id", "")).strip()],
                "recovery_warnings": warnings,
                "counts": {
                    "open_patch": len(pending_patch),
                    "open_tool": len(pending_tool),
                    "ready_patch": len(ready_patch),
                    "ready_tool": len(ready_tool),
                },
            }

        def _collect_pending_requests(
            self,
            *,
            limit: int,
            request_type: str | None,
            risk: str | None,
            intent_id: str | None,
        ) -> dict[str, Any]:
            projection = _rebuild_request_projections(_read_all_operations())
            pending_patch = list(projection.get("pending_patch_requests", []))
            pending_tool = list(projection.get("pending_tool_requests", []))

            if intent_id:
                pending_patch = [
                    item
                    for item in pending_patch
                    if str(item.get("intent_id", "")).strip() == intent_id
                ]
                pending_tool = [
                    item
                    for item in pending_tool
                    if str(item.get("intent_id", "")).strip() == intent_id
                ]

            if risk:
                pending_patch = [
                    item
                    for item in pending_patch
                    if str(item.get("risk", "")).strip().lower() == risk
                ]
                pending_tool = [
                    item
                    for item in pending_tool
                    if str(item.get("risk", "")).strip().lower() == risk
                ]

            pending_patch.sort(key=lambda item: float(item.get("timestamp", 0) or 0), reverse=True)
            pending_tool.sort(key=lambda item: float(item.get("timestamp", 0) or 0), reverse=True)

            if request_type == "patch":
                pending_patch = pending_patch[:limit]
                pending_tool = []
            elif request_type == "tool":
                pending_tool = pending_tool[:limit]
                pending_patch = []
            else:
                merged = [
                    {"lane": "patch", "item": item}
                    for item in pending_patch
                ] + [
                    {"lane": "tool", "item": item}
                    for item in pending_tool
                ]
                merged.sort(
                    key=lambda row: float((row.get("item") or {}).get("timestamp", 0) or 0),
                    reverse=True,
                )
                merged = merged[:limit]
                pending_patch = [row["item"] for row in merged if row.get("lane") == "patch"]
                pending_tool = [row["item"] for row in merged if row.get("lane") == "tool"]

            return {
                "patch_requests": pending_patch,
                "tool_requests": pending_tool,
                "counts": {
                    "patch": len(pending_patch),
                    "tool": len(pending_tool),
                },
            }

        def _collect_ready_requests(
            self,
            *,
            limit: int,
            request_type: str | None,
            risk: str | None,
            intent_id: str | None,
        ) -> dict[str, Any]:
            projection = _rebuild_request_projections(_read_all_operations())
            ready_patch = list(projection.get("ready_patch_requests", []))
            ready_tool = list(projection.get("ready_tool_requests", []))

            if intent_id:
                ready_patch = [
                    item
                    for item in ready_patch
                    if str(item.get("intent_id", "")).strip() == intent_id
                ]
                ready_tool = [
                    item
                    for item in ready_tool
                    if str(item.get("intent_id", "")).strip() == intent_id
                ]

            if risk:
                ready_patch = [
                    item
                    for item in ready_patch
                    if str(item.get("risk", "")).strip().lower() == risk
                ]
                ready_tool = [
                    item
                    for item in ready_tool
                    if str(item.get("risk", "")).strip().lower() == risk
                ]

            ready_patch.sort(key=lambda item: float(item.get("approved_at", item.get("timestamp", 0)) or 0), reverse=True)
            ready_tool.sort(key=lambda item: float(item.get("approved_at", item.get("timestamp", 0)) or 0), reverse=True)

            if request_type == "patch":
                ready_patch = ready_patch[:limit]
                ready_tool = []
            elif request_type == "tool":
                ready_tool = ready_tool[:limit]
                ready_patch = []
            else:
                merged = [
                    {"lane": "patch", "item": item}
                    for item in ready_patch
                ] + [
                    {"lane": "tool", "item": item}
                    for item in ready_tool
                ]
                merged.sort(
                    key=lambda row: float((row.get("item") or {}).get("approved_at", (row.get("item") or {}).get("timestamp", 0)) or 0),
                    reverse=True,
                )
                merged = merged[:limit]
                ready_patch = [row["item"] for row in merged if row.get("lane") == "patch"]
                ready_tool = [row["item"] for row in merged if row.get("lane") == "tool"]

            return {
                "ready_patch_requests": ready_patch,
                "ready_tool_requests": ready_tool,
                "counts": {
                    "patch": len(ready_patch),
                    "tool": len(ready_tool),
                },
            }

        def _record_operation(
            self,
            operation_type: str,
            *,
            status: str,
            references: dict[str, Any] | None = None,
            details: dict[str, Any] | None = None,
            parent_operation_id: str | None = None,
            reconciles_operation_id: str | None = None,
            actor: str = "big_homie",
            extra_fields: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if status not in OPERATION_LIFECYCLE_STATUSES:
                raise ValueError(
                    f"operation status must be one of: {', '.join(sorted(OPERATION_LIFECYCLE_STATUSES))}"
                )
            record = {
                "operation_id": _next_operation_id(),
                "timestamp": time.time(),
                "actor": actor,
                "operation_type": operation_type,
                "status": status,
                "parent_operation_id": parent_operation_id,
                "reconciles_operation_id": reconciles_operation_id,
                "references": dict(references or {}),
                "details": dict(details or {}),
            }
            if extra_fields:
                for key, value in extra_fields.items():
                    if key in record:
                        continue
                    record[key] = value
            _append_operation(record)
            return record

        def _record_tool_request_operations(
            self,
            tool_requests: list[dict[str, Any]],
            *,
            intent_id: str,
            parent_operation_id: str | None = None,
        ) -> None:
            for request in tool_requests:
                request_id = str(request.get("request_id", "")).strip()
                tool_calls_raw = request.get("tool_calls")
                tool_calls: list[dict[str, Any]] = []
                if isinstance(tool_calls_raw, list):
                    for item in tool_calls_raw:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name", "")).strip()
                        if not name:
                            continue
                        arguments = item.get("arguments")
                        if not isinstance(arguments, dict):
                            arguments = {}
                        call_id = str(item.get("call_id", "")).strip() or None
                        tool_calls.append(
                            {
                                "name": name,
                                "arguments": dict(arguments),
                                "call_id": call_id,
                            }
                        )
                tools = [
                    str(item.get("name", "")).strip()
                    for item in tool_calls
                    if str(item.get("name", "")).strip()
                ] or list(request.get("tools", []))
                self._record_operation(
                    "tool_request",
                    status="proposed",
                    references={
                        "intent_id": intent_id,
                        "tool_request_id": request_id,
                    },
                    details={
                        "tool_count": int(request.get("tool_count", len(tools)) or 0),
                        "tools": tools,
                        "tool_calls": tool_calls,
                    },
                    parent_operation_id=parent_operation_id,
                )

        def _find_request_operation(self, request_id: str) -> tuple[str, dict[str, Any]] | None:
            rid = str(request_id).strip()
            if not rid:
                return None
            for row in reversed(_read_all_operations()):
                if not isinstance(row, dict):
                    continue
                refs = row.get("references") or {}
                if not isinstance(refs, dict):
                    continue
                op_type = str(row.get("operation_type", "")).strip()
                if op_type == "patch_request" and str(refs.get("patch_request_id", "")).strip() == rid:
                    return ("patch", row)
                if op_type == "tool_request" and str(refs.get("tool_request_id", "")).strip() == rid:
                    return ("tool", row)
            return None

        def _find_decision_record(self, request_type: str, request_id: str) -> dict[str, Any] | None:
            rid = str(request_id).strip()
            op_type = f"{request_type}_decision"
            for row in reversed(_read_all_operations()):
                if not isinstance(row, dict):
                    continue
                if str(row.get("operation_type", "")).strip() != op_type:
                    continue
                if str(row.get("request_id", "")).strip() == rid:
                    return row
            return None

        def _decision_receipt_from_record(self, record: dict[str, Any]) -> dict[str, Any]:
            return {
                "decision_id": record.get("decision_id"),
                "request_id": record.get("request_id"),
                "decision": record.get("decision"),
                "actor": record.get("actor"),
                "status": record.get("status"),
                "timestamp": record.get("timestamp"),
            }

        def _handle_decision_submission(self, request_type: str, body: dict[str, Any]) -> None:
            request_id = str(body.get("request_id", "")).strip()
            decision = str(body.get("decision", "")).strip().lower()
            actor = str(body.get("actor", "")).strip()
            rationale = str(body.get("rationale", "")).strip()
            if not request_id:
                self._json_response(HTTPStatus.BAD_REQUEST, {"error": "request_id is required"})
                return
            if decision not in {"approved", "denied"}:
                self._json_response(HTTPStatus.BAD_REQUEST, {"error": "decision must be approved or denied"})
                return
            if not actor:
                self._json_response(HTTPStatus.BAD_REQUEST, {"error": "actor is required"})
                return
            if not rationale:
                self._json_response(HTTPStatus.BAD_REQUEST, {"error": "rationale is required"})
                return

            located = self._find_request_operation(request_id)
            if located is None:
                self._json_response(HTTPStatus.NOT_FOUND, {"error": "request not found"})
                return
            actual_type, request_record = located
            if actual_type != request_type:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"request_id belongs to {actual_type}_request, not {request_type}_request"},
                )
                return

            existing = self._find_decision_record(request_type, request_id)
            if existing is not None:
                existing_decision = str(existing.get("decision", "")).strip().lower()
                if existing_decision == decision:
                    self._json_response(HTTPStatus.OK, self._decision_receipt_from_record(existing))
                    return
                self._json_response(
                    HTTPStatus.CONFLICT,
                    {
                        "error": "conflicting decision already recorded",
                        "existing_decision": existing_decision,
                    },
                )
                return

            intent_id = str((request_record.get("references") or {}).get("intent_id", "")).strip()
            decision_id = _next_decision_id()
            record = self._record_operation(
                f"{request_type}_decision",
                status=decision,
                actor=actor,
                references={
                    "intent_id": intent_id,
                    f"{request_type}_request_id": request_id,
                },
                details={"rationale": rationale},
                parent_operation_id=str(request_record.get("operation_id", "")) or None,
                extra_fields={
                    "decision_id": decision_id,
                    "request_id": request_id,
                    "decision": decision,
                },
            )
            receipt = self._decision_receipt_from_record(record)
            self._json_response(HTTPStatus.OK, receipt)

        @staticmethod
        def _resolve_workspace_target(workspace_root: Path, file_path: str) -> Path | None:
            candidate = str(file_path or "").strip()
            if not candidate:
                return None
            joined = (workspace_root / candidate).resolve()
            if joined == workspace_root:
                return None
            if workspace_root not in joined.parents:
                return None
            return joined

        @staticmethod
        def _split_keepends(text: str) -> list[str]:
            return text.splitlines(keepends=True)

        def _apply_replace_range_content(
            self,
            current_content: str,
            *,
            start_line: int,
            end_line: int,
            replacement_content: str,
        ) -> tuple[bool, str | None]:
            lines = self._split_keepends(current_content)
            line_count = len(lines)
            if start_line < 1 or end_line < start_line:
                return (False, "range_conflict")
            if start_line > line_count or end_line > line_count:
                return (False, "range_conflict")
            replacement_lines = self._split_keepends(replacement_content)
            updated = lines[: start_line - 1] + replacement_lines + lines[end_line:]
            return (True, "".join(updated))

        def _build_atomic_patch_plan(
            self,
            patches: list[dict[str, Any]],
            *,
            workspace_root: Path,
        ) -> tuple[dict[str, Any] | None, str | None, str | None]:
            virtual: dict[str, dict[str, Any]] = {}
            ordered_keys: list[str] = []

            def _ensure_entry(target: Path) -> tuple[dict[str, Any] | None, str | None]:
                key = str(target)
                existing = virtual.get(key)
                if existing is not None:
                    return (existing, None)
                if target.exists():
                    if target.is_dir():
                        return (None, "invalid_patch")
                    try:
                        original_content = target.read_text(encoding="utf-8")
                    except OSError:
                        return (None, "io_error")
                    entry = {
                        "path": target,
                        "original_exists": True,
                        "original_content": original_content,
                        "exists": True,
                        "content": original_content,
                    }
                else:
                    entry = {
                        "path": target,
                        "original_exists": False,
                        "original_content": None,
                        "exists": False,
                        "content": None,
                    }
                virtual[key] = entry
                ordered_keys.append(key)
                return (entry, None)

            for patch in patches:
                op = str(patch.get("op", "")).strip()
                patch_id = str(patch.get("patch_id", "")).strip() or None
                file_path = str(patch.get("file", "")).strip()
                target = self._resolve_workspace_target(workspace_root, file_path)
                if target is None:
                    return (None, "invalid_patch", patch_id)
                entry, ensure_error = _ensure_entry(target)
                if ensure_error is not None or entry is None:
                    return (None, ensure_error or "io_error", patch_id)

                if op == "create_file":
                    content = patch.get("content")
                    if not isinstance(content, str):
                        return (None, "invalid_patch", patch_id)
                    if bool(entry.get("exists")):
                        return (None, "file_already_exists", patch_id)
                    entry["exists"] = True
                    entry["content"] = content
                    continue

                if op == "delete_file":
                    if not bool(entry.get("exists")):
                        return (None, "file_not_found", patch_id)
                    entry["exists"] = False
                    entry["content"] = None
                    continue

                if op == "replace_range":
                    start_line = patch.get("start_line")
                    end_line = patch.get("end_line")
                    content = patch.get("content")
                    if (
                        not isinstance(start_line, int)
                        or not isinstance(end_line, int)
                        or not isinstance(content, str)
                    ):
                        return (None, "invalid_patch", patch_id)
                    if not bool(entry.get("exists")):
                        return (None, "file_not_found", patch_id)
                    current_content = entry.get("content")
                    if not isinstance(current_content, str):
                        return (None, "invalid_patch", patch_id)
                    ok, updated = self._apply_replace_range_content(
                        current_content,
                        start_line=start_line,
                        end_line=end_line,
                        replacement_content=content,
                    )
                    if not ok:
                        return (None, str(updated or "range_conflict"), patch_id)
                    entry["content"] = str(updated)
                    continue

                return (None, "invalid_patch", patch_id)

            writes: list[tuple[Path, str]] = []
            deletes: list[Path] = []
            for key in ordered_keys:
                entry = virtual[key]
                target = entry["path"]
                if bool(entry.get("exists")):
                    content = entry.get("content")
                    if not isinstance(content, str):
                        return (None, "invalid_patch", None)
                    writes.append((target, content))
                else:
                    if bool(entry.get("original_exists")):
                        deletes.append(target)
            return (
                {
                    "writes": writes,
                    "deletes": deletes,
                },
                None,
                None,
            )

        @staticmethod
        def _cleanup_path(path: Path) -> None:
            with suppress(Exception):
                if path.exists():
                    if path.is_dir():
                        return
                    path.unlink()

        def _commit_atomic_patch_plan(self, plan: dict[str, Any]) -> tuple[bool, str | None]:
            writes = list(plan.get("writes", []))
            deletes = list(plan.get("deletes", []))

            temp_writes: list[tuple[Path, Path]] = []
            backups: list[tuple[Path, Path]] = []
            activated: list[Path] = []

            def _restore_backups() -> None:
                for backup, original in reversed(backups):
                    try:
                        if original.exists():
                            self._cleanup_path(original)
                        backup.rename(original)
                    except OSError:
                        pass

            try:
                # Stage all write payloads to temporary files first.
                for target, content in writes:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_name(f".{target.name}.bh_tmp_{uuid.uuid4().hex}")
                    tmp.write_text(content, encoding="utf-8")
                    temp_writes.append((tmp, target))

                # Backup all existing files that will be replaced.
                for _tmp, target in temp_writes:
                    if target.exists():
                        if target.is_dir():
                            raise ValueError("invalid_patch")
                        backup = target.with_name(f".{target.name}.bh_bak_{uuid.uuid4().hex}")
                        target.rename(backup)
                        backups.append((backup, target))

                # Backup files to be deleted so we can restore on failure.
                for target in deletes:
                    if target.exists():
                        if target.is_dir():
                            raise ValueError("invalid_patch")
                        backup = target.with_name(f".{target.name}.bh_bak_{uuid.uuid4().hex}")
                        target.rename(backup)
                        backups.append((backup, target))

                # Activate staged writes.
                for tmp, target in temp_writes:
                    tmp.rename(target)
                    activated.append(target)

                # Success; cleanup backups permanently.
                for backup, _original in backups:
                    self._cleanup_path(backup)
                backups.clear()
                return (True, None)
            except ValueError:
                for target in reversed(activated):
                    self._cleanup_path(target)
                _restore_backups()
                for tmp, _target in temp_writes:
                    self._cleanup_path(tmp)
                return (False, "invalid_patch")
            except OSError:
                # Best-effort rollback to prevent partial writes.
                for target in reversed(activated):
                    self._cleanup_path(target)
                _restore_backups()
                for tmp, _target in temp_writes:
                    self._cleanup_path(tmp)
                return (False, "io_error")
            finally:
                for tmp, _target in temp_writes:
                    self._cleanup_path(tmp)

        def _extract_request_patches(self, request_record: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
            refs = request_record.get("references") or {}
            if not isinstance(refs, dict):
                refs = {}
            details = request_record.get("details") or {}
            if not isinstance(details, dict):
                details = {}

            patch_ids = [
                str(item).strip()
                for item in list(refs.get("patch_ids", []))
                if str(item).strip()
            ]
            patches_payload = details.get("patches")
            if not isinstance(patches_payload, list):
                return ([], "patch_not_found")

            patch_index: dict[str, dict[str, Any]] = {}
            for raw in patches_payload:
                if not isinstance(raw, dict):
                    continue
                patch_id = str(raw.get("patch_id", "")).strip()
                if not patch_id:
                    continue
                patch_index[patch_id] = dict(raw)

            selected: list[dict[str, Any]] = []
            for patch_id in patch_ids:
                patch = patch_index.get(patch_id)
                if patch is None:
                    return ([], "patch_not_found")
                selected.append(patch)
            if not selected and patch_ids:
                return ([], "patch_not_found")
            return (selected, None)

        def _extract_request_tool_calls(self, request_record: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
            details = request_record.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            tool_calls_raw = details.get("tool_calls")
            if not isinstance(tool_calls_raw, list):
                return ([], "invalid_tool_call")

            normalized: list[dict[str, Any]] = []
            for index, call in enumerate(tool_calls_raw):
                if not isinstance(call, dict):
                    return ([], "invalid_tool_call")
                name = str(call.get("name", "")).strip()
                if not name:
                    return ([], "invalid_tool_call")
                arguments = call.get("arguments")
                if arguments is None:
                    arguments = {}
                if not isinstance(arguments, dict):
                    return ([], "invalid_tool_call")
                call_id = str(call.get("call_id", "")).strip() or f"tool_call_{index + 1}"
                normalized.append(
                    {
                        "name": name,
                        "arguments": dict(arguments),
                        "call_id": call_id,
                    }
                )
            if not normalized:
                return ([], "invalid_tool_call")
            return (normalized, None)

        def _handle_patch_execution(self, body: dict[str, Any]) -> None:
            request_id = str(body.get("request_id", "")).strip()
            actor = str(body.get("actor", "")).strip()
            if not request_id:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "failure_reason": "request_not_found", "error": "request_id is required"},
                )
                return
            if not actor:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "failure_reason": "invalid_patch", "error": "actor is required"},
                )
                return

            located = self._find_request_operation(request_id)
            if located is None:
                self._json_response(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "request_id": request_id, "failure_reason": "request_not_found"},
                )
                return
            request_type, request_record = located
            if request_type != "patch":
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "request_id": request_id, "failure_reason": "invalid_patch", "error": "request_id is not a patch request"},
                )
                return

            decision = self._find_decision_record("patch", request_id)
            if decision is None or str(decision.get("decision", "")).strip().lower() != "approved":
                self._json_response(
                    HTTPStatus.CONFLICT,
                    {"ok": False, "request_id": request_id, "failure_reason": "request_not_approved"},
                )
                return

            intent_id = str((request_record.get("references") or {}).get("intent_id", "")).strip()
            started_at = time.perf_counter()
            patches, extract_error = self._extract_request_patches(request_record)
            requested_patch_ids = [str(item.get("patch_id", "")).strip() for item in patches]
            target_files = [
                str(item.get("file", "")).strip()
                for item in patches
                if str(item.get("file", "")).strip()
            ]
            if extract_error is not None:
                refs = request_record.get("references") or {}
                if not isinstance(refs, dict):
                    refs = {}
                fallback_ids = [str(item).strip() for item in list(refs.get("patch_ids", [])) if str(item).strip()]
                failed_patch_id = fallback_ids[0] if fallback_ids else None
                duration_ms = int(round((time.perf_counter() - started_at) * 1000))
                attempt = self._record_operation(
                    "patch_apply_attempt",
                    status="executed",
                    actor=actor,
                    references={"intent_id": intent_id, "patch_request_id": request_id},
                    details={"patch_count": 0, "target_files": []},
                    parent_operation_id=str(request_record.get("operation_id", "")) or None,
                )
                result = self._record_operation(
                    "patch_apply_result",
                    status="failed",
                    actor=actor,
                    references={"intent_id": intent_id, "patch_request_id": request_id},
                    details={
                        "failure_reason": extract_error,
                        "failed_patch_id": failed_patch_id,
                        "applied_count": 0,
                        "applied_patch_ids": [],
                        "requested_patch_ids": fallback_ids,
                        "target_files": [],
                        "duration_ms": duration_ms,
                        "rollback_available": False,
                        "rollback_stub": True,
                        "rollback_performed": False,
                    },
                    parent_operation_id=attempt["operation_id"],
                )
                reconciliation = self._record_operation(
                    "execution_reconciliation",
                    status="reconciled",
                    actor=actor,
                    references={"intent_id": intent_id, "patch_request_id": request_id},
                    details={
                        "final_status": "failed",
                        "reconciled_reason": "patch_apply_result",
                        "rollback_available": False,
                        "rollback_stub": True,
                    },
                    parent_operation_id=result["operation_id"],
                    reconciles_operation_id=result["operation_id"],
                )
                self._json_response(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "request_id": request_id,
                        "status": "failed",
                        "failure_reason": extract_error,
                        "failed_patch_id": failed_patch_id,
                        "applied_count": 0,
                        "applied_patch_ids": [],
                        "requested_patch_ids": fallback_ids,
                        "target_files": [],
                        "duration_ms": duration_ms,
                        "rollback_available": False,
                        "rollback_stub": True,
                        "rollback_performed": False,
                        "reconciliation_operation_id": reconciliation.get("operation_id"),
                    },
                )
                return

            workspace_root = runtime.workspace_root.resolve()
            attempt = self._record_operation(
                "patch_apply_attempt",
                status="executed",
                actor=actor,
                references={"intent_id": intent_id, "patch_request_id": request_id},
                details={"patch_count": len(patches), "target_files": target_files},
                parent_operation_id=str(request_record.get("operation_id", "")) or None,
            )

            plan, failure_reason, failed_patch_id = self._build_atomic_patch_plan(
                patches,
                workspace_root=workspace_root,
            )
            applied_patch_ids: list[str] = []
            rollback_performed = False
            if failure_reason is None:
                committed, commit_error = self._commit_atomic_patch_plan(plan or {})
                if committed:
                    applied_patch_ids = [pid for pid in requested_patch_ids if pid]
                else:
                    failure_reason = commit_error or "io_error"
                    failed_patch_id = failed_patch_id or (requested_patch_ids[0] if requested_patch_ids else None)
                    rollback_performed = True

            if failure_reason and failure_reason not in PATCH_EXECUTION_FAILURE_REASONS:
                failure_reason = "io_error"

            success = failure_reason is None
            if success:
                failed_patch_id = None
            applied_count = len(applied_patch_ids) if success else 0
            duration_ms = int(round((time.perf_counter() - started_at) * 1000))
            result = self._record_operation(
                "patch_apply_result",
                status="applied" if success else "failed",
                actor=actor,
                references={"intent_id": intent_id, "patch_request_id": request_id},
                details={
                    "failure_reason": None if success else failure_reason,
                    "failed_patch_id": failed_patch_id,
                    "applied_count": applied_count,
                    "applied_patch_ids": applied_patch_ids,
                    "requested_patch_ids": requested_patch_ids,
                    "target_files": target_files,
                    "duration_ms": duration_ms,
                    "rollback_available": False,
                    "rollback_stub": True,
                    "rollback_performed": rollback_performed,
                },
                parent_operation_id=attempt["operation_id"],
            )
            reconciliation = self._record_operation(
                "execution_reconciliation",
                status="reconciled",
                actor=actor,
                references={"intent_id": intent_id, "patch_request_id": request_id},
                details={
                    "final_status": "applied" if success else "failed",
                    "reconciled_reason": "patch_apply_result",
                    "rollback_available": False,
                    "rollback_stub": True,
                    "rollback_performed": rollback_performed,
                },
                parent_operation_id=result["operation_id"],
                reconciles_operation_id=result["operation_id"],
            )

            if success:
                self._json_response(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "request_id": request_id,
                        "status": "applied",
                        "applied_count": applied_count,
                        "applied_patch_ids": applied_patch_ids,
                        "requested_patch_ids": requested_patch_ids,
                        "target_files": target_files,
                        "duration_ms": duration_ms,
                        "failure_reason": None,
                        "failed_patch_id": None,
                        "rollback_available": False,
                        "rollback_stub": True,
                        "rollback_performed": False,
                        "reconciliation_operation_id": reconciliation.get("operation_id"),
                    },
                )
                return

            self._json_response(
                HTTPStatus.CONFLICT,
                {
                    "ok": False,
                    "request_id": request_id,
                    "status": "failed",
                    "applied_count": applied_count,
                    "applied_patch_ids": applied_patch_ids,
                    "requested_patch_ids": requested_patch_ids,
                    "target_files": target_files,
                    "duration_ms": duration_ms,
                    "failure_reason": failure_reason,
                    "failed_patch_id": failed_patch_id,
                    "rollback_available": False,
                    "rollback_stub": True,
                    "rollback_performed": rollback_performed,
                    "reconciliation_operation_id": reconciliation.get("operation_id"),
                },
            )

        def _handle_tool_execution(self, body: dict[str, Any]) -> None:
            request_id = str(body.get("request_id", "")).strip()
            actor = str(body.get("actor", "")).strip()
            if not request_id:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "failure_reason": "request_not_found", "error": "request_id is required"},
                )
                return
            if not actor:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "failure_reason": "invalid_tool_call", "error": "actor is required"},
                )
                return

            located = self._find_request_operation(request_id)
            if located is None:
                self._json_response(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "request_id": request_id, "failure_reason": "request_not_found"},
                )
                return
            request_type, request_record = located
            if request_type != "tool":
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "request_id": request_id,
                        "failure_reason": "invalid_tool_call",
                        "error": "request_id is not a tool request",
                    },
                )
                return

            decision = self._find_decision_record("tool", request_id)
            if decision is None or str(decision.get("decision", "")).strip().lower() != "approved":
                self._json_response(
                    HTTPStatus.CONFLICT,
                    {"ok": False, "request_id": request_id, "failure_reason": "request_not_approved"},
                )
                return

            intent_id = str((request_record.get("references") or {}).get("intent_id", "")).strip()
            started_at = time.perf_counter()
            tool_calls, extract_error = self._extract_request_tool_calls(request_record)
            requested_tool_ids = [str(item.get("call_id", "")).strip() for item in tool_calls if str(item.get("call_id", "")).strip()]
            requested_tools = [str(item.get("name", "")).strip() for item in tool_calls if str(item.get("name", "")).strip()]
            if extract_error is not None:
                duration_ms = int(round((time.perf_counter() - started_at) * 1000))
                attempt = self._record_operation(
                    "tool_apply_attempt",
                    status="executed",
                    actor=actor,
                    references={"intent_id": intent_id, "tool_request_id": request_id},
                    details={"tool_count": 0, "tools": []},
                    parent_operation_id=str(request_record.get("operation_id", "")) or None,
                )
                result = self._record_operation(
                    "tool_apply_result",
                    status="failed",
                    actor=actor,
                    references={"intent_id": intent_id, "tool_request_id": request_id},
                    details={
                        "failure_reason": extract_error,
                        "failed_tool_id": None,
                        "executed_count": 0,
                        "executed_tool_ids": [],
                        "requested_tool_ids": [],
                        "requested_tools": [],
                        "duration_ms": duration_ms,
                        "rollback_available": False,
                        "rollback_stub": True,
                        "rollback_performed": False,
                    },
                    parent_operation_id=attempt["operation_id"],
                )
                reconciliation = self._record_operation(
                    "execution_reconciliation",
                    status="reconciled",
                    actor=actor,
                    references={"intent_id": intent_id, "tool_request_id": request_id},
                    details={
                        "final_status": "failed",
                        "reconciled_reason": "tool_apply_result",
                        "rollback_available": False,
                        "rollback_stub": True,
                    },
                    parent_operation_id=result["operation_id"],
                    reconciles_operation_id=result["operation_id"],
                )
                self._json_response(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "request_id": request_id,
                        "status": "failed",
                        "failure_reason": extract_error,
                        "failed_tool_id": None,
                        "executed_count": 0,
                        "executed_tool_ids": [],
                        "requested_tool_ids": [],
                        "requested_tools": [],
                        "duration_ms": duration_ms,
                        "rollback_available": False,
                        "rollback_stub": True,
                        "rollback_performed": False,
                        "reconciliation_operation_id": reconciliation.get("operation_id"),
                    },
                )
                return

            attempt = self._record_operation(
                "tool_apply_attempt",
                status="executed",
                actor=actor,
                references={"intent_id": intent_id, "tool_request_id": request_id},
                details={
                    "tool_count": len(tool_calls),
                    "tools": requested_tools,
                    "tool_call_ids": requested_tool_ids,
                },
                parent_operation_id=str(request_record.get("operation_id", "")) or None,
            )

            failure_reason: str | None = None
            failed_tool_id: str | None = None
            failed_tool_name: str | None = None
            executed_tool_ids: list[str] = []
            execution_outputs: list[dict[str, Any]] = []
            for call in tool_calls:
                tool_name = str(call.get("name", "")).strip()
                arguments = call.get("arguments")
                call_id = str(call.get("call_id", "")).strip() or None
                if not tool_name or not isinstance(arguments, dict):
                    failure_reason = "invalid_tool_call"
                    failed_tool_id = call_id
                    failed_tool_name = tool_name or None
                    break
                spec = runtime.registry.get(tool_name)
                if spec is None:
                    failure_reason = "tool_not_found"
                    failed_tool_id = call_id
                    failed_tool_name = tool_name
                    break
                validation_errors = runtime.registry.validate_arguments(tool_name, arguments)
                if validation_errors:
                    failure_reason = "invalid_tool_call"
                    failed_tool_id = call_id
                    failed_tool_name = tool_name
                    break
                try:
                    output = runtime.registry.execute(tool_name, arguments)
                except Exception:
                    failure_reason = "tool_execution_failed"
                    failed_tool_id = call_id
                    failed_tool_name = tool_name
                    break
                if call_id:
                    executed_tool_ids.append(call_id)
                execution_outputs.append(
                    {
                        "tool_id": call_id,
                        "name": tool_name,
                        "arguments": dict(arguments),
                        "output": output,
                    }
                )

            if failure_reason and failure_reason not in TOOL_EXECUTION_FAILURE_REASONS:
                failure_reason = "tool_execution_failed"

            success = failure_reason is None
            duration_ms = int(round((time.perf_counter() - started_at) * 1000))
            executed_count = len(executed_tool_ids)
            result = self._record_operation(
                "tool_apply_result",
                status="executed" if success else "failed",
                actor=actor,
                references={"intent_id": intent_id, "tool_request_id": request_id},
                details={
                    "failure_reason": None if success else failure_reason,
                    "failed_tool_id": failed_tool_id,
                    "failed_tool_name": failed_tool_name,
                    "executed_count": executed_count,
                    "executed_tool_ids": executed_tool_ids,
                    "requested_tool_ids": requested_tool_ids,
                    "requested_tools": requested_tools,
                    "duration_ms": duration_ms,
                    "rollback_available": False,
                    "rollback_stub": True,
                    "rollback_performed": False,
                    "execution_outputs": execution_outputs if success else [],
                },
                parent_operation_id=attempt["operation_id"],
            )
            reconciliation = self._record_operation(
                "execution_reconciliation",
                status="reconciled",
                actor=actor,
                references={"intent_id": intent_id, "tool_request_id": request_id},
                details={
                    "final_status": "executed" if success else "failed",
                    "reconciled_reason": "tool_apply_result",
                    "rollback_available": False,
                    "rollback_stub": True,
                    "rollback_performed": False,
                },
                parent_operation_id=result["operation_id"],
                reconciles_operation_id=result["operation_id"],
            )

            if success:
                self._json_response(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "request_id": request_id,
                        "status": "executed",
                        "executed_count": executed_count,
                        "executed_tool_ids": executed_tool_ids,
                        "requested_tool_ids": requested_tool_ids,
                        "requested_tools": requested_tools,
                        "duration_ms": duration_ms,
                        "failure_reason": None,
                        "failed_tool_id": None,
                        "failed_tool_name": None,
                        "rollback_available": False,
                        "rollback_stub": True,
                        "rollback_performed": False,
                        "reconciliation_operation_id": reconciliation.get("operation_id"),
                    },
                )
                return

            self._json_response(
                HTTPStatus.CONFLICT,
                {
                    "ok": False,
                    "request_id": request_id,
                    "status": "failed",
                    "executed_count": executed_count,
                    "executed_tool_ids": executed_tool_ids,
                    "requested_tool_ids": requested_tool_ids,
                    "requested_tools": requested_tools,
                    "duration_ms": duration_ms,
                    "failure_reason": failure_reason,
                    "failed_tool_id": failed_tool_id,
                    "failed_tool_name": failed_tool_name,
                    "rollback_available": False,
                    "rollback_stub": True,
                    "rollback_performed": False,
                    "reconciliation_operation_id": reconciliation.get("operation_id"),
                },
            )

        @staticmethod
        def _validate_patch_ops(raw_ops: Any) -> list[dict[str, Any]]:
            if raw_ops is None:
                return []
            if not isinstance(raw_ops, list):
                raise ValueError("patches must be a list")

            validated: list[dict[str, Any]] = []
            seen_patch_ids: set[str] = set()
            for index, raw in enumerate(raw_ops):
                if not isinstance(raw, dict):
                    raise ValueError(f"patches[{index}] must be an object")
                raw_patch_id = raw.get("patch_id")
                patch_id = str(raw_patch_id).strip() if raw_patch_id is not None else ""
                if not patch_id:
                    patch_id = f"PATCH_{index + 1:03d}"
                if patch_id in seen_patch_ids:
                    raise ValueError(f"patches[{index}].patch_id must be unique")
                seen_patch_ids.add(patch_id)
                op = str(raw.get("op", "")).strip()
                if op == "replace_range":
                    required = {"op", "file", "start_line", "end_line", "content"}
                    allowed = required | {"patch_id"}
                    extra = set(raw.keys()) - allowed
                    missing = [k for k in required if k not in raw]
                    if missing:
                        raise ValueError(f"patches[{index}] missing required fields: {', '.join(sorted(missing))}")
                    if extra:
                        raise ValueError(f"patches[{index}] has unknown fields: {', '.join(sorted(extra))}")
                    file_path = str(raw.get("file", "")).strip()
                    if not file_path:
                        raise ValueError(f"patches[{index}].file must be a non-empty string")
                    start_line = raw.get("start_line")
                    end_line = raw.get("end_line")
                    content = raw.get("content")
                    if not isinstance(start_line, int) or start_line < 1:
                        raise ValueError(f"patches[{index}].start_line must be an integer >= 1")
                    if not isinstance(end_line, int) or end_line < start_line:
                        raise ValueError(f"patches[{index}].end_line must be an integer >= start_line")
                    if not isinstance(content, str):
                        raise ValueError(f"patches[{index}].content must be a string")
                    validated.append(
                        {
                            "patch_id": patch_id,
                            "op": "replace_range",
                            "file": file_path,
                            "start_line": start_line,
                            "end_line": end_line,
                            "content": content,
                        }
                    )
                    continue

                if op == "create_file":
                    required = {"op", "file", "content"}
                    allowed = required | {"patch_id"}
                    extra = set(raw.keys()) - allowed
                    missing = [k for k in required if k not in raw]
                    if missing:
                        raise ValueError(f"patches[{index}] missing required fields: {', '.join(sorted(missing))}")
                    if extra:
                        raise ValueError(f"patches[{index}] has unknown fields: {', '.join(sorted(extra))}")
                    file_path = str(raw.get("file", "")).strip()
                    content = raw.get("content")
                    if not file_path:
                        raise ValueError(f"patches[{index}].file must be a non-empty string")
                    if not isinstance(content, str):
                        raise ValueError(f"patches[{index}].content must be a string")
                    validated.append({"patch_id": patch_id, "op": "create_file", "file": file_path, "content": content})
                    continue

                if op == "delete_file":
                    required = {"op", "file"}
                    allowed = required | {"patch_id"}
                    extra = set(raw.keys()) - allowed
                    missing = [k for k in required if k not in raw]
                    if missing:
                        raise ValueError(f"patches[{index}] missing required fields: {', '.join(sorted(missing))}")
                    if extra:
                        raise ValueError(f"patches[{index}] has unknown fields: {', '.join(sorted(extra))}")
                    file_path = str(raw.get("file", "")).strip()
                    if not file_path:
                        raise ValueError(f"patches[{index}].file must be a non-empty string")
                    validated.append({"patch_id": patch_id, "op": "delete_file", "file": file_path})
                    continue

                raise ValueError(
                    f"patches[{index}].op must be one of: replace_range, create_file, delete_file"
                )
            return validated

        @staticmethod
        def _validate_patch_requests(
            raw_requests: Any,
            patch_ids: list[str],
            *,
            instruction: str,
        ) -> list[dict[str, Any]]:
            if raw_requests is None:
                if not patch_ids:
                    return []
                reason = f"Apply {len(patch_ids)} proposed patch(es) from IDE request."
                return [
                    {
                        "request_id": "PATCH_REQ_001",
                        "patch_ids": patch_ids,
                        "reason": reason,
                        "risk": "low",
                        "requires_approval": True,
                    }
                ]

            if not isinstance(raw_requests, list):
                raise ValueError("patch_requests must be a list")

            known_patch_ids = set(patch_ids)
            normalized_requests: list[dict[str, Any]] = []
            seen_request_ids: set[str] = set()
            for index, raw in enumerate(raw_requests):
                if not isinstance(raw, dict):
                    raise ValueError(f"patch_requests[{index}] must be an object")
                allowed = {"request_id", "patch_ids", "reason", "risk", "requires_approval"}
                extra = set(raw.keys()) - allowed
                if extra:
                    raise ValueError(f"patch_requests[{index}] has unknown fields: {', '.join(sorted(extra))}")

                request_id = str(raw.get("request_id", "")).strip() or f"PATCH_REQ_{index + 1:03d}"
                if request_id in seen_request_ids:
                    raise ValueError(f"patch_requests[{index}].request_id must be unique")
                seen_request_ids.add(request_id)

                raw_patch_ids = raw.get("patch_ids")
                if not isinstance(raw_patch_ids, list) or not raw_patch_ids:
                    raise ValueError(f"patch_requests[{index}].patch_ids must be a non-empty list")
                normalized_patch_ids: list[str] = []
                seen_local_patch_ids: set[str] = set()
                for candidate in raw_patch_ids:
                    patch_id = str(candidate).strip()
                    if not patch_id:
                        raise ValueError(f"patch_requests[{index}].patch_ids contains an empty patch_id")
                    if patch_id in seen_local_patch_ids:
                        continue
                    seen_local_patch_ids.add(patch_id)
                    if patch_id not in known_patch_ids:
                        raise ValueError(
                            f"patch_requests[{index}].patch_ids references unknown patch_id: {patch_id}"
                        )
                    normalized_patch_ids.append(patch_id)

                reason = raw.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError(f"patch_requests[{index}].reason must be a non-empty string")

                risk = str(raw.get("risk", "")).strip().lower()
                if risk not in {"low", "medium", "high"}:
                    raise ValueError(f"patch_requests[{index}].risk must be one of: low, medium, high")

                requires_approval = raw.get("requires_approval")
                if not isinstance(requires_approval, bool):
                    raise ValueError(f"patch_requests[{index}].requires_approval must be a boolean")

                normalized_requests.append(
                    {
                        "request_id": request_id,
                        "patch_ids": normalized_patch_ids,
                        "reason": reason.strip(),
                        "risk": risk,
                        "requires_approval": requires_approval,
                    }
                )
            return normalized_requests

        @staticmethod
        def _extract_first_diff_patch(reply: str) -> str | None:
            match = re.search(r"```diff\s*(.*?)```", reply, flags=re.DOTALL | re.IGNORECASE)
            if match:
                patch = match.group(1).strip()
                return patch or None
            return None

        def _write_cors_headers(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin and _LOCAL_ORIGIN_RE.match(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    return CommsHandler


def make_server(
    runtime: RuntimeRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    truth_api: TruthAPI | None = None,
    assets_dir: Path | None = None,
) -> ThreadingHTTPServer:
    truth_api = truth_api or TruthAPI(runtime.log_path)
    assets_dir = assets_dir or (Path(__file__).parent.parent / "monitor_dashboard")
    return ThreadingHTTPServer((host, port), create_handler(runtime, truth_api, assets_dir))


class CommsServer:
    def __init__(self, runtime: RuntimeRuntime, *, host: str = "127.0.0.1", port: int = 0, truth_api: TruthAPI | None = None, assets_dir: Path | None = None) -> None:
        self.runtime = runtime
        self.truth_api = truth_api or TruthAPI(runtime.log_path)
        self.server = make_server(runtime, host=host, port=port, truth_api=self.truth_api, assets_dir=assets_dir)
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "CommsServer":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        with suppress(Exception):
            self.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runtime comms plane localhost control plane")
    parser.add_argument("--host", default=os.environ.get("RUNTIME_COMMS_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RUNTIME_COMMS_PORT", "8047")),
    )
    parser.add_argument(
        "--config-path",
        default=str(Path(__file__).parent.parent / "runtime_capabilities.md"),
    )
    parser.add_argument("--model", default=os.environ.get("LMSTUDIO_MODEL") or os.environ.get("OPENAI_MODEL"))
    parser.add_argument("--lmstudio-base-url", default=os.environ.get("LMSTUDIO_BASE_URL"))
    return parser


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists() or not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def main() -> int:
    _load_dotenv(Path(__file__).parent.parent / ".env")
    args = build_parser().parse_args()
    model = load_model(
        provider="lmstudio",
        model=args.model or os.environ.get("OPENAI_MODEL") or "openai/gpt-oss-20b",
        base_url=args.lmstudio_base_url or os.environ.get("OPENAI_BASE_URL"),
    )
    root = Path(args.config_path).parent
    workspace = root / "SYSTEMS"
    workspace.mkdir(parents=True, exist_ok=True)
    runtime_root = root / ".runtime"
    if not runtime_root.exists():
        # Fallback for legacy or if .runtime doesn't exist yet but .runtime_core does
        legacy_root = root / ".runtime_core"
        if legacy_root.exists():
            runtime_root = legacy_root
            
    runtime_root.mkdir(parents=True, exist_ok=True)
    from .events import trim_event_log
    trim_event_log(runtime_root / "events.jsonl")
    runtime = RuntimeRuntime(
        state_path=runtime_root / "state.json",
        log_path=runtime_root / "events.jsonl",
        mcp_config_path=runtime_root / "mcp.json",
        workspace_root=workspace,
        contract_path=Path(args.config_path),
        model=model,
    )
    server = make_server(runtime, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(f"Runtime comms plane listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
