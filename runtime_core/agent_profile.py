from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class VoiceProfile:
    style: tuple[str, ...]
    avoid: tuple[str, ...]


@dataclass(frozen=True)
class ResponseContract:
    prefer_short_paragraphs: bool
    ask_only_when_needed: bool
    admit_uncertainty_plainly: bool
    default_posture: str


@dataclass(frozen=True)
class ResumeBehavior:
    enabled: bool
    max_lines: int
    default_action: str
    include: tuple[str, ...]
    avoid: tuple[str, ...]


@dataclass(frozen=True)
class CheckInPolicy:
    enabled_by_default: bool
    requires_explicit_opt_in: bool
    max_unsolicited_checkins_per_day: int


@dataclass(frozen=True)
class MemoryContract:
    never_fake_familiarity: bool
    admit_missing_memory: bool
    mark_assumptions_as_assumptions: bool
    promote_long_term_memory_only_when_governed: bool


@dataclass(frozen=True)
class AgentProfile:
    version: int
    name: str
    mode: str
    purpose: str
    core_directive: str
    voice: VoiceProfile
    values: tuple[str, ...]
    boundaries: tuple[str, ...]
    response_contract: ResponseContract
    resume_behavior: ResumeBehavior
    check_in_policy: CheckInPolicy
    memory_contract: MemoryContract

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentProfile":
        _require_mapping(payload, "agent profile")
        version = _require_positive_int(payload, "version", "agent profile")
        name = _require_non_empty_string(payload, "name", "agent profile")
        mode = _require_non_empty_string(payload, "mode", "agent profile")
        purpose = _require_non_empty_string(payload, "purpose", "agent profile")
        core_directive = _require_non_empty_string(payload, "core_directive", "agent profile")
        values = _require_string_list(payload, "values", "agent profile", min_items=1)
        boundaries = _require_string_list(payload, "boundaries", "agent profile", min_items=1)

        voice_payload = _require_mapping_field(payload, "voice", "agent profile")
        voice = VoiceProfile(
            style=_require_string_list(voice_payload, "style", "voice", min_items=1),
            avoid=_require_string_list(voice_payload, "avoid", "voice", min_items=1),
        )

        response_payload = _require_mapping_field(payload, "response_contract", "agent profile")
        response_contract = ResponseContract(
            prefer_short_paragraphs=_require_bool(response_payload, "prefer_short_paragraphs", "response_contract"),
            ask_only_when_needed=_require_bool(response_payload, "ask_only_when_needed", "response_contract"),
            admit_uncertainty_plainly=_require_bool(
                response_payload, "admit_uncertainty_plainly", "response_contract"
            ),
            default_posture=_require_non_empty_string(response_payload, "default_posture", "response_contract"),
        )

        resume_payload = _require_mapping_field(payload, "resume_behavior", "agent profile")
        resume_behavior = ResumeBehavior(
            enabled=_require_bool(resume_payload, "enabled", "resume_behavior"),
            max_lines=_require_positive_int(resume_payload, "max_lines", "resume_behavior"),
            default_action=_require_non_empty_string(resume_payload, "default_action", "resume_behavior"),
            include=_require_string_list(resume_payload, "include", "resume_behavior", min_items=1),
            avoid=_require_string_list(resume_payload, "avoid", "resume_behavior", min_items=1),
        )

        check_in_payload = _require_mapping_field(payload, "check_in_policy", "agent profile")
        check_in_policy = CheckInPolicy(
            enabled_by_default=_require_bool(check_in_payload, "enabled_by_default", "check_in_policy"),
            requires_explicit_opt_in=_require_bool(
                check_in_payload, "requires_explicit_opt_in", "check_in_policy"
            ),
            max_unsolicited_checkins_per_day=_require_non_negative_int(
                check_in_payload, "max_unsolicited_checkins_per_day", "check_in_policy"
            ),
        )

        memory_payload = _require_mapping_field(payload, "memory_contract", "agent profile")
        memory_contract = MemoryContract(
            never_fake_familiarity=_require_bool(memory_payload, "never_fake_familiarity", "memory_contract"),
            admit_missing_memory=_require_bool(memory_payload, "admit_missing_memory", "memory_contract"),
            mark_assumptions_as_assumptions=_require_bool(
                memory_payload, "mark_assumptions_as_assumptions", "memory_contract"
            ),
            promote_long_term_memory_only_when_governed=_require_bool(
                memory_payload,
                "promote_long_term_memory_only_when_governed",
                "memory_contract",
            ),
        )

        return cls(
            version=version,
            name=name,
            mode=mode,
            purpose=purpose,
            core_directive=core_directive,
            voice=voice,
            values=values,
            boundaries=boundaries,
            response_contract=response_contract,
            resume_behavior=resume_behavior,
            check_in_policy=check_in_policy,
            memory_contract=memory_contract,
        )


def load_agent_profile(path: str | Path) -> AgentProfile:
    profile_path = Path(path)
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("agent profile must be a JSON object")
    return AgentProfile.from_dict(payload)


def _require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _require_mapping_field(payload: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    if key not in payload:
        raise ValueError(f"{context} is missing required field '{key}'")
    value = payload[key]
    if not isinstance(value, dict):
        raise ValueError(f"{context}.{key} must be an object")
    return value


def _require_non_empty_string(payload: dict[str, Any], key: str, context: str) -> str:
    if key not in payload:
        raise ValueError(f"{context} is missing required field '{key}'")
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value


def _require_bool(payload: dict[str, Any], key: str, context: str) -> bool:
    if key not in payload:
        raise ValueError(f"{context} is missing required field '{key}'")
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{context}.{key} must be a boolean")
    return value


def _require_positive_int(payload: dict[str, Any], key: str, context: str) -> int:
    if key not in payload:
        raise ValueError(f"{context} is missing required field '{key}'")
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{context}.{key} must be a positive integer")
    return value


def _require_non_negative_int(payload: dict[str, Any], key: str, context: str) -> int:
    if key not in payload:
        raise ValueError(f"{context} is missing required field '{key}'")
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{context}.{key} must be a non-negative integer")
    return value


def _require_string_list(
    payload: dict[str, Any],
    key: str,
    context: str,
    *,
    min_items: int = 0,
) -> tuple[str, ...]:
    if key not in payload:
        raise ValueError(f"{context} is missing required field '{key}'")
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"{context}.{key} must be an array")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{context}.{key}[{index}] must be a non-empty string")
        items.append(item)
    if len(items) < min_items:
        noun = "item" if min_items == 1 else "items"
        raise ValueError(f"{context}.{key} must contain at least {min_items} {noun}")
    return tuple(items)
