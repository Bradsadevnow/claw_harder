from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any


_PROFILE_RE = re.compile(
    r"## Machine-Readable Control Profile.*?```json\s+(?P<body>\{.*?\})\s+```",
    re.DOTALL,
)


@dataclass(frozen=True)
class RuntimeControls:
    soft_stop: bool
    hard_nuke: bool
    latched_offline_after_nuke: bool


@dataclass(frozen=True)
class ScratchSurfaces:
    plan: str
    audit: str
    data: str
    distilled: str

    def all(self) -> tuple[str, ...]:
        return (self.plan, self.audit, self.data, self.distilled)


@dataclass(frozen=True)
class RoadmapConfig:
    config_version: str
    model_provider: str
    model_name: str
    context_window_cap: int
    tokenizer_name: str
    harmony_encoding_name: str
    prompt_template: str
    completion_token_reserve: int
    workspace_root: str
    manual_workspace_selection: bool
    allowed_action_types: tuple[str, ...]
    immutable_targets: tuple[str, ...]
    read_targets: tuple[str, ...]
    write_targets: tuple[str, ...]
    delete_targets: tuple[str, ...]
    primary_read_target: str
    output_target: str
    output_write_mode: str
    parse_failure_mode: str
    tool_access_phase: str
    write_requires_approval: bool
    scratch_surfaces: ScratchSurfaces
    scratch_owner_header: str
    scratch_root: str
    quarantine_root: str
    scratch_token_threshold: int
    grounding_required: bool
    grounding_mode: str
    max_steps_per_run: int
    trace_events: tuple[str, ...]
    runtime_controls: RuntimeControls

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["allowed_action_types"] = list(self.allowed_action_types)
        payload["immutable_targets"] = list(self.immutable_targets)
        payload["read_targets"] = list(self.read_targets)
        payload["write_targets"] = list(self.write_targets)
        payload["delete_targets"] = list(self.delete_targets)
        payload["trace_events"] = list(self.trace_events)
        return payload


class RoadmapConfigError(ValueError):
    pass


