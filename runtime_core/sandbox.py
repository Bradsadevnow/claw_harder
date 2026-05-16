from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from math import isfinite, isinf
from pathlib import Path
import re
import shutil
from time import time
from typing import Any, Callable
from uuid import uuid4


MEMORY_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class BudgetExhaustedError(ValueError):
    pass


_BUDGET_KINDS = frozenset({"memory_writes", "file_reads", "file_writes"})


@dataclass
class ResourceBudget:
    memory_writes: float = float("inf")
    file_reads: float = float("inf")
    file_writes: float = float("inf")

    def check(self, kind: str, cost: float) -> None:
        remaining = self._get(kind)
        if remaining < cost:
            raise BudgetExhaustedError(
                f"resource budget exhausted for '{kind}': need {cost:.4g}, have {remaining:.4g}"
            )

    def deduct(self, kind: str, cost: float) -> None:
        current = self._get(kind)
        if not isinf(current):
            setattr(self, kind, max(0.0, current - cost))

    def snapshot(self) -> dict[str, float]:
        return {
            "memory_writes": self.memory_writes,
            "file_reads": self.file_reads,
            "file_writes": self.file_writes,
        }

    def _get(self, kind: str) -> float:
        if kind not in _BUDGET_KINDS:
            raise ValueError(f"unknown resource budget kind: {kind!r}")
        return float(getattr(self, kind))


def _empty_memory() -> dict[str, Any]:
    return {
        "context": {},
        "facts": {},
        "tasks": [],
        "artifacts": [],
        "metadata": {"revision": 0},
        "version": MEMORY_SCHEMA_VERSION,
    }


