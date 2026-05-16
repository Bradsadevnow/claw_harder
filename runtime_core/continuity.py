from __future__ import annotations

from dataclasses import asdict, dataclass
from time import time
from typing import Any

from .agent_profile import AgentProfile
from .state import RuntimeState


_CLOSED_TASK_STATUSES = frozenset({"cancelled", "canceled", "closed", "completed", "done"})
_STATUS_KEYWORDS = frozenset(
    {"active", "archived", "blocked", "closed", "completed", "done", "open", "paused"}
)


@dataclass(frozen=True)
class ContinuityIdentity:
    name: str
    mode: str
    purpose: str
    values: tuple[str, ...]
    boundaries: tuple[str, ...]
    resume_enabled: bool
    resume_max_lines: int


@dataclass(frozen=True)
class ContinuityNow:
    active_focus: str | None
    current_blocker: str | None
    next_step: str | None
    last_updated_at: float | None

    @property
    def present(self) -> bool:
        return any(
            value is not None and value != ""
            for value in (self.active_focus, self.current_blocker, self.next_step)
        ) or self.last_updated_at is not None


@dataclass(frozen=True)
class ContinuityProject:
    slug: str
    project_name: str
    goal: str | None
    status: str | None
    constraints: tuple[str, ...]
    last_touched_at: float | None


@dataclass(frozen=True)
class ContinuityUser:
    stated_preferences: tuple[str, ...]
    working_style: tuple[str, ...]
    stable_boundaries: tuple[str, ...]

    @property
    def present(self) -> bool:
        return bool(self.stated_preferences or self.working_style or self.stable_boundaries)


@dataclass(frozen=True)
class ContinuityThread:
    task_id: str
    description: str
    status: str
    created_at: float | None
    updated_at: float | None


@dataclass(frozen=True)
class WorkingAssumption:
    subject: str
    assumption_text: str
    reason: str | None
    confidence: float | None
    expires_after: float | None
    last_checked_at: float | None


@dataclass(frozen=True)
class TranscriptExcerpt:
    role: str
    content: str
    ts: float


@dataclass(frozen=True)
class UncertaintyItem:
    kind: str
    subject: str
    detail: str


@dataclass(frozen=True)
class ContinuityLanes:
    now: ContinuityNow
    projects: tuple[ContinuityProject, ...]
    user: ContinuityUser
    open_threads: tuple[ContinuityThread, ...]
    working_assumptions: tuple[WorkingAssumption, ...]
    carry_forward_notes: tuple[str, ...]
    recent_transcript: tuple[TranscriptExcerpt, ...]


@dataclass(frozen=True)
class ContinuitySnapshot:
    identity: ContinuityIdentity
    lanes: ContinuityLanes
    uncertainty: tuple[UncertaintyItem, ...]
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_continuity_snapshot(
    profile: AgentProfile,
    state: RuntimeState,
    durable_memory: dict[str, Any],
    *,
    now: float | None = None,
    transcript_limit: int = 8,
    note_limit: int = 5,
    low_confidence_threshold: float = 0.5,
) -> ContinuitySnapshot:
    if not isinstance(durable_memory, dict):
        raise ValueError("durable memory must be an object")

    snapshot_time = float(time() if now is None else now)
    memory = _normalize_memory_root(durable_memory)
    facts = memory["facts"]
    continuity = facts.get("continuity", {})
    if not isinstance(continuity, dict):
        continuity = {}

    identity = ContinuityIdentity(
        name=profile.name,
        mode=profile.mode,
        purpose=profile.purpose,
        values=tuple(profile.values),
        boundaries=tuple(profile.boundaries),
        resume_enabled=profile.resume_behavior.enabled,
        resume_max_lines=profile.resume_behavior.max_lines,
    )
    now_lane = _build_now_lane(continuity.get("now"))
    projects = _build_projects(continuity.get("projects"))
    user_lane = _build_user_lane(continuity.get("user"))
    open_threads = _build_open_threads(memory["tasks"])
    active_assumptions, assumption_uncertainties = _build_assumptions(
        continuity.get("assumptions"),
        now=snapshot_time,
        low_confidence_threshold=low_confidence_threshold,
    )
    carry_forward_notes = _build_carry_forward_notes(memory["context"], note_limit=note_limit)
    recent_transcript = _build_recent_transcript(state, transcript_limit=transcript_limit)
    conflict_uncertainties = _detect_project_conflicts(projects, recent_transcript)
    gap_uncertainties = _build_known_gaps(
        now_lane=now_lane,
        projects=projects,
        user_lane=user_lane,
        open_threads=open_threads,
        carry_forward_notes=carry_forward_notes,
    )

    return ContinuitySnapshot(
        identity=identity,
        lanes=ContinuityLanes(
            now=now_lane,
            projects=projects,
            user=user_lane,
            open_threads=open_threads,
            working_assumptions=active_assumptions,
            carry_forward_notes=carry_forward_notes,
            recent_transcript=recent_transcript,
        ),
        uncertainty=tuple([*assumption_uncertainties, *conflict_uncertainties, *gap_uncertainties]),
        timestamp=snapshot_time,
    )