def extract_roadmap_config_payload(markdown: str) -> dict[str, Any]:
    match = _PROFILE_RE.search(markdown)
    if match is None:
        raise RoadmapConfigError(
            "runtime_capabilities.md is missing a valid 'Machine-Readable Control Profile' JSON block."
        )
    try:
        payload = json.loads(match.group("body"))
    except json.JSONDecodeError as exc:
        raise RoadmapConfigError(
            f"Machine-readable control profile JSON is invalid: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RoadmapConfigError("Machine-readable control profile must be a JSON object.")
    return payload


def load_roadmap_config(path: str | Path) -> RoadmapConfig:
    doc_path = Path(path)
    payload = extract_roadmap_config_payload(doc_path.read_text(encoding="utf-8"))
    controls = payload.get("runtime_controls", {})
    if not isinstance(controls, dict):
        raise RoadmapConfigError("runtime_controls must be an object.")
    scratch_surfaces = payload.get("scratch_surfaces", {})
    if not isinstance(scratch_surfaces, dict):
        raise RoadmapConfigError("scratch_surfaces must be an object.")

    config = RoadmapConfig(
        config_version=str(payload.get("config_version", "")),
        model_provider=str(payload.get("model_provider", "")),
        model_name=str(payload.get("model_name", "")),
        context_window_cap=int(payload.get("context_window_cap", 0)),
        tokenizer_name=str(payload.get("tokenizer_name", "")),
        harmony_encoding_name=str(payload.get("harmony_encoding_name", "")),
        prompt_template=str(payload.get("prompt_template", "")),
        completion_token_reserve=int(payload.get("completion_token_reserve", 0)),
        workspace_root=str(payload.get("workspace_root", "")),
        manual_workspace_selection=bool(payload.get("manual_workspace_selection", False)),
        allowed_action_types=tuple(str(item) for item in payload.get("allowed_action_types", [])),
        immutable_targets=tuple(str(item) for item in payload.get("immutable_targets", [])),
        read_targets=tuple(str(item) for item in payload.get("read_targets", [])),
        write_targets=tuple(str(item) for item in payload.get("write_targets", [])),
        delete_targets=tuple(str(item) for item in payload.get("delete_targets", [])),
        primary_read_target=str(payload.get("primary_read_target", "")),
        output_target=str(payload.get("output_target", "")),
        output_write_mode=str(payload.get("output_write_mode", "")),
        parse_failure_mode=str(payload.get("parse_failure_mode", "")),
        tool_access_phase=str(payload.get("tool_access_phase", "")),
        write_requires_approval=bool(payload.get("write_requires_approval", False)),
        scratch_surfaces=ScratchSurfaces(
            plan=str(scratch_surfaces.get("plan", "")),
            audit=str(scratch_surfaces.get("audit", "")),
            data=str(scratch_surfaces.get("data", "")),
            distilled=str(scratch_surfaces.get("distilled", "")),
        ),
        scratch_owner_header=str(payload.get("scratch_owner_header", "")),
        scratch_root=str(payload.get("scratch_root", "")),
        quarantine_root=str(payload.get("quarantine_root", "")),
        scratch_token_threshold=int(payload.get("scratch_token_threshold", 0)),
        grounding_required=bool(payload.get("grounding_required", False)),
        grounding_mode=str(payload.get("grounding_mode", "")),
        max_steps_per_run=int(payload.get("max_steps_per_run", 0)),
        trace_events=tuple(str(item) for item in payload.get("trace_events", [])),
        runtime_controls=RuntimeControls(
            soft_stop=bool(controls.get("soft_stop", False)),
            hard_nuke=bool(controls.get("hard_nuke", False)),
            latched_offline_after_nuke=bool(controls.get("latched_offline_after_nuke", False)),
        ),
    )
    _validate_roadmap_config(config)
    return config


def _validate_roadmap_config(config: RoadmapConfig) -> None:
    required = {
        "config_version": config.config_version,
        "model_provider": config.model_provider,
        "model_name": config.model_name,
        "tokenizer_name": config.tokenizer_name,
        "harmony_encoding_name": config.harmony_encoding_name,
        "prompt_template": config.prompt_template,
        "workspace_root": config.workspace_root,
        "primary_read_target": config.primary_read_target,
        "output_target": config.output_target,
        "output_write_mode": config.output_write_mode,
        "parse_failure_mode": config.parse_failure_mode,
        "tool_access_phase": config.tool_access_phase,
        "scratch_owner_header": config.scratch_owner_header,
        "scratch_root": config.scratch_root,
        "quarantine_root": config.quarantine_root,
        "grounding_mode": config.grounding_mode,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RoadmapConfigError(f"Roadmap config is missing required values: {', '.join(missing)}")

    if config.model_provider != "lmstudio":
        raise RoadmapConfigError(
            f"Roadmap config must lock model_provider to 'lmstudio', got {config.model_provider!r}."
        )
    if config.model_name != "openai/gpt-oss-20b":
        raise RoadmapConfigError(
            f"Roadmap config must lock model_name to 'openai/gpt-oss-20b', got {config.model_name!r}."
        )
    if config.context_window_cap <= 0:
        raise RoadmapConfigError("Roadmap config must provide a positive context_window_cap.")
    if config.tokenizer_name != "o200k_harmony":
        raise RoadmapConfigError(
            f"Roadmap config must lock tokenizer_name to 'o200k_harmony', got {config.tokenizer_name!r}."
        )
    if config.harmony_encoding_name != "HarmonyGptOss":
        raise RoadmapConfigError(
            "Roadmap config must lock harmony_encoding_name to 'HarmonyGptOss', "
            f"got {config.harmony_encoding_name!r}."
        )
    if config.prompt_template != "openai_harmony":
        raise RoadmapConfigError(
            f"Roadmap config must lock prompt_template to 'openai_harmony', got {config.prompt_template!r}."
        )
    if config.completion_token_reserve <= 0:
        raise RoadmapConfigError("Roadmap config must provide a positive completion_token_reserve.")
    if config.completion_token_reserve >= config.context_window_cap:
        raise RoadmapConfigError(
            "Roadmap config must keep completion_token_reserve below context_window_cap."
        )
    if set(config.allowed_action_types) != {"READ", "WRITE", "DELETE"}:
        raise RoadmapConfigError(
            "Roadmap config must allow only READ, WRITE, and DELETE action types during the current phase."
        )
    if config.output_target != "output.md":
        raise RoadmapConfigError("Roadmap config must lock output_target to 'output.md'.")
    if config.output_write_mode != "append_only":
        raise RoadmapConfigError("Roadmap config must lock output_write_mode to 'append_only'.")
    if config.parse_failure_mode != "halt":
        raise RoadmapConfigError("Roadmap config must lock parse_failure_mode to 'halt'.")
    if config.tool_access_phase != "file_read_write_delete_only":
        raise RoadmapConfigError(
            "Roadmap config must lock tool_access_phase to 'file_read_write_delete_only'."
        )
    if config.scratch_token_threshold <= 0:
        raise RoadmapConfigError("Roadmap config must provide a positive scratch_token_threshold.")
    if config.scratch_token_threshold >= config.context_window_cap:
        raise RoadmapConfigError(
            "Roadmap config must keep scratch_token_threshold below context_window_cap."
        )
    if config.max_steps_per_run <= 0:
        raise RoadmapConfigError("Roadmap config must provide a positive max_steps_per_run.")
    scratch_files = config.scratch_surfaces.all()
    if any(not name for name in scratch_files):
        raise RoadmapConfigError("All scratch surface filenames must be present.")
    if any(not name.startswith("scratch_") or not name.endswith(".md") for name in scratch_files):
        raise RoadmapConfigError("Scratch surface filenames must use the scratch_*.md convention.")
    if config.primary_read_target != "design.md":
        raise RoadmapConfigError("Roadmap config must lock primary_read_target to 'design.md'.")
    if set(config.immutable_targets) != {"design.md"}:
        raise RoadmapConfigError("Roadmap config must lock immutable_targets to ['design.md'].")
    if "design.md" not in config.read_targets:
        raise RoadmapConfigError("Roadmap config must allow READ access to 'design.md'.")
    if set(config.delete_targets) != set(scratch_files):
        raise RoadmapConfigError("Roadmap config must lock DELETE targets to the scratch surfaces only.")
    expected_write_targets = set(scratch_files) | {"output.md"}
    if set(config.write_targets) != expected_write_targets:
        raise RoadmapConfigError(
            "Roadmap config must lock WRITE targets to the scratch surfaces plus 'output.md'."
        )
    if set(config.read_targets) != (set(scratch_files) | {"design.md"}):
        raise RoadmapConfigError(
            "Roadmap config must lock READ targets to 'design.md' plus the scratch surfaces."
        )
