from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable

ToolHandler = Callable[..., Any]
ToolPreflight = Callable[[dict[str, Any]], list[str]]
ToolRollback = Callable[[Any, dict[str, Any]], None]

@dataclass
class ToolBindingResult:
    tool_name: str
    ok: bool
    value: Any | None = None
    error: str | None = None
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    phase: str = "success"
    rolled_back: bool = False
    denial_feedback: dict[str, Any] | None = None

@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: ToolHandler
    source: str = "local"
    scope: str = "local_workspace"
    mutates_state: bool = False
    high_risk: bool = False
    external_side_effect: bool = False
    reads_sensitive_data: bool = False
    scope_root: str = "."
    preflight: ToolPreflight | None = None
    rollback: ToolRollback | None = None
    safety_level: str = "read_only"