@dataclass
class SandboxReceipt:
    resource: str
    operation: str
    path: str
    committed: bool
    cost: float
    before_hash: str
    after_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateSandbox:
    """Authoritative resource layer for durable runtime state.

    Phase 1 owns memory only. Runtime transcript frames still live in
    RuntimeState; durable agent memory must pass through this object.
    """

    def __init__(self, root: Path, budget: ResourceBudget | None = None, use_memory_json: bool = True) -> None:
        self.root = root.resolve()
        self.memory_path = self.root / "memory.json"
        self._budget = budget
        self._gate_anchor: dict[str, Any] | None = None
        self.use_memory_json = use_memory_json
        if self.use_memory_json:
            self.root.mkdir(parents=True, exist_ok=True)
            self._ensure_memory()
        else:
            self.root.mkdir(parents=True, exist_ok=True)

    def set_gate_anchor(self, anchor: dict[str, Any] | None) -> None:
        self._gate_anchor = deepcopy(anchor) if anchor is not None else None

    @classmethod
    def for_workspace(cls, workspace_root: Path, session_id: str = "default", use_memory_json: bool = True) -> "StateSandbox":
        session_id = cls._parse_session_id(session_id)
        # Point directly to the flat scratch directory logic
        return cls(workspace_root / ".runtime_core" / "scratch" / session_id, use_memory_json=use_memory_json)

    def set_budget(self, budget: ResourceBudget) -> None:
        self._budget = budget

    def budget_snapshot(self) -> dict[str, float] | None:
        return self._budget.snapshot() if self._budget is not None else None

    def _check_budget(self, kind: str, cost: float) -> None:
        if self._budget is not None:
            self._budget.check(kind, cost)

    def _deduct_budget(self, kind: str, cost: float) -> None:
        if self._budget is not None:
            self._budget.deduct(kind, cost)

    def reset(self) -> dict[str, Any]:
        """
        Reset sandbox memory and files to a clean state.
        Does NOT touch event log or runtime state.
        """
        if self.use_memory_json:
            self._write_memory(_empty_memory())
        
        files_cleared = False
        if self.files_root.exists():
            shutil.rmtree(str(self.files_root))
            self.files_root.mkdir(parents=True, exist_ok=True)
            files_cleared = True
            
        manifest_cleared = False
        if self.manifest_path.exists():
            self.manifest_path.unlink()
            manifest_cleared = True
            
        self._ensure_memory()
        return {
            "ok": True,
            "memory_cleared": True,
            "files_cleared": files_cleared,
            "manifest_cleared": manifest_cleared,
        }

    def read_memory(self) -> dict[str, Any]:
        if not self.use_memory_json:
            return _empty_memory()
        self._ensure_memory()
        return self._load_memory()

    def get_fact(self, key: str) -> dict[str, Any]:
        parts = self._parse_key(key)
        memory = self.read_memory()
        target: Any = memory.get("facts", {})
        for part in parts:
            if not isinstance(target, dict) or part not in target:
                return {"key": key, "value": None, "found": False}
            target = target[part]
        return {"key": key, "value": target, "found": True}

    def set_fact(self, key: str, value: Any, cost: float = 1.0) -> dict[str, Any]:
        parts = self._parse_key(key)
        previous: Any = None
        existed = False

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            nonlocal previous, existed
            target = memory["facts"]
            for part in parts[:-1]:
                child = target.get(part)
                if not isinstance(child, dict):
                    child = {}
                    target[part] = child
                target = child
            existed = parts[-1] in target
            previous = deepcopy(target.get(parts[-1]))
            target[parts[-1]] = value
            return {"key": key, "value": value, "previous": previous, "existed": existed}

        receipt, result = self._transaction(
            operation="set_fact",
            path=f"memory.facts.{key}",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def rollback_set_fact(self, key: str, previous: Any, existed: bool, cost: float = 0.0) -> dict[str, Any]:
        parts = self._parse_key(key)

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            target = memory["facts"]
            for part in parts[:-1]:
                child = target.get(part)
                if not isinstance(child, dict):
                    child = {}
                    target[part] = child
                target = child
            if existed:
                target[parts[-1]] = previous
            else:
                target.pop(parts[-1], None)
            return {"key": key, "restored": existed}

        receipt, result = self._transaction(
            operation="rollback_set_fact",
            path=f"memory.facts.{key}",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def append_task(self, description: str, status: str = "open", cost: float = 1.0) -> dict[str, Any]:
        if not description.strip():
            raise ValueError("task description cannot be empty")
        if not status.strip():
            raise ValueError("task status cannot be empty")
        task_id = ""

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            nonlocal task_id
            revision = int(memory["metadata"].get("revision", 0))
            task_id = f"task-{revision + 1}"
            task = {
                "id": task_id,
                "description": description,
                "status": status,
                "created_at": time(),
            }
            memory["tasks"].append(task)
            return {"task": deepcopy(task)}

        receipt, result = self._transaction(
            operation="append_task",
            path="memory.tasks",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def update_task(
        self,
        task_id: str,
        description: str | None = None,
        status: str | None = None,
        cost: float = 1.0,
    ) -> dict[str, Any]:
        task_id = self._parse_resource_id(task_id, label="task id")
        if description is None and status is None:
            raise ValueError("update_task requires description or status")

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            for task in memory["tasks"]:
                if task.get("id") != task_id:
                    continue
                previous = deepcopy(task)
                if description is not None:
                    if not description.strip():
                        raise ValueError("task description cannot be empty")
                    task["description"] = description
                if status is not None:
                    if not status.strip():
                        raise ValueError("task status cannot be empty")
                    task["status"] = status
                task["updated_at"] = time()
                return {"task": deepcopy(task), "previous": previous}
            raise KeyError(f"Unknown task id: {task_id}")

        receipt, result = self._transaction(
            operation="update_task",
            path=f"memory.tasks.{task_id}",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def rollback_update_task(self, task_id: str, previous: dict[str, Any], cost: float = 0.0) -> dict[str, Any]:
        task_id = self._parse_resource_id(task_id, label="task id")

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            for index, task in enumerate(memory["tasks"]):
                if task.get("id") == task_id:
                    memory["tasks"][index] = deepcopy(previous)
                    return {"task": deepcopy(previous)}
            raise KeyError(f"Unknown task id: {task_id}")

        receipt, result = self._transaction(
            operation="rollback_update_task",
            path=f"memory.tasks.{task_id}",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def remove_task(self, task_id: str, cost: float = 0.0) -> dict[str, Any]:
        task_id = self._parse_resource_id(task_id, label="task id")

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            for index in range(len(memory["tasks"]) - 1, -1, -1):
                if memory["tasks"][index].get("id") == task_id:
                    task = memory["tasks"].pop(index)
                    return {"removed": True, "task": deepcopy(task)}
            return {"removed": False}

        receipt, result = self._transaction(
            operation="remove_task",
            path=f"memory.tasks.{task_id}",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def store_artifact(
        self,
        name: str,
        path: str,
        metadata: dict[str, Any] | None = None,
        cost: float = 1.0,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("artifact name cannot be empty")
        path = self._parse_relative_resource_path(path)
        artifact_id = ""

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            nonlocal artifact_id
            revision = int(memory["metadata"].get("revision", 0))
            artifact_id = f"artifact-{revision + 1}"
            artifact = {
                "id": artifact_id,
                "name": name,
                "path": path,
                "metadata": deepcopy(metadata or {}),
                "created_at": time(),
            }
            memory["artifacts"].append(artifact)
            return {"artifact": deepcopy(artifact)}

        receipt, result = self._transaction(
            operation="store_artifact",
            path="memory.artifacts",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def remove_artifact(self, artifact_id: str, cost: float = 0.0) -> dict[str, Any]:
        artifact_id = self._parse_resource_id(artifact_id, label="artifact id")

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            for index in range(len(memory["artifacts"]) - 1, -1, -1):
                if memory["artifacts"][index].get("id") == artifact_id:
                    artifact = memory["artifacts"].pop(index)
                    return {"removed": True, "artifact": deepcopy(artifact)}
            return {"removed": False}

        receipt, result = self._transaction(
            operation="remove_artifact",
            path=f"memory.artifacts.{artifact_id}",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def append_context_note(self, note: str, cost: float = 1.0) -> dict[str, Any]:
        if not note.strip():
            raise ValueError("note cannot be empty")

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            notes = memory["context"].setdefault("notes", [])
            if not isinstance(notes, list):
                raise ValueError("memory.context.notes must be a list")
            notes.append(note)
            return {"stored": note, "note_count": len(notes)}

        receipt, result = self._transaction(
            operation="append_context_note",
            path="memory.context.notes",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def remove_context_note(self, note: str, cost: float = 0.0) -> dict[str, Any]:
        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            notes = memory["context"].setdefault("notes", [])
            if not isinstance(notes, list):
                raise ValueError("memory.context.notes must be a list")
            removed = False
            for index in range(len(notes) - 1, -1, -1):
                if notes[index] == note:
                    notes.pop(index)
                    removed = True
                    break
            return {"removed": removed, "note_count": len(notes)}

        receipt, result = self._transaction(
            operation="remove_context_note",
            path="memory.context.notes",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def recall_notes(self, query: str = "", limit: int = 5) -> dict[str, Any]:
        memory = self.read_memory()
        raw_notes = memory.get("context", {}).get("notes", [])
        notes = [str(note) for note in raw_notes] if isinstance(raw_notes, list) else []
        if query:
            notes = [note for note in notes if query.lower() in note.lower()]
            return {"notes": notes[:limit]}
        return {"notes": notes[-limit:]}

    # -------------------------------------------------------------------------
    # File resources
    # -------------------------------------------------------------------------

    @property
    def files_root(self) -> Path:
        return self.root / "files"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def read_manifest(self) -> dict[str, Any]:
        return self._load_manifest()

    def read_file(self, path: str, cost: float = 0.5) -> dict[str, Any]:
        normalized, resolved = self._resolve_sandbox_file_path(path)
        cost = self._parse_cost(cost)
        self._check_budget("file_reads", cost)
        if not resolved.exists():
            raise FileNotFoundError(f"sandbox file not found: {path!r}")
        if not resolved.is_file():
            raise ValueError(f"sandbox path is not a file: {path!r}")
        raw = resolved.read_bytes()
        content = raw.decode("utf-8")
        file_hash = sha256(raw).hexdigest()
        self._deduct_budget("file_reads", cost)
        receipt = SandboxReceipt(
            resource="file",
            operation="read_file",
            path=f"files/{normalized}",
            committed=True,
            cost=cost,
            before_hash=file_hash,
            after_hash=file_hash,
            metadata=self._gate_bound_metadata({"size_bytes": len(raw)}),
        )
        return {
            "receipt": receipt.to_dict(),
            "path": normalized,
            "content": content,
            "size_bytes": len(raw),
        }

    def write_file(self, path: str, content: str, cost: float = 1.0) -> dict[str, Any]:
        if not isinstance(content, str):
            raise TypeError("file content must be a string")
        normalized, resolved = self._resolve_sandbox_file_path(path)
        cost = self._parse_cost(cost)
        self._check_budget("file_writes", cost)

        before_hash = ""
        before_existed = resolved.exists() and resolved.is_file()
        previous_content: str | None = None
        if before_existed:
            raw_before = resolved.read_bytes()
            previous_content = raw_before.decode("utf-8")
            before_hash = sha256(raw_before).hexdigest()

        raw_after = content.encode("utf-8")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_bytes(raw_after)
        tmp.replace(resolved)

        after_hash = sha256(raw_after).hexdigest()
        self._update_manifest_entry(normalized, resolved, "write")
        self._deduct_budget("file_writes", cost)

        receipt = SandboxReceipt(
            resource="file",
            operation="write_file",
            path=f"files/{normalized}",
            committed=True,
            cost=cost,
            before_hash=before_hash,
            after_hash=after_hash,
            metadata=self._gate_bound_metadata({"existed": before_existed, "size_bytes": len(raw_after)}),
        )
        return {
            "receipt": receipt.to_dict(),
            "path": normalized,
            "size_bytes": len(raw_after),
            "existed": before_existed,
            "previous_content": previous_content,
        }

    def rollback_write_file(
        self,
        path: str,
        previous_content: str | None,
        existed: bool,
        cost: float = 0.0,
    ) -> dict[str, Any]:
        normalized, resolved = self._resolve_sandbox_file_path(path)
        cost = self._parse_cost(cost)
        self._check_budget("file_writes", cost)

        before_hash = ""
        if resolved.exists() and resolved.is_file():
            before_hash = sha256(resolved.read_bytes()).hexdigest()

        if existed and previous_content is not None:
            raw = previous_content.encode("utf-8")
            resolved.parent.mkdir(parents=True, exist_ok=True)
            tmp = resolved.with_suffix(resolved.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(resolved)
            self._update_manifest_entry(normalized, resolved, "write")
            after_hash = sha256(raw).hexdigest()
        else:
            if resolved.exists():
                resolved.unlink()
            self._update_manifest_entry(normalized, resolved, "delete")
            after_hash = ""

        self._deduct_budget("file_writes", cost)
        receipt = SandboxReceipt(
            resource="file",
            operation="rollback_write_file",
            path=f"files/{normalized}",
            committed=True,
            cost=cost,
            before_hash=before_hash,
            after_hash=after_hash,
            metadata=self._gate_bound_metadata({"restored": existed}),
        )
        return {"receipt": receipt.to_dict(), "path": normalized, "restored": existed}

    def delete_file(self, path: str, cost: float = 0.5) -> dict[str, Any]:
        normalized, resolved = self._resolve_sandbox_file_path(path)
        cost = self._parse_cost(cost)
        self._check_budget("file_writes", cost)

        if not resolved.exists():
            receipt = SandboxReceipt(
                resource="file",
                operation="delete_file",
                path=f"files/{normalized}",
                committed=True,
                cost=cost,
                before_hash="",
                after_hash="",
                metadata=self._gate_bound_metadata({"removed": False}),
            )
            return {
                "receipt": receipt.to_dict(),
                "path": normalized,
                "removed": False,
                "previous_content": None,
            }

        if not resolved.is_file():
            raise ValueError(f"sandbox path is not a file: {path!r}")

        raw = resolved.read_bytes()
        previous_content = raw.decode("utf-8")
        before_hash = sha256(raw).hexdigest()

        resolved.unlink()
        self._update_manifest_entry(normalized, resolved, "delete")
        self._deduct_budget("file_writes", cost)

        receipt = SandboxReceipt(
            resource="file",
            operation="delete_file",
            path=f"files/{normalized}",
            committed=True,
            cost=cost,
            before_hash=before_hash,
            after_hash="",
            metadata=self._gate_bound_metadata({"removed": True}),
        )
        return {
            "receipt": receipt.to_dict(),
            "path": normalized,
            "removed": True,
            "previous_content": previous_content,
        }

    def rollback_delete_file(self, path: str, previous_content: str, cost: float = 0.0) -> dict[str, Any]:
        normalized, resolved = self._resolve_sandbox_file_path(path)
        cost = self._parse_cost(cost)
        self._check_budget("file_writes", cost)

        raw = previous_content.encode("utf-8")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(resolved)

        after_hash = sha256(raw).hexdigest()
        self._update_manifest_entry(normalized, resolved, "write")
        self._deduct_budget("file_writes", cost)

        receipt = SandboxReceipt(
            resource="file",
            operation="rollback_delete_file",
            path=f"files/{normalized}",
            committed=True,
            cost=cost,
            before_hash="",
            after_hash=after_hash,
            metadata=self._gate_bound_metadata({"restored": True}),
        )
        return {"receipt": receipt.to_dict(), "path": normalized, "restored": True}

    def list_files(self, path: str = "") -> dict[str, Any]:
        if path:
            normalized = self._parse_relative_resource_path(path)
            candidate = self.files_root / normalized
            if candidate.exists() and candidate.is_symlink():
                raise ValueError(f"sandbox path cannot be a symlink: {path!r}")
            resolved_dir = candidate.resolve()
            self._assert_inside_root(resolved_dir)
            display_path = normalized
        else:
            resolved_dir = self.files_root.resolve()
            self._assert_inside_root(resolved_dir)
            display_path = "."

        if not resolved_dir.exists():
            return {"path": display_path, "entries": []}

        if not resolved_dir.is_dir():
            raise ValueError(f"sandbox path is not a directory: {path!r}")

        entries = []
        for entry in sorted(resolved_dir.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_symlink():
                continue
            rel = str(entry.relative_to(self.files_root))
            entries.append({
                "name": entry.name,
                "path": rel,
                "kind": "dir" if entry.is_dir() else "file",
                "size_bytes": entry.stat().st_size if entry.is_file() else None,
            })

        return {"path": display_path, "entries": entries}

    # -------------------------------------------------------------------------
    # Lifecycle operations
    # -------------------------------------------------------------------------

    @property
    def snapshots_root(self) -> Path:
        return self.root / "snapshots"

    def compress_memory(self, max_notes: int = 20, cost: float = 1.0) -> dict[str, Any]:
        """Truncate context notes to the most recent max_notes entries."""
        if max_notes < 0:
            raise ValueError("max_notes must be non-negative")

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            notes = memory["context"].setdefault("notes", [])
            if not isinstance(notes, list):
                raise ValueError("memory.context.notes must be a list")
            before_count = len(notes)
            if before_count > max_notes:
                memory["context"]["notes"] = notes[-max_notes:] if max_notes > 0 else []
            return {
                "before_count": before_count,
                "after_count": len(memory["context"]["notes"]),
                "removed_count": max(0, before_count - max_notes),
            }

        receipt, result = self._transaction(
            operation="compress_memory",
            path="memory.context.notes",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def prune_memory(self, prune_done_tasks: bool = True, cost: float = 1.0) -> dict[str, Any]:
        """Remove completed/closed tasks from sandbox memory."""
        _done_statuses = frozenset({"done", "closed", "completed"})

        def mutate(memory: dict[str, Any]) -> dict[str, Any]:
            removed_tasks = 0
            if prune_done_tasks:
                before = len(memory["tasks"])
                memory["tasks"] = [
                    t for t in memory["tasks"]
                    if t.get("status") not in _done_statuses
                ]
                removed_tasks = before - len(memory["tasks"])
            return {"removed_tasks": removed_tasks, "prune_done_tasks": prune_done_tasks}

        receipt, result = self._transaction(
            operation="prune_memory",
            path="memory.tasks",
            cost=cost,
            mutate=mutate,
        )
        return {"receipt": receipt.to_dict(), **result}

    def snapshot_state(self, label: str = "", cost: float = 2.0) -> dict[str, Any]:
        """Create a versioned, point-in-time snapshot of sandbox memory and files."""
        cost = self._parse_cost(cost)
        self._check_budget("file_writes", cost)

        snapshot_id = self._generate_snapshot_id()
        snapshot_dir = self.snapshots_root / snapshot_id
        self._assert_inside_root(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        memory = self._load_memory()
        before_hash = self._hash(memory)
        manifest = self._load_manifest()

        meta = {
            "snapshot_id": snapshot_id,
            "label": label,
            "created_at": time(),
            "memory_hash": before_hash,
            "memory_revision": int(memory["metadata"].get("revision", 0)),
        }

        (snapshot_dir / "meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8"
        )
        (snapshot_dir / "memory.json").write_text(
            json.dumps(memory, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8"
        )
        (snapshot_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8"
        )

        if self.files_root.exists():
            shutil.copytree(str(self.files_root), str(snapshot_dir / "files"))

        self._deduct_budget("file_writes", cost)

        receipt = SandboxReceipt(
            resource="snapshot",
            operation="snapshot_state",
            path=f"snapshots/{snapshot_id}",
            committed=True,
            cost=cost,
            before_hash=before_hash,
            after_hash=before_hash,
            metadata=self._gate_bound_metadata({
                "snapshot_id": snapshot_id,
                "label": label,
                "memory_revision": meta["memory_revision"],
            }),
        )
        return {"receipt": receipt.to_dict(), "snapshot_id": snapshot_id, "label": label}

    def restore_snapshot(self, snapshot_id: str, cost: float = 2.0) -> dict[str, Any]:
        """Restore sandbox memory and files from a previously created snapshot."""
        snapshot_id = self._parse_resource_id(snapshot_id, label="snapshot id")
        snapshot_dir = self.snapshots_root / snapshot_id
        self._assert_inside_root(snapshot_dir)

        cost = self._parse_cost(cost)
        self._check_budget("file_writes", cost)

        if not (snapshot_dir / "meta.json").exists():
            raise FileNotFoundError(f"snapshot not found: {snapshot_id!r}")

        before_memory = self._load_memory()
        before_hash = self._hash(before_memory)

        snap_memory = json.loads((snapshot_dir / "memory.json").read_text(encoding="utf-8"))
        self._write_memory(snap_memory)

        files_snap = snapshot_dir / "files"
        if self.files_root.exists():
            shutil.rmtree(str(self.files_root))
        if files_snap.exists():
            shutil.copytree(str(files_snap), str(self.files_root))
        else:
            self.files_root.mkdir(parents=True, exist_ok=True)

        snap_manifest_path = snapshot_dir / "manifest.json"
        if snap_manifest_path.exists():
            snap_manifest = json.loads(snap_manifest_path.read_text(encoding="utf-8"))
            self._write_manifest(snap_manifest)

        after_hash = self._hash(snap_memory)
        self._deduct_budget("file_writes", cost)

        receipt = SandboxReceipt(
            resource="snapshot",
            operation="restore_snapshot",
            path=f"snapshots/{snapshot_id}",
            committed=True,
            cost=cost,
            before_hash=before_hash,
            after_hash=after_hash,
            metadata=self._gate_bound_metadata({"snapshot_id": snapshot_id}),
        )
        return {"receipt": receipt.to_dict(), "snapshot_id": snapshot_id, "restored": True}

    def list_snapshots(self) -> dict[str, Any]:
        """List available snapshots in this sandbox."""
        if not self.snapshots_root.exists():
            return {"snapshots": []}
        snapshots = []
        for entry in sorted(self.snapshots_root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            meta_path = entry / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            snapshots.append({
                "snapshot_id": meta.get("snapshot_id", entry.name),
                "label": meta.get("label", ""),
                "created_at": meta.get("created_at"),
                "memory_revision": meta.get("memory_revision"),
            })
        return {"snapshots": snapshots}

    def _generate_snapshot_id(self) -> str:
        ts = int(time() * 1000)
        uid = uuid4().hex[:8]
        return f"snap-{ts}-{uid}"

    def _gate_bound_metadata(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(metadata or {})
        if self._gate_anchor is None:
            return payload
        payload["gate_anchor_id"] = self._gate_anchor.get("id")
        payload["gate_anchor_issued_at"] = self._gate_anchor.get("issued_at")
        payload["gate_anchor_expires_at"] = self._gate_anchor.get("expires_at")
        payload["gate_anchor_issued_at_monotonic"] = self._gate_anchor.get("issued_at_monotonic")
        payload["gate_anchor_expires_at_monotonic"] = self._gate_anchor.get("expires_at_monotonic")
        payload["gate_anchor_clock_domain"] = self._gate_anchor.get("clock_domain")
        payload["gate_anchor_ttl_seconds"] = self._gate_anchor.get("ttl_seconds")
        payload["gate_anchor_runtime_state_hash"] = self._gate_anchor.get("runtime_state_hash")
        payload["gate_anchor_persisted_state_hash"] = self._gate_anchor.get("persisted_state_hash")
        payload["gate_anchor_sandbox_memory_hash"] = self._gate_anchor.get("sandbox_memory_hash")
        payload["gate_anchor_sandbox_manifest_hash"] = self._gate_anchor.get("sandbox_manifest_hash")
        payload["gate_anchor_reality_hash"] = self._gate_anchor.get("reality_hash")
        return payload

    def _transaction(
        self,
        operation: str,
        path: str,
        cost: float,
        mutate: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> tuple[SandboxReceipt, dict[str, Any]]:
        cost = self._parse_cost(cost)
        self._check_budget("memory_writes", cost)
        before = self._load_memory()
        before_revision = int(before["metadata"].get("revision", 0))
        before_hash = self._hash(before)
        working = deepcopy(before)
        result = mutate(working)
        self._normalize_memory(working)
        working["metadata"]["revision"] = before_revision + 1
        self._assert_json_serializable(working, context="sandbox memory")
        after_hash = self._hash(working)
        self._write_memory(working)
        self._deduct_budget("memory_writes", cost)
        return (
            SandboxReceipt(
                resource="memory",
                operation=operation,
                path=path,
                committed=True,
                cost=cost,
                before_hash=before_hash,
                after_hash=after_hash,
                metadata=self._gate_bound_metadata({
                    "before_revision": before_revision,
                    "after_revision": before_revision + 1,
                }),
            ),
            result,
        )

    def _ensure_memory(self) -> None:
        if not self.use_memory_json:
            return
        if not self.memory_path.exists():
            self._write_memory(_empty_memory())
            return
        payload = json.loads(self.memory_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("sandbox memory must be a JSON object")
        memory = deepcopy(payload)
        self._normalize_memory(memory)
        if memory != payload:
            self._write_memory(memory)

    def _load_memory(self) -> dict[str, Any]:
        if not self.use_memory_json:
            return _empty_memory()
        try:
            payload = json.loads(self.memory_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = _empty_memory()
        if not isinstance(payload, dict):
            raise ValueError("sandbox memory must be a JSON object")
        self._normalize_memory(payload)
        return payload

    def _write_memory(self, memory: dict[str, Any]) -> None:
        if not self.use_memory_json:
            return
        self._assert_inside_root(self.memory_path)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp_path = self.memory_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(memory, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8")
        tmp_path.replace(self.memory_path)

    def _assert_inside_root(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError(f"path '{path}' escapes sandbox root")

    @staticmethod
    def _normalize_memory(memory: dict[str, Any]) -> None:
        defaults = _empty_memory()
        for key, default in defaults.items():
            if key not in memory:
                memory[key] = deepcopy(default)
        if not isinstance(memory["context"], dict):
            raise ValueError("memory.context must be an object")
        if not isinstance(memory["facts"], dict):
            raise ValueError("memory.facts must be an object")
        if not isinstance(memory["tasks"], list):
            raise ValueError("memory.tasks must be a list")
        if not isinstance(memory["artifacts"], list):
            raise ValueError("memory.artifacts must be a list")
        if not isinstance(memory["metadata"], dict):
            raise ValueError("memory.metadata must be an object")
        memory["version"] = MEMORY_SCHEMA_VERSION
        memory["metadata"]["revision"] = int(memory["metadata"].get("revision", 0))

    @staticmethod
    def _hash(memory: dict[str, Any]) -> str:
        body = json.dumps(memory, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return sha256(body.encode("utf-8")).hexdigest()

    def _resolve_sandbox_file_path(self, path: str) -> tuple[str, Path]:
        normalized = self._parse_relative_resource_path(path)
        candidate = self.files_root / normalized
        if candidate.exists() and candidate.is_symlink():
            raise ValueError(f"sandbox file path cannot be a symlink: {path!r}")
        resolved = candidate.resolve()
        self._assert_inside_root(resolved)
        return normalized, resolved

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"resources": [], "version": MANIFEST_SCHEMA_VERSION}
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"resources": [], "version": MANIFEST_SCHEMA_VERSION}
        if not isinstance(payload, dict):
            raise ValueError("sandbox manifest must be a JSON object")
        if not isinstance(payload.get("resources"), list):
            payload["resources"] = []
        return payload

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self._assert_inside_root(self.manifest_path)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True),
            encoding="utf-8",
        )
        tmp.replace(self.manifest_path)

    def _update_manifest_entry(self, path: str, file_path: Path, operation: str) -> None:
        manifest = self._load_manifest()
        resources = manifest["resources"]
        if operation == "delete":
            manifest["resources"] = [r for r in resources if r.get("path") != path]
        else:
            raw = file_path.read_bytes()
            file_hash = sha256(raw).hexdigest()
            size_bytes = len(raw)
            now = time()
            existing = next((r for r in resources if r.get("path") == path), None)
            if existing is not None:
                updated = {
                    **existing,
                    "hash": file_hash,
                    "size_bytes": size_bytes,
                    "version": existing.get("version", 0) + 1,
                    "updated_at": now,
                }
                manifest["resources"] = [
                    updated if r.get("path") == path else r for r in resources
                ]
            else:
                manifest["resources"].append({
                    "type": "file",
                    "path": path,
                    "version": 1,
                    "hash": file_hash,
                    "size_bytes": size_bytes,
                    "created_at": now,
                    "updated_at": now,
                })
        self._write_manifest(manifest)

    @staticmethod
    def _parse_key(key: str) -> list[str]:
        raw_parts = key.split(".")
        parts = [part.strip() for part in raw_parts]
        if not parts or any(not part for part in parts):
            raise ValueError("fact key cannot be empty")
        for part in parts:
            if not KEY_SEGMENT_RE.fullmatch(part):
                raise ValueError(f"fact key contains an invalid segment: {part!r}")
        return parts

    @staticmethod
    def _parse_session_id(session_id: str) -> str:
        normalized = session_id.strip()
        if not SANDBOX_ID_RE.fullmatch(normalized):
            raise ValueError("sandbox session id must be 1-64 characters of letters, numbers, dot, dash, or underscore")
        if normalized in {".", ".."}:
            raise ValueError("sandbox session id cannot be a relative path segment")
        return normalized

    @staticmethod
    def _parse_resource_id(value: str, label: str) -> str:
        normalized = value.strip()
        if not SANDBOX_ID_RE.fullmatch(normalized):
            raise ValueError(f"{label} must be 1-64 characters of letters, numbers, dot, dash, or underscore")
        if normalized in {".", ".."}:
            raise ValueError(f"{label} cannot be a relative path segment")
        return normalized

    @staticmethod
    def _parse_relative_resource_path(path: str) -> str:
        normalized = path.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("artifact path cannot be empty")
        raw_parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise ValueError("artifact path cannot contain empty or relative traversal segments")
        candidate = Path(normalized)
        if candidate.is_absolute():
            raise ValueError("artifact path must be relative")
        return normalized

    @staticmethod
    def _parse_cost(cost: float) -> float:
        normalized = float(cost)
        if not isfinite(normalized) or normalized < 0.0:
            raise ValueError("sandbox transaction cost must be a finite non-negative number")
        return normalized

    @staticmethod
    def _assert_json_serializable(value: Any, context: str) -> None:
        try:
            json.dumps(value, ensure_ascii=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{context} must be JSON serializable") from exc
