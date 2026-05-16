from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
import inspect
import json
from pathlib import Path
from typing import Any, Callable

from ..memory import MemoryStore
from ..sandbox import StateSandbox
from .repair_tool import RepairPatchTool


from .types import ToolHandler, ToolPreflight, ToolRollback, ToolBindingResult, ToolSpec


def _infer_safety_level(
    *,
    mutates_state: bool,
    external_side_effect: bool,
    source: str,
    rollback: ToolRollback | None,
) -> str:
    if external_side_effect or source.startswith("mcp:"):
        return "irreversible"
    if mutates_state and rollback is not None:
        return "reversible"
    if mutates_state:
        return "irreversible"
    return "read_only"


def _schema_type_matches(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def _validate_schema_value(name: str, value: Any, schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _schema_type_matches(expected_type, value):
        errors.append(f"Argument '{name}' must be of type '{expected_type}'.")
        return errors

    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and isinstance(value, (int, float)) and value < minimum:
        errors.append(f"Argument '{name}' must be >= {minimum}.")
    if maximum is not None and isinstance(value, (int, float)) and value > maximum:
        errors.append(f"Argument '{name}' must be <= {maximum}.")

    min_length = schema.get("minLength")
    max_length = schema.get("maxLength")
    if min_length is not None and isinstance(value, str) and len(value) < min_length:
        errors.append(f"Argument '{name}' must be at least {min_length} characters.")
    if max_length is not None and isinstance(value, str) and len(value) > max_length:
        errors.append(f"Argument '{name}' must be at most {max_length} characters.")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"Argument '{name}' must be one of: {', '.join(str(item) for item in enum)}.")
    return errors


def validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    if not isinstance(arguments, dict):
        return ["Tool arguments must be a JSON object."]

    if schema.get("type") not in {None, "object"}:
        return []

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    errors: list[str] = []

    for key in required:
        if key not in arguments:
            errors.append(f"Missing required argument '{key}'.")

    if isinstance(properties, dict):
        for key in arguments:
            if key not in properties:
                errors.append(f"Unexpected argument '{key}'.")
        for key, value in arguments.items():
            prop_schema = properties.get(key)
            if not isinstance(prop_schema, dict):
                continue
            errors.extend(_validate_schema_value(key, value, prop_schema))

    return errors




class ToolRegistry:
    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: ToolHandler,
        source: str = "local",
        scope: str = "local_workspace",
        mutates_state: bool = False,
        high_risk: bool = False,
        external_side_effect: bool = False,
        reads_sensitive_data: bool = False,
        scope_root: str = ".",
        preflight: ToolPreflight | None = None,
        rollback: ToolRollback | None = None,
        safety_level: str | None = None,
    ) -> None:
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            schema=schema,
            handler=handler,
            source=source,
            scope=scope,
            mutates_state=mutates_state,
            high_risk=high_risk,
            external_side_effect=external_side_effect,
            reads_sensitive_data=reads_sensitive_data,
            scope_root=scope_root,
            preflight=preflight,
            rollback=rollback,
            safety_level=safety_level
            or _infer_safety_level(
                mutates_state=mutates_state,
                external_side_effect=external_side_effect,
                source=source,
                rollback=rollback,
            ),
        )

    def list_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def render_tool_descriptions(self) -> str:
        """Renders tools into a readable Markdown-like format for system prompt injection."""
        lines = []
        for spec in self.list_specs():
            lines.append(f"### {spec.name}")
            lines.append(f"Description: {spec.description}")
            lines.append(f"Safety Level: {spec.safety_level}")
            lines.append("Schema:")
            lines.append(f"```json\n{json.dumps(spec.schema, indent=2)}\n```")
            lines.append("")
        return "\n".join(lines)

    def names(self) -> set[str]:
        return set(self._tools.keys())

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name].handler(**arguments)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> list[str]:
        spec = self.get(name)
        if spec is None:
            return [f"Unknown tool: {name}"]
        errors = validate_tool_arguments(spec.schema, arguments)
        if spec.preflight is not None:
            errors.extend(spec.preflight(arguments))
        return errors

    def rollback(self, name: str, value: Any, arguments: dict[str, Any]) -> None:
        spec = self.get(name)
        if spec is None or spec.rollback is None:
            return
        spec.rollback(value, arguments)


