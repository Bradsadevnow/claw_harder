from __future__ import annotations

from dataclasses import asdict, dataclass

from .continuity import ContinuityProject, ContinuitySnapshot, UncertaintyItem


_ACTIVE_PROJECT_STATUSES = frozenset({"active", "blocked", "open"})


@dataclass(frozen=True)
class ContinuityRender:
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def render_continuity(snapshot: ContinuitySnapshot) -> ContinuityRender:
    max_lines = max(1, int(snapshot.identity.resume_max_lines))
    candidates: list[str] = []

    active_line = _render_active_line(snapshot)
    if active_line is not None:
        candidates.append(active_line)

    open_line = _render_open_line(snapshot)
    if open_line is not None and open_line not in candidates:
        candidates.append(open_line)

    uncertainty_line = _render_uncertainty_line(snapshot)
    if uncertainty_line is not None and uncertainty_line not in candidates:
        candidates.append(uncertainty_line)

    if not candidates:
        candidates.append("I don't have enough continuity context yet to give a reliable recap.")

    return ContinuityRender(lines=tuple(candidates[:max_lines]))


def _render_active_line(snapshot: ContinuitySnapshot) -> str | None:
    focus = _clean_fragment(snapshot.lanes.now.active_focus)
    project = _select_primary_project(snapshot.lanes.projects)

    parts: list[str] = []
    if focus is not None:
        parts.append(focus)

    if project is not None:
        project_text = project.project_name
        if project.status:
            project_text = f"{project_text} ({project.status})"
        if focus is None or project.project_name.lower() not in focus.lower():
            parts.append(project_text)

    if not parts:
        return None
    return _sentence("Active", parts[:2])


def _render_open_line(snapshot: ContinuitySnapshot) -> str | None:
    items: list[str] = []
    blocker = _clean_fragment(snapshot.lanes.now.current_blocker)
    next_step = _clean_fragment(snapshot.lanes.now.next_step)

    if blocker is not None:
        items.append(f"blocker: {blocker}")

    for thread in snapshot.lanes.open_threads:
        detail = _clean_fragment(thread.description)
        if detail is None:
            continue
        if thread.status.strip().lower() != "open":
            detail = f"{detail} ({thread.status})"
        items.append(detail)

    if next_step is not None:
        items.append(f"next: {next_step}")

    deduped = _dedupe_preserve(items)
    if not deduped:
        return None
    return _sentence("Open", deduped[:2])


def _render_uncertainty_line(snapshot: ContinuitySnapshot) -> str | None:
    if not snapshot.uncertainty:
        return None

    if any(item.kind == "known_gap" and item.subject == "continuity" for item in snapshot.uncertainty):
        return "I don't have enough continuity context yet to give a reliable recap."

    non_gap_items = [item for item in snapshot.uncertainty if item.kind != "known_gap"]
    source_items = non_gap_items if non_gap_items else list(snapshot.uncertainty)

    details: list[str] = []
    for item in source_items:
        summary = _summarize_uncertainty(item)
        if summary is None or summary in details:
            continue
        details.append(summary)
    if not details:
        return None
    return _sentence("Uncertain", details[:2])


def _select_primary_project(projects: tuple[ContinuityProject, ...]) -> ContinuityProject | None:
    if not projects:
        return None
    ranked = sorted(
        projects,
        key=lambda project: (
            _project_status_rank(project.status),
            project.last_touched_at if project.last_touched_at is not None else float("-inf"),
            project.slug,
        ),
        reverse=True,
    )
    return ranked[0]


def _project_status_rank(status: str | None) -> int:
    lowered = status.strip().lower() if isinstance(status, str) else ""
    if lowered in _ACTIVE_PROJECT_STATUSES:
        return 2
    if lowered:
        return 1
    return 0


def _summarize_uncertainty(item: UncertaintyItem) -> str | None:
    if item.kind == "conflicting_signal":
        return _clean_fragment(item.detail)
    if item.kind == "low_confidence_assumption":
        return _clean_fragment(f"{item.subject} is low confidence")
    if item.kind == "stale_assumption":
        return _clean_fragment(f"{item.subject} is stale")
    if item.kind == "known_gap":
        if item.subject == "continuity.now":
            return "active focus is not stored yet"
        if item.subject == "continuity.projects":
            return "project continuity is not stored yet"
        if item.subject == "continuity.user":
            return "durable user preferences are not stored yet"
        return _clean_fragment(item.detail)
    return _clean_fragment(item.detail)


def _sentence(label: str, parts: list[str]) -> str:
    return f"{label}: {'; '.join(_clean_fragment(part) for part in parts if _clean_fragment(part))}."


def _clean_fragment(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    while cleaned.endswith((".", "!", "?")):
        cleaned = cleaned[:-1].rstrip()
    return cleaned or None


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(item)
    return result