def _normalize_memory_root(durable_memory: dict[str, Any]) -> dict[str, Any]:
    context = durable_memory.get("context", {})
    facts = durable_memory.get("facts", {})
    tasks = durable_memory.get("tasks", [])
    return {
        "context": context if isinstance(context, dict) else {},
        "facts": facts if isinstance(facts, dict) else {},
        "tasks": tasks if isinstance(tasks, list) else [],
    }


def _build_now_lane(raw: Any) -> ContinuityNow:
    if not isinstance(raw, dict):
        return ContinuityNow(None, None, None, None)
    return ContinuityNow(
        active_focus=_string_or_none(raw.get("active_focus")),
        current_blocker=_string_or_none(raw.get("current_blocker")),
        next_step=_string_or_none(raw.get("next_step")),
        last_updated_at=_float_or_none(raw.get("last_updated_at")),
    )


def _build_projects(raw: Any) -> tuple[ContinuityProject, ...]:
    if not isinstance(raw, dict):
        return ()
    projects: list[ContinuityProject] = []
    for slug in sorted(raw):
        details = raw.get(slug)
        if not isinstance(details, dict):
            continue
        projects.append(
            ContinuityProject(
                slug=str(slug),
                project_name=_string_or_none(details.get("project_name"))
                or _string_or_none(details.get("name"))
                or str(slug),
                goal=_string_or_none(details.get("goal")),
                status=_string_or_none(details.get("status")),
                constraints=_string_tuple(details.get("constraints")),
                last_touched_at=_float_or_none(details.get("last_touched_at")),
            )
        )
    return tuple(projects)


def _build_user_lane(raw: Any) -> ContinuityUser:
    if not isinstance(raw, dict):
        return ContinuityUser((), (), ())
    return ContinuityUser(
        stated_preferences=_string_tuple(raw.get("stated_preferences")),
        working_style=_string_tuple(raw.get("working_style")),
        stable_boundaries=_string_tuple(raw.get("stable_boundaries")),
    )


def _build_open_threads(raw_tasks: list[Any]) -> tuple[ContinuityThread, ...]:
    threads: list[ContinuityThread] = []
    for task in raw_tasks:
        if not isinstance(task, dict):
            continue
        status = _string_or_none(task.get("status")) or "open"
        if _normalize_status(status) in _CLOSED_TASK_STATUSES:
            continue
        description = _string_or_none(task.get("description"))
        task_id = _string_or_none(task.get("id"))
        if description is None or task_id is None:
            continue
        threads.append(
            ContinuityThread(
                task_id=task_id,
                description=description,
                status=status,
                created_at=_float_or_none(task.get("created_at")),
                updated_at=_float_or_none(task.get("updated_at")),
            )
        )
    return tuple(threads)