def build_default_registry(
    memory: MemoryStore,
    workspace_root: Path,
    status_provider: Callable[[], dict[str, Any]],
    sandbox: StateSandbox | None = None,
    repair_lab: Any = None,
    ledger: CanonLedger | None = None,
) -> ToolRegistry:
    registry = ToolRegistry(memory=memory)
    workspace_root = workspace_root.resolve()
    sandbox = sandbox or StateSandbox.for_workspace(workspace_root)
    temp_notes_root = workspace_root / ".runtime" / "temp_notes"

    def resolve_workspace_path(raw_path: str) -> Path:
        candidate = (workspace_root / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path).resolve()
        if candidate != workspace_root and workspace_root not in candidate.parents:
            raise ValueError(f"path '{raw_path}' escapes workspace root")
        return candidate

    def safe_temp_note_name(raw_name: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in raw_name).strip("_") or "note"

    def preflight_list_workspace_files(arguments: dict[str, Any]) -> list[str]:
        path = str(arguments.get("path", "."))
        try:
            root = resolve_workspace_path(path)
        except ValueError as exc:
            return [str(exc)]
        if not root.exists():
            return [f"path '{path}' does not exist"]
        if not root.is_dir():
            return [f"path '{path}' is not a directory"]
        return []

    def preflight_read_text_file(arguments: dict[str, Any]) -> list[str]:
        path = str(arguments.get("path", ""))
        if not path:
            return []
        try:
            target = resolve_workspace_path(path)
        except ValueError as exc:
            return [str(exc)]
        if not target.exists():
            return [f"path '{path}' does not exist"]
        if not target.is_file():
            return [f"path '{path}' is not a file"]
        return []

    def preflight_search_workspace(arguments: dict[str, Any]) -> list[str]:
        path = str(arguments.get("path", "."))
        try:
            root = resolve_workspace_path(path)
        except ValueError as exc:
            return [str(exc)]
        if not root.exists():
            return [f"path '{path}' does not exist"]
        return []

    def list_tools() -> dict[str, Any]:
        return {
            "tools": [
                {"name": spec.name, "description": spec.description, "source": spec.source}
                | {
                    "scope": spec.scope,
                    "mutates_state": spec.mutates_state,
                    "high_risk": spec.high_risk,
                    "external_side_effect": spec.external_side_effect,
                    "reads_sensitive_data": spec.reads_sensitive_data,
                    "scope_root": spec.scope_root,
                    "safety_level": spec.safety_level,
                }
                for spec in registry.list_specs()
            ]
        }

    def remember(note: str) -> dict[str, Any]:
        return sandbox.append_context_note(note)

    def rollback_remember(result: Any, arguments: dict[str, Any]) -> None:
        note = ""
        if isinstance(result, dict):
            note = str(result.get("stored", ""))
        if not note:
            note = str(arguments.get("note", ""))
        if note:
            sandbox.remove_context_note(note)

    def recall_notes(query: str = "", limit: int = 5) -> dict[str, Any]:
        return sandbox.recall_notes(query=query, limit=limit)

    def get_fact(key: str) -> dict[str, Any]:
        return sandbox.get_fact(key)

    def set_fact(key: str, value: Any) -> dict[str, Any]:
        return sandbox.set_fact(key, value)

    def rollback_set_fact(result: Any, arguments: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            return
        sandbox.rollback_set_fact(
            key=str(result.get("key", arguments.get("key", ""))),
            previous=result.get("previous"),
            existed=bool(result.get("existed", False)),
        )

    def append_task(description: str, status: str = "open") -> dict[str, Any]:
        return sandbox.append_task(description, status=status)

    def rollback_append_task(result: Any, arguments: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            return
        task = result.get("task")
        if isinstance(task, dict) and task.get("id"):
            sandbox.remove_task(str(task["id"]))

    def update_task(task_id: str, description: str | None = None, status: str | None = None) -> dict[str, Any]:
        return sandbox.update_task(task_id, description=description, status=status)

    def rollback_update_task(result: Any, arguments: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            return
        previous = result.get("previous")
        task_id = arguments.get("task_id")
        if isinstance(previous, dict) and task_id:
            sandbox.rollback_update_task(str(task_id), previous)

    def store_artifact(name: str, path: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return sandbox.store_artifact(name=name, path=path, metadata=metadata or {})

    def rollback_store_artifact(result: Any, arguments: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            return
        artifact = result.get("artifact")
        if isinstance(artifact, dict) and artifact.get("id"):
            sandbox.remove_artifact(str(artifact["id"]))

    def read_sandbox_file(path: str) -> dict[str, Any]:
        return sandbox.read_file(path)

    def write_sandbox_file(path: str, content: str) -> dict[str, Any]:
        return sandbox.write_file(path, content)

    def rollback_write_sandbox_file(result: Any, arguments: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            return
        sandbox.rollback_write_file(
            path=str(result.get("path", arguments.get("path", ""))),
            previous_content=result.get("previous_content"),
            existed=bool(result.get("existed", False)),
        )

    def delete_sandbox_file(path: str) -> dict[str, Any]:
        return sandbox.delete_file(path)

    def rollback_delete_sandbox_file(result: Any, arguments: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            return
        previous_content = result.get("previous_content")
        path = str(result.get("path", arguments.get("path", "")))
        if previous_content is not None and path:
            sandbox.rollback_delete_file(path, previous_content)

    def list_sandbox_files(path: str = "") -> dict[str, Any]:
        return sandbox.list_files(path)

    def compress_sandbox_memory(max_notes: int = 20) -> dict[str, Any]:
        return sandbox.compress_memory(max_notes=max_notes)

    def prune_sandbox_memory(prune_done_tasks: bool = True) -> dict[str, Any]:
        return sandbox.prune_memory(prune_done_tasks=prune_done_tasks)

    def snapshot_sandbox_state(label: str = "") -> dict[str, Any]:
        return sandbox.snapshot_state(label=label)

    def restore_sandbox_snapshot(snapshot_id: str) -> dict[str, Any]:
        return sandbox.restore_snapshot(snapshot_id)

    def list_sandbox_snapshots() -> dict[str, Any]:
        return sandbox.list_snapshots()

    def sleep_runtime(seconds: int) -> dict[str, Any]:
        import time as _time
        _time.sleep(seconds)
        return {"slept_seconds": seconds}

    def utc_time() -> dict[str, Any]:
        return {"utc": datetime.now(UTC).isoformat()}

    def list_workspace_files(path: str = ".", limit: int = 50) -> dict[str, Any]:
        root = resolve_workspace_path(path)
        if not root.is_dir():
            raise ValueError(f"path '{path}' is not a directory")
        entries = sorted(root.iterdir(), key=lambda item: item.name.lower())[:limit]
        return {
            "path": str(root.relative_to(workspace_root)),
            "entries": [
                {
                    "name": entry.name,
                    "kind": "dir" if entry.is_dir() else "file",
                }
                for entry in entries
            ],
        }

    def read_text_file(path: str, max_chars: int = 4000) -> dict[str, Any]:
        target = resolve_workspace_path(path)
        if not target.is_file():
            raise ValueError(f"path '{path}' is not a file")
        text = target.read_text(encoding="utf-8")
        return {
            "path": str(target.relative_to(workspace_root)),
            "content": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    def search_workspace(query: str, path: str = ".", limit: int = 20) -> dict[str, Any]:
        root = resolve_workspace_path(path)
        if not root.exists():
            raise ValueError(f"path '{path}' does not exist")
        matches: list[dict[str, Any]] = []
        for file_path in sorted(root.rglob("*")):
            if len(matches) >= limit:
                break
            if not file_path.is_file():
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query.lower() in line.lower():
                    matches.append(
                        {
                            "path": str(file_path.relative_to(workspace_root)),
                            "line_number": line_number,
                            "line": line.strip(),
                        }
                    )
                    break
        return {"query": query, "matches": matches}

    def write_temp_note(name: str, content: str) -> dict[str, Any]:
        safe_name = safe_temp_note_name(name)
        temp_notes_root.mkdir(parents=True, exist_ok=True)
        target = temp_notes_root / f"{safe_name}.txt"
        target.write_text(content, encoding="utf-8")
        return {
            "path": str(target.relative_to(workspace_root)),
            "bytes_written": len(content.encode("utf-8")),
        }

    def rollback_write_temp_note(result: Any, arguments: dict[str, Any]) -> None:
        relative_path = ""
        if isinstance(result, dict):
            relative_path = str(result.get("path", ""))
        if relative_path:
            target = workspace_root / relative_path
        else:
            target = temp_notes_root / f"{safe_temp_note_name(str(arguments.get('name', 'note')))}.txt"
        if target.exists():
            target.unlink()

    def runtime_status() -> dict[str, Any]:
        return status_provider()

    registry.register(
        name="list_tools",
        description="List all registered local and MCP-backed tools.",
        schema={"type": "object", "properties": {}},
        handler=list_tools,
        scope="runtime_memory",
        scope_root=str(workspace_root),
    )
    registry.register(
        name="remember",
        description="Persist a quick note into runtime memory.",
        schema={
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
        handler=remember,
        scope="runtime_memory",
        mutates_state=True,
        scope_root=str(workspace_root),
        rollback=rollback_remember,
    )
    registry.register(
        name="recall_notes",
        description="Recall matching notes from unstructured runtime memory.",
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
        },
        handler=recall_notes,
        scope="runtime_memory",
        scope_root=str(workspace_root),
    )
    registry.register(
        name="get_fact",
        description="Read a structured fact from sandbox memory by dotted key (e.g. 'project.name'). Returns null if not found.",
        schema={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
            },
            "required": ["key"],
        },
        handler=get_fact,
        scope="runtime_memory",
        scope_root=str(sandbox.root),
    )
    registry.register(
        name="set_fact",
        description="Set a structured fact inside sandbox memory.",
        schema={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {},
            },
            "required": ["key", "value"],
        },
        handler=set_fact,
        scope="runtime_memory",
        mutates_state=True,
        scope_root=str(sandbox.root),
        rollback=rollback_set_fact,
    )
    registry.register(
        name="append_task",
        description="Append a task to sandbox memory.",
        schema={
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["description"],
        },
        handler=append_task,
        scope="runtime_memory",
        mutates_state=True,
        scope_root=str(sandbox.root),
        rollback=rollback_append_task,
    )
    registry.register(
        name="update_task",
        description="Update an existing sandbox memory task.",
        schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "description": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["task_id"],
        },
        handler=update_task,
        scope="runtime_memory",
        mutates_state=True,
        scope_root=str(sandbox.root),
        rollback=rollback_update_task,
    )
    registry.register(
        name="store_artifact",
        description="Store artifact metadata inside sandbox memory.",
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["name", "path"],
        },
        handler=store_artifact,
        scope="runtime_memory",
        mutates_state=True,
        scope_root=str(sandbox.root),
        rollback=rollback_store_artifact,
    )
    registry.register(
        name="utc_time",
        description="Return the current UTC time.",
        schema={"type": "object", "properties": {}},
        handler=utc_time,
        scope="runtime_memory",
        scope_root=str(workspace_root),
    )
    registry.register(
        name="list_workspace_files",
        description="List files and directories inside the workspace root or a subdirectory.",
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
        handler=list_workspace_files,
        scope="local_workspace",
        scope_root=str(workspace_root),
        preflight=preflight_list_workspace_files,
    )
    registry.register(
        name="read_text_file",
        description="Read a UTF-8 text file from the workspace root.",
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 20000},
            },
            "required": ["path"],
        },
        handler=read_text_file,
        scope="local_workspace",
        reads_sensitive_data=True,
        scope_root=str(workspace_root),
        preflight=preflight_read_text_file,
    )
    registry.register(
        name="search_workspace",
        description="Search workspace text files for a query string.",
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        },
        handler=search_workspace,
        scope="local_workspace",
        reads_sensitive_data=True,
        scope_root=str(workspace_root),
        preflight=preflight_search_workspace,
    )
    registry.register(
        name="write_temp_note",
        description="Write a temporary text note under .runtime/temp_notes inside the workspace.",
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["name", "content"],
        },
        handler=write_temp_note,
        scope="local_workspace",
        mutates_state=True,
        scope_root=str(temp_notes_root),
        rollback=rollback_write_temp_note,
    )
    registry.register(
        name="runtime_status",
        description="Report current runtime state, governance mode, and workspace info.",
        schema={"type": "object", "properties": {}},
        handler=runtime_status,
        scope="runtime_memory",
        scope_root=str(workspace_root),
    )
    registry.register(
        name="read_sandbox_file",
        description="Read a UTF-8 text file from the sandbox file resource root.",
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=read_sandbox_file,
        scope="runtime_memory",
        reads_sensitive_data=True,
        scope_root=str(sandbox.root),
    )
    registry.register(
        name="write_sandbox_file",
        description="Write a UTF-8 text file into the sandbox file resource root.",
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        handler=write_sandbox_file,
        scope="runtime_memory",
        mutates_state=True,
        high_risk=True,
        scope_root=str(sandbox.root),
        rollback=rollback_write_sandbox_file,
    )
    registry.register(
        name="delete_sandbox_file",
        description="Delete a file from the sandbox file resource root.",
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=delete_sandbox_file,
        scope="runtime_memory",
        mutates_state=True,
        high_risk=True,
        scope_root=str(sandbox.root),
        rollback=rollback_delete_sandbox_file,
    )
    registry.register(
        name="list_sandbox_files",
        description="List files and directories in the sandbox file resource root.",
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
        handler=list_sandbox_files,
        scope="runtime_memory",
        scope_root=str(sandbox.root),
    )
    registry.register(
        name="compress_sandbox_memory",
        description="Truncate sandbox context notes to the most recent max_notes entries.",
        schema={
            "type": "object",
            "properties": {
                "max_notes": {"type": "integer", "minimum": 0, "maximum": 1000},
            },
        },
        handler=compress_sandbox_memory,
        scope="runtime_memory",
        mutates_state=True,
        scope_root=str(sandbox.root),
    )
    registry.register(
        name="prune_sandbox_memory",
        description="Remove completed and closed tasks from sandbox memory.",
        schema={
            "type": "object",
            "properties": {
                "prune_done_tasks": {"type": "boolean"},
            },
        },
        handler=prune_sandbox_memory,
        scope="runtime_memory",
        mutates_state=True,
        scope_root=str(sandbox.root),
    )
    registry.register(
        name="snapshot_sandbox_state",
        description="Create a versioned snapshot of sandbox memory and files.",
        schema={
            "type": "object",
            "properties": {
                "label": {"type": "string"},
            },
        },
        handler=snapshot_sandbox_state,
        scope="runtime_memory",
        mutates_state=True,
        scope_root=str(sandbox.root),
    )
    def propose_identity_update(trait_key: str, proposed_value: str, justification: str) -> dict[str, Any]:
        proposal = {
            "trait_key": trait_key,
            "proposed_value": proposed_value,
            "justification": justification,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        current = sandbox.get_fact("identity.proposals")
        proposals: list[dict[str, Any]]
        if bool(current.get("found")) and isinstance(current.get("value"), list):
            proposals = list(current["value"])
        else:
            proposals = []
        proposals.append(proposal)
        persisted = sandbox.set_fact("identity.proposals", proposals)
        return {
            "status": "proposal_ledgered",
            "trait": trait_key,
            "value": proposed_value,
            "reason": justification,
            "notice": "This change is deferred to the consolidation (Simulation) phase.",
            "proposal": proposal,
            "proposal_count": len(proposals),
            "receipt": persisted.get("receipt"),
        }

    registry.register(
        name="propose_identity_update",
        description="Propose an update to a core identity trait. Changes are deferred to the Simulation phase.",
        schema={
            "type": "object",
            "properties": {
                "trait_key": {"type": "string"},
                "proposed_value": {"type": "string"},
                "justification": {"type": "string"},
            },
            "required": ["trait_key", "proposed_value", "justification"],
        },
        handler=propose_identity_update,
        scope="runtime_memory",
        mutates_state=True, # It "mutates" the proposal ledger
        scope_root=str(workspace_root),
    )

    def propose_doctrine(
        content: str, 
        claims: list[str], 
        parent_id: str | None = None,
        edges: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        if ledger is None:
            return {"status": "error", "message": "Canon Ledger not initialized in this registry."}
        
        from ..canon_ledger import Revision
        import uuid
        
        rev_id = f"rev_{uuid.uuid4().hex[:8]}"
        revision = Revision(
            revision_id=rev_id,
            content=content,
            claims=claims,
            origin_id=parent_id,
            edges=edges or [],
            alignment_source="USER_NEGOTIATION",
            continuity_flags=["PENDING"]
        )
        ledger.append(revision)
        return {
            "status": "doctrine_proposed",
            "revision_id": rev_id,
            "content_summary": content[:100],
            "claim_count": len(claims)
        }

    registry.register(
        name="propose_doctrine",
        description="Submit a candidate revision to the Canon Ledger. Use this to metabolize player requests into institutional truth.",
        schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The natural language content of the doctrine."},
                "claims": {"type": "array", "items": {"type": "string"}, "description": "Structured machine-readable assertions extracted from the content."},
                "parent_id": {"type": "string", "description": "Optional ID of the revision this doctrine supersedes or cites."},
                "edges": {
                    "type": "array", 
                    "items": {
                        "type": "object",
                        "properties": {
                            "target_id": {"type": "string"},
                            "type": {"type": "string", "enum": ["contradiction", "supersession", "citation", "reconciliation"]}
                        },
                        "required": ["target_id", "type"]
                    }
                }
            },
            "required": ["content", "claims"]
        },
        handler=propose_doctrine,
        mutates_state=True
    )
    
    registry.register(
        name="restore_sandbox_snapshot",
        description="Restore sandbox memory and files from a previously created snapshot.",
        schema={
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
            },
            "required": ["snapshot_id"],
        },
        handler=restore_sandbox_snapshot,
        scope="runtime_memory",
        mutates_state=True,
        high_risk=True,
        scope_root=str(sandbox.root),
        safety_level="irreversible",
    )
    registry.register(
        name="list_sandbox_snapshots",
        description="List available snapshots in the sandbox.",
        schema={"type": "object", "properties": {}},
        handler=list_sandbox_snapshots,
        scope="runtime_memory",
        scope_root=str(sandbox.root),
    )
    registry.register(
        name="sleep",
        description="Suspend runtime execution for a specified number of seconds.",
        schema={
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            "required": ["seconds"],
        },
        handler=sleep_runtime,
        scope="runtime_memory",
        scope_root=str(workspace_root),
    )

    # 4. ValidationManager/Repair Logic
    repair_tool = RepairPatchTool(workspace_root=workspace_root, repair_lab=repair_lab)
    registry.register(
        name="repair_patch",
        description="Propose a governed code repair patch. Forces narrow scope and authority checks.",
        schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "target_file": {"type": "string"},
                "unified_diff": {"type": "string"},
                "rationale": {"type": "string"},
                "regression_test": {"type": "string"},
                "authority_impact": {"type": "string", "enum": ["none", "uncertain", "yes"]},
            },
            "required": ["artifact_id", "target_file", "unified_diff", "rationale", "regression_test"],
        },
        handler=repair_tool.execute,
        scope="local_workspace",
        mutates_state=True,
        high_risk=True,
        safety_level="reversible",
        scope_root=str(workspace_root),
    )

    return registry