def _build_assumptions(
    raw: Any,
    *,
    now: float,
    low_confidence_threshold: float,
) -> tuple[tuple[WorkingAssumption, ...], tuple[UncertaintyItem, ...]]:
    uncertainties: list[UncertaintyItem] = []
    assumptions: list[WorkingAssumption] = []
    raw_items = _normalize_assumption_items(raw)
    for index, item in enumerate(raw_items):
        assumption_text = _string_or_none(item.get("assumption_text")) or _string_or_none(item.get("text"))
        if assumption_text is None:
            continue
        subject = _string_or_none(item.get("id")) or f"assumptions[{index}]"
        record = WorkingAssumption(
            subject=subject,
            assumption_text=assumption_text,
            reason=_string_or_none(item.get("reason")),
            confidence=_float_or_none(item.get("confidence")),
            expires_after=_float_or_none(item.get("expires_after"))
            if item.get("expires_after") is not None
            else _float_or_none(item.get("expires_at")),
            last_checked_at=_float_or_none(item.get("last_checked_at")),
        )
        if record.expires_after is not None and record.expires_after <= now:
            uncertainties.append(
                UncertaintyItem(
                    kind="stale_assumption",
                    subject=record.subject,
                    detail=f"Assumption expired at {record.expires_after}.",
                )
            )
            continue
        assumptions.append(record)
        if record.confidence is not None and record.confidence < low_confidence_threshold:
            uncertainties.append(
                UncertaintyItem(
                    kind="low_confidence_assumption",
                    subject=record.subject,
                    detail=f"Assumption confidence {record.confidence} is below {low_confidence_threshold}.",
                )
            )
    return tuple(assumptions), tuple(uncertainties)


def _normalize_assumption_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        items = raw.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        if "assumption_text" in raw or "text" in raw:
            return [raw]
    return []


def _build_carry_forward_notes(raw_context: dict[str, Any], *, note_limit: int) -> tuple[str, ...]:
    raw_notes = raw_context.get("notes", [])
    if not isinstance(raw_notes, list):
        return ()
    notes = [note for note in raw_notes if isinstance(note, str) and note.strip()]
    if note_limit <= 0:
        return ()
    return tuple(notes[-note_limit:])


def _build_recent_transcript(state: RuntimeState, *, transcript_limit: int) -> tuple[TranscriptExcerpt, ...]:
    if transcript_limit <= 0:
        return ()
    frames = state.memory.recent(transcript_limit)
    return tuple(
        TranscriptExcerpt(role=frame.role, content=frame.content, ts=frame.ts)
        for frame in frames
    )


def _detect_project_conflicts(
    projects: tuple[ContinuityProject, ...],
    transcript: tuple[TranscriptExcerpt, ...],
) -> tuple[UncertaintyItem, ...]:
    uncertainties: list[UncertaintyItem] = []
    for project in projects:
        durable_status = _normalize_status(project.status)
        if durable_status is None:
            continue
        project_name = project.project_name.lower()
        for frame in transcript:
            lowered = frame.content.lower()
            if project_name not in lowered:
                continue
            mentioned_statuses = {
                word.strip(".,!?;:()[]{}")
                for word in lowered.split()
                if word.strip(".,!?;:()[]{}") in _STATUS_KEYWORDS
            }
            conflicting = sorted(status for status in mentioned_statuses if status != durable_status)
            if conflicting:
                uncertainties.append(
                    UncertaintyItem(
                        kind="conflicting_signal",
                        subject=f"projects.{project.slug}.status",
                        detail=(
                            f"Durable status '{project.status}' conflicts with recent transcript "
                            f"mention(s): {', '.join(conflicting)}."
                        ),
                    )
                )
                break
    return tuple(uncertainties)


def _build_known_gaps(
    *,
    now_lane: ContinuityNow,
    projects: tuple[ContinuityProject, ...],
    user_lane: ContinuityUser,
    open_threads: tuple[ContinuityThread, ...],
    carry_forward_notes: tuple[str, ...],
) -> tuple[UncertaintyItem, ...]:
    uncertainties: list[UncertaintyItem] = []
    if not now_lane.present:
        uncertainties.append(
            UncertaintyItem(
                kind="known_gap",
                subject="continuity.now",
                detail="No active focus is stored in governed continuity memory.",
            )
        )
    if not projects:
        uncertainties.append(
            UncertaintyItem(
                kind="known_gap",
                subject="continuity.projects",
                detail="No durable project records are stored in governed continuity memory.",
            )
        )
    if not user_lane.present:
        uncertainties.append(
            UncertaintyItem(
                kind="known_gap",
                subject="continuity.user",
                detail="No durable user preferences are stored in governed continuity memory.",
            )
        )
    if not now_lane.present and not projects and not user_lane.present and not open_threads and not carry_forward_notes:
        uncertainties.append(
            UncertaintyItem(
                kind="known_gap",
                subject="continuity",
                detail="No governed continuity memory is currently available.",
            )
        )
    return tuple(uncertainties)


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _normalize_status(status: str | None) -> str | None:
    if status is None:
        return None
    lowered = status.strip().lower()
    return lowered or None