def openai_tool_spec(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.schema,
        },
    }


def pretty_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2, ensure_ascii=True)


def example_tool_snippet(name: str, description: str, params: list[tuple[str, str]]) -> str:
    signature = ", ".join(f"{key}: {kind}" for key, kind in params)
    args_schema = ",\n                ".join(
        f'"{key}": {{"type": "{kind}"}}' for key, kind in params
    )
    required = ", ".join(f'"{key}"' for key, _ in params)
    return inspect.cleandoc(
        f"""
        registry.register(
            name="{name}",
            description="{description}",
            schema={{
                "type": "object",
                "properties": {{
                    {args_schema}
                }},
                "required": [{required}],
            }},
            handler=lambda {signature}: {{"ok": True}},
        )
        """
    )

def build_gloss_registry(
    memory: MemoryStore,
    workspace_root: Path,
    status_provider: Callable[[], dict[str, Any]],
    ledger: CanonLedger
) -> ToolRegistry:
    registry = ToolRegistry(memory=memory)
    
    def utc_time() -> dict[str, Any]:
        return {"utc": datetime.now(UTC).isoformat()}
        
    def runtime_status() -> dict[str, Any]:
        return status_provider()

    def propose_doctrine(
        content: str, 
        claims: list[str], 
        parent_id: str | None = None,
        edges: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        from ..canon_ledger import Revision
        import uuid
        
        rev_id = f"rev_{uuid.uuid4().hex[:8]}"
        revision = Revision(
            revision_id=rev_id,
            content=content,
            claims=claims,
            origin_id=parent_id,
            edges=edges or [],
            alignment_source="USER_NEGOTIATION",
            continuity_flags=["PENDING"]
        )
        ledger.append(revision)
        return {
            "status": "doctrine_proposed",
            "revision_id": rev_id,
            "content_summary": content[:100],
            "claim_count": len(claims)
        }

    registry.register(
        name="propose_doctrine",
        description="Submit a candidate revision to the Canon Ledger. Use this to metabolize player requests into institutional truth.",
        schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The natural language doctrine."},
                "claims": {"type": "array", "items": {"type": "string"}, "description": "Structured machine-readable assertions extracted from the content."},
                "parent_id": {"type": "string", "description": "Optional ID of the revision this doctrine supersedes or cites."},
                "edges": {
                    "type": "array", 
                    "items": {
                        "type": "object",
                        "properties": {
                            "target_id": {"type": "string"},
                            "type": {"type": "string", "enum": ["contradiction", "supersession", "citation", "reconciliation"]}
                        },
                        "required": ["target_id", "type"]
                    }
                }
            },
            "required": ["content", "claims"]
        },
        handler=propose_doctrine,
        mutates_state=True
    )

    registry.register(
        name="utc_time",
        description="Return the current UTC time.",
        schema={"type": "object", "properties": {}},
        handler=utc_time,
        scope="runtime_memory",
    )

    registry.register(
        name="runtime_status",
        description="Report current runtime state and institutional vitals.",
        schema={"type": "object", "properties": {}},
        handler=runtime_status,
        scope="runtime_memory",
    )

    return registry
