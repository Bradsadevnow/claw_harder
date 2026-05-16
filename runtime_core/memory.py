from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, Callable, Literal
import uuid

if TYPE_CHECKING:
    from .salience import ProposalMutation


STM_MAX_EPOCHS_DEFAULT = 6


@dataclass
class MemoryFrame:
    role: str
    content: str
    ts: float = field(default_factory=time)


@dataclass
class RetentionPolicy:
    max_items: int
    eviction: str   # "fifo" | "none"
    scope: str      # "turn" | "session" | "durable" | "stable_config"


@dataclass
class StmEpoch:
    epoch_id: str
    epoch_seq: int
    opened_at: float
    messages: list[MemoryFrame] = field(default_factory=list)
    closed_at: float | None = None
    close_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "epoch_seq": self.epoch_seq,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
            "messages": [{"role": msg.role, "content": msg.content, "ts": msg.ts} for msg in self.messages],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StmEpoch":
        return cls(
            epoch_id=str(payload.get("epoch_id", "")),
            epoch_seq=int(payload.get("epoch_seq", 0)),
            opened_at=float(payload.get("opened_at", time())),
            closed_at=(float(payload["closed_at"]) if payload.get("closed_at") is not None else None),
            close_reason=(str(payload["close_reason"]) if payload.get("close_reason") is not None else None),
            messages=[
                MemoryFrame(
                    role=str(item.get("role", "unknown")),
                    content=str(item.get("content", "")),
                    ts=float(item.get("ts", time())),
                )
                for item in payload.get("messages", [])
                if isinstance(item, dict)
            ],
        )


@dataclass
class StmAppendResult:
    epoch_id: str
    epoch_seq: int
    epoch_closed: bool
    evicted_epoch_ids: list[str] = field(default_factory=list)


@dataclass
class StmStore:
    max_epochs: int = STM_MAX_EPOCHS_DEFAULT
    close_roles: tuple[str, ...] = ("assistant",)
    epochs: list[StmEpoch] = field(default_factory=list)
    open_epoch: StmEpoch | None = None
    next_epoch_seq: int = 1

    def _begin_epoch(self, at: float) -> StmEpoch:
        epoch_seq = self.next_epoch_seq
        self.next_epoch_seq += 1
        self.open_epoch = StmEpoch(
            epoch_id=f"EPOCH_{epoch_seq:06d}",
            epoch_seq=epoch_seq,
            opened_at=at,
        )
        return self.open_epoch

    def append_frame(self, frame: MemoryFrame) -> StmAppendResult:
        epoch = self.open_epoch or self._begin_epoch(frame.ts)
        epoch.messages.append(frame)
        evicted: list[str] = []
        epoch_closed = False

        if frame.role in self.close_roles:
            epoch_closed = True
            epoch.closed_at = frame.ts
            epoch.close_reason = "assistant_turn_complete"
            self.epochs.append(epoch)
            self.open_epoch = None
            evicted = self._evict_if_needed()

        return StmAppendResult(
            epoch_id=epoch.epoch_id,
            epoch_seq=epoch.epoch_seq,
            epoch_closed=epoch_closed,
            evicted_epoch_ids=evicted,
        )

    def force_close(self, reason: str = "session_boundary", at: float | None = None) -> tuple[StmEpoch | None, list[str]]:
        if self.open_epoch is None:
            return None, []
        if not self.open_epoch.messages:
            self.open_epoch = None
            return None, []

        close_at = at if at is not None else self.open_epoch.messages[-1].ts
        self.open_epoch.closed_at = close_at
        self.open_epoch.close_reason = reason
        closed = self.open_epoch
        self.epochs.append(closed)
        self.open_epoch = None
        evicted = self._evict_if_needed()
        return closed, evicted

    def _evict_if_needed(self) -> list[str]:
        evicted: list[str] = []
        while len(self.epochs) > self.max_epochs:
            dropped = self.epochs.pop(0)
            evicted.append(dropped.epoch_id)
        return evicted

    def recent_epochs(self, limit: int | None = None, include_open: bool = True) -> list[StmEpoch]:
        epochs = list(self.epochs)
        if include_open and self.open_epoch is not None:
            epochs.append(self.open_epoch)
        if limit is None or limit >= len(epochs):
            return epochs
        return epochs[-limit:]

    def active_prompt_frames(self, limit_epochs: int | None = None) -> list[MemoryFrame]:
        epochs = self.recent_epochs(limit=limit_epochs, include_open=True)
        frames: list[MemoryFrame] = []
        for epoch in epochs:
            frames.extend(epoch.messages)
        return frames

    def clear(self) -> None:
        self.epochs.clear()
        self.open_epoch = None
        self.next_epoch_seq = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_epochs": self.max_epochs,
            "close_roles": list(self.close_roles),
            "next_epoch_seq": self.next_epoch_seq,
            "epochs": [epoch.to_dict() for epoch in self.epochs],
            "open_epoch": self.open_epoch.to_dict() if self.open_epoch is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StmStore":
        close_roles_raw = payload.get("close_roles", ["assistant"])
        close_roles = tuple(str(role) for role in close_roles_raw if isinstance(role, str)) or ("assistant",)
        store = cls(
            max_epochs=max(1, int(payload.get("max_epochs", STM_MAX_EPOCHS_DEFAULT))),
            close_roles=close_roles,
        )
        store.next_epoch_seq = max(1, int(payload.get("next_epoch_seq", 1)))
        store.epochs = [
            StmEpoch.from_dict(item)
            for item in payload.get("epochs", [])
            if isinstance(item, dict)
        ]
        open_epoch_raw = payload.get("open_epoch")
        if isinstance(open_epoch_raw, dict):
            store.open_epoch = StmEpoch.from_dict(open_epoch_raw)
        store._evict_if_needed()
        return store


@dataclass
class SessionLedgerEntry:
    seq: int
    event_kind: str
    ts: float
    epoch_seq: int | None = None
    role: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_kind": self.event_kind,
            "ts": self.ts,
            "epoch_seq": self.epoch_seq,
            "role": self.role,
            "content": self.content,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionLedgerEntry":
        return cls(
            seq=int(payload.get("seq", 0)),
            event_kind=str(payload.get("event_kind", "unknown")),
            ts=float(payload.get("ts", time())),
            epoch_seq=(int(payload["epoch_seq"]) if payload.get("epoch_seq") is not None else None),
            role=(str(payload["role"]) if payload.get("role") is not None else None),
            content=(str(payload["content"]) if payload.get("content") is not None else None),
            metadata=dict(payload.get("metadata", {})) if isinstance(payload.get("metadata", {}), dict) else {},
        )


@dataclass
class SessionLedger:
    entries: list[SessionLedgerEntry] = field(default_factory=list)
    next_seq: int = 1

    def append_frame(self, frame: MemoryFrame, *, epoch_seq: int | None = None, metadata: dict[str, Any] | None = None) -> SessionLedgerEntry:
        return self.append_event(
            event_kind="message.appended",
            epoch_seq=epoch_seq,
            role=frame.role,
            content=frame.content,
            ts=frame.ts,
            metadata=metadata,
        )

    def append_event(
        self,
        *,
        event_kind: str,
        epoch_seq: int | None = None,
        role: str | None = None,
        content: str | None = None,
        ts: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionLedgerEntry:
        entry = SessionLedgerEntry(
            seq=self.next_seq,
            event_kind=event_kind,
            ts=float(ts if ts is not None else time()),
            epoch_seq=epoch_seq,
            role=role,
            content=content,
            metadata=dict(metadata or {}),
        )
        self.entries.append(entry)
        self.next_seq += 1
        return entry

    def all(self) -> list[SessionLedgerEntry]:
        return list(self.entries)

    def since(self, seq: int) -> list[SessionLedgerEntry]:
        return [entry for entry in self.entries if entry.seq >= seq]

    def clear(self) -> None:
        self.entries.clear()
        self.next_seq = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_seq": self.next_seq,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionLedger":
        entries = [
            SessionLedgerEntry.from_dict(item)
            for item in payload.get("entries", [])
            if isinstance(item, dict)
        ]
        next_seq = int(payload.get("next_seq", len(entries) + 1))
        if next_seq <= 0:
            next_seq = len(entries) + 1
        return cls(entries=entries, next_seq=next_seq)


@dataclass
class LtmEpisodeArtifact:
    artifact_id: str
    session_id: str
    created_at: float
    source_seq_start: int
    source_seq_end: int
    entry_count: int
    epoch_count: int
    summary: str
    open_loops: list[str] = field(default_factory=list)
    provenance_refs: list[int] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "source_seq_start": self.source_seq_start,
            "source_seq_end": self.source_seq_end,
            "entry_count": self.entry_count,
            "epoch_count": self.epoch_count,
            "summary": self.summary,
            "open_loops": list(self.open_loops),
            "provenance_refs": list(self.provenance_refs),
            "metadata": dict(self.metadata),
        }


class LtmArchive:
    """Append-only long-term memory artifacts + deterministic TOC."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.episodes_path = self.root_dir / "ltm.jsonl"
        self.toc_path = self.root_dir / "ltm_toc.json"

    def compress_session(self, *, session_id: str, ledger: SessionLedger, stm: StmStore) -> LtmEpisodeArtifact:
        entries = ledger.all()
        seq_start = entries[0].seq if entries else 0
        seq_end = entries[-1].seq if entries else 0
        open_loops = self._open_loops(entries)

        artifact = LtmEpisodeArtifact(
            artifact_id=f"LTM_{session_id}_{uuid.uuid4().hex[:10]}",
            session_id=session_id,
            created_at=time(),
            source_seq_start=seq_start,
            source_seq_end=seq_end,
            entry_count=len(entries),
            epoch_count=len(stm.epochs),
            summary=self._summary(entries, stm),
            open_loops=open_loops,
            provenance_refs=[entry.seq for entry in entries],
            metadata={"compression_policy": "deterministic-v1"},
        )

        with self.episodes_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(artifact.to_dict(), ensure_ascii=True) + "\n")

        toc = self._load_toc()
        toc_entries = list(toc.get("episodes", []))
        toc_entries.append(
            {
                "artifact_id": artifact.artifact_id,
                "session_id": artifact.session_id,
                "created_at": artifact.created_at,
                "source_seq_start": artifact.source_seq_start,
                "source_seq_end": artifact.source_seq_end,
                "entry_count": artifact.entry_count,
                "epoch_count": artifact.epoch_count,
                "summary": artifact.summary,
            }
        )
        toc["episodes"] = toc_entries
        toc["version"] = "ltm-toc-v1"
        self.toc_path.write_text(json.dumps(toc, ensure_ascii=True, indent=2), encoding="utf-8")
        return artifact

    def _load_toc(self) -> dict[str, Any]:
        if not self.toc_path.exists():
            return {"version": "ltm-toc-v1", "episodes": []}
        try:
            payload = json.loads(self.toc_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"version": "ltm-toc-v1", "episodes": []}
        if not isinstance(payload, dict):
            return {"version": "ltm-toc-v1", "episodes": []}
        payload.setdefault("episodes", [])
        return payload

    @staticmethod
    def _summary(entries: list[SessionLedgerEntry], stm: StmStore) -> str:
        user_msgs = sum(1 for entry in entries if entry.role == "user")
        assistant_msgs = sum(1 for entry in entries if entry.role == "assistant")
        return (
            f"Session compressed with {len(entries)} ledger entries, "
            f"{len(stm.epochs)} closed epochs, {user_msgs} user messages, "
            f"{assistant_msgs} assistant messages."
        )

    @staticmethod
    def _open_loops(entries: list[SessionLedgerEntry]) -> list[str]:
        last_user_content: str | None = None
        answered_after_last_user = False
        for entry in entries:
            if entry.role == "user" and entry.content:
                last_user_content = entry.content
                answered_after_last_user = False
            elif entry.role == "assistant" and last_user_content is not None:
                answered_after_last_user = True
        if last_user_content is not None and not answered_after_last_user:
            return [last_user_content[:280]]
        return []


# ---------------------------------------------------------------------------
# Semantic nomination + deterministic admissibility surfaces
# ---------------------------------------------------------------------------


ASSOCIATIVE_RECALL_POLICY = "Associative Recall Is Non-Authoritative"


@dataclass
class RecallCandidate:
    candidate_id: str
    source: Literal["stm", "mtm", "ltm", "semantic"]
    content: str
    reference: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecallDecision:
    decision: Literal["admit", "reject"]
    reason_code: str
    rationale: str
    candidate: RecallCandidate


class DeterministicAdmitter:
    """Deterministic gate for context inclusion candidates."""

    def __init__(
        self,
        reviewer: Callable[[RecallCandidate], tuple[bool, str, str]] | None = None,
    ) -> None:
        self.reviewer = reviewer

    def evaluate(self, candidate: RecallCandidate) -> RecallDecision:
        if self.reviewer is not None:
            allowed, reason_code, rationale = self.reviewer(candidate)
            return RecallDecision(
                decision="admit" if allowed else "reject",
                reason_code=reason_code,
                rationale=rationale,
                candidate=candidate,
            )

        if candidate.source == "semantic":
            return RecallDecision(
                decision="reject",
                reason_code="semantic_nomination_requires_governance_admission",
                rationale=(
                    "Semantic output is evidence of possible relevance, not evidence of truth. "
                    "Deterministic governance admission is required before context injection."
                ),
                candidate=candidate,
            )
        return RecallDecision(
            decision="admit",
            reason_code="deterministic_lane",
            rationale="Candidate originates from deterministic memory lane.",
            candidate=candidate,
        )


class SemanticNominationIndex:
    """Optional non-authoritative semantic nomination sidecar."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def nominate(self, query: str, *, episodes: list[dict[str, Any]], limit: int = 5) -> list[RecallCandidate]:
        if not self.enabled:
            return []
        tokens = {token.lower() for token in query.split() if token.strip()}
        if not tokens:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        for episode in episodes:
            summary = str(episode.get("summary", ""))
            haystack = summary.lower()
            overlap = sum(1 for token in tokens if token in haystack)
            if overlap <= 0:
                continue
            score = overlap / max(1, len(tokens))
            scored.append((score, episode))

        scored.sort(key=lambda item: item[0], reverse=True)
        candidates: list[RecallCandidate] = []
        for score, episode in scored[: max(1, limit)]:
            artifact_id = str(episode.get("artifact_id", "unknown"))
            candidates.append(
                RecallCandidate(
                    candidate_id=f"semantic:{artifact_id}",
                    source="semantic",
                    content=str(episode.get("summary", "")),
                    reference=artifact_id,
                    score=float(score),
                    metadata={"session_id": episode.get("session_id")},
                )
            )
        return candidates


# ---------------------------------------------------------------------------
# Working memory — turn-scoped key/value context, cleared at turn boundary
# ---------------------------------------------------------------------------

@dataclass
class WorkingStore:
    items: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time)

    retention: RetentionPolicy = field(
        default_factory=lambda: RetentionPolicy(max_items=64, eviction="fifo", scope="turn")
    )

    def set(self, key: str, value: Any) -> None:
        if len(self.items) >= self.retention.max_items and key not in self.items:
            oldest = next(iter(self.items))
            del self.items[oldest]
        self.items[key] = value
        self.updated_at = time()

    def get(self, key: str, default: Any = None) -> Any:
        return self.items.get(key, default)

    def clear(self) -> None:
        self.items.clear()
        self.updated_at = time()


# ---------------------------------------------------------------------------
# Episodic memory — bounded conversation log, FIFO eviction
# ---------------------------------------------------------------------------

@dataclass
class EpisodicStore:
    frames: list[MemoryFrame] = field(default_factory=list)

    retention: RetentionPolicy = field(
        default_factory=lambda: RetentionPolicy(max_items=500, eviction="fifo", scope="session")
    )

    def append(self, role: str, content: str) -> MemoryFrame:
        frame = MemoryFrame(role=role, content=content)
        self.frames.append(frame)
        if len(self.frames) > self.retention.max_items:
            del self.frames[: len(self.frames) - self.retention.max_items]
        return frame

    def recent(self, limit: int = 100) -> list[MemoryFrame]:
        return self.frames[-limit:]

    def retrieve_related(self, query: str, limit: int = 50) -> list[MemoryFrame]:
        query_terms = {term.lower() for term in query.split() if term.strip()}
        if not query_terms:
            return self.recent(limit)
        scored: list[tuple[int, MemoryFrame]] = []
        for frame in self.frames:
            overlap = len(query_terms & set(frame.content.lower().split()))
            if overlap:
                scored.append((overlap, frame))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [f for _, f in scored[:limit]]

    def clear(self) -> None:
        self.frames.clear()


# ---------------------------------------------------------------------------
# Semantic memory — key-addressed facts, durable within session
# ---------------------------------------------------------------------------

@dataclass
class SemanticItem:
    key: str
    content: str
    ts: float = field(default_factory=time)
    retention: str = "durable"


@dataclass
class SemanticStore:
    items: list[SemanticItem] = field(default_factory=list)

    retention: RetentionPolicy = field(
        default_factory=lambda: RetentionPolicy(max_items=200, eviction="none", scope="durable")
    )

    def write(self, key: str, content: str) -> SemanticItem:
        for item in self.items:
            if item.key == key:
                item.content = content
                item.ts = time()
                return item
        item = SemanticItem(key=key, content=content)
        self.items.append(item)
        return item

    def read(self, key: str) -> SemanticItem | None:
        for item in self.items:
            if item.key == key:
                return item
        return None

    def all(self) -> list[SemanticItem]:
        return list(self.items)


# ---------------------------------------------------------------------------
# Procedural memory — governance/policy config, controlled write
# ---------------------------------------------------------------------------

@dataclass
class ProceduralStore:
    entries: dict[str, Any] = field(default_factory=dict)

    retention: RetentionPolicy = field(
        default_factory=lambda: RetentionPolicy(max_items=64, eviction="none", scope="stable_config")
    )

    def write(self, key: str, value: Any) -> None:
        self.entries[key] = value

    def read(self, key: str, default: Any = None) -> Any:
        return self.entries.get(key, default)


# ---------------------------------------------------------------------------
# MemoryStore — facade over behaviorally-distinct substrates.
# ---------------------------------------------------------------------------

@dataclass
class MemoryStore:
    episodic: EpisodicStore = field(default_factory=EpisodicStore)
    semantic: SemanticStore = field(default_factory=SemanticStore)
    working: WorkingStore = field(default_factory=WorkingStore)
    procedural: ProceduralStore = field(default_factory=ProceduralStore)
    stm: StmStore = field(default_factory=StmStore)
    mtm: SessionLedger = field(default_factory=SessionLedger)
    _proposals: list[ProposalMutation] = field(default_factory=list, repr=False)
    _legacy_notes: list[str] = field(default_factory=list, repr=False)
    _last_ltm_artifact: dict[str, Any] | None = field(default=None, repr=False)
    _ltm_toc_tail: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def frames(self) -> list[MemoryFrame]:
        return self.episodic.frames

    @frames.setter
    def frames(self, value: list[MemoryFrame]) -> None:
        self.episodic.frames = value

    @property
    def notes(self) -> tuple[str, ...]:
        return tuple(self._legacy_notes)

    @property
    def ltm_last_artifact(self) -> dict[str, Any] | None:
        return dict(self._last_ltm_artifact) if self._last_ltm_artifact is not None else None

    @property
    def ltm_toc_tail(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._ltm_toc_tail)

    def propose(self, proposal: ProposalMutation) -> None:
        """Stage a persistence candidate for evaluation at the next commit."""
        self._proposals.append(proposal)

    def drain_proposals(self) -> list[ProposalMutation]:
        """Return and clear the pending proposal list. Called by _promote_working_memory."""
        pending = list(self._proposals)
        self._proposals.clear()
        return pending

    def load_legacy_notes(self, notes: list[str]) -> None:
        self._legacy_notes = list(notes)
        for i, note in enumerate(notes):
            self.semantic.write(f"legacy_note_{i}", note)

    def append(self, role: str, content: str) -> MemoryFrame:
        frame = self.episodic.append(role, content)
        stm_result = self.stm.append_frame(frame)
        self.mtm.append_frame(
            frame,
            epoch_seq=stm_result.epoch_seq,
            metadata={
                "epoch_id": stm_result.epoch_id,
                "epoch_closed": stm_result.epoch_closed,
                "evicted_epoch_ids": list(stm_result.evicted_epoch_ids),
            },
        )
        if stm_result.epoch_closed:
            self.mtm.append_event(
                event_kind="epoch_closed",
                epoch_seq=stm_result.epoch_seq,
                metadata={"epoch_id": stm_result.epoch_id},
            )
        for evicted_id in stm_result.evicted_epoch_ids:
            self.mtm.append_event(
                event_kind="stm_epoch_evicted",
                metadata={"epoch_id": evicted_id},
            )
        return frame

    def recent(self, limit: int = 100) -> list[MemoryFrame]:
        return self.episodic.recent(limit)

    def retrieve_related(self, query: str, limit: int = 50) -> list[MemoryFrame]:
        return self.episodic.retrieve_related(query, limit)

    def close_epoch(self, reason: str = "session_boundary") -> StmEpoch | None:
        closed, evicted = self.stm.force_close(reason=reason)
        if closed is not None:
            self.mtm.append_event(
                event_kind="epoch_closed",
                epoch_seq=closed.epoch_seq,
                metadata={"epoch_id": closed.epoch_id, "reason": reason},
            )
        for evicted_id in evicted:
            self.mtm.append_event(
                event_kind="stm_epoch_evicted",
                metadata={"epoch_id": evicted_id, "reason": reason},
            )
        return closed

    def close_session(self, session_id: str, ltm_root: Path) -> LtmEpisodeArtifact:
        self.close_epoch(reason="session_closed")
        self.mtm.append_event(event_kind="session_closed", metadata={"session_id": session_id})
        archive = LtmArchive(ltm_root)
        self.mtm.append_event(event_kind="compression_started", metadata={"session_id": session_id})
        artifact = archive.compress_session(session_id=session_id, ledger=self.mtm, stm=self.stm)
        self.mtm.append_event(
            event_kind="compression_completed",
            metadata={"session_id": session_id, "artifact_id": artifact.artifact_id},
        )
        self.mtm.append_event(
            event_kind="ltm_promoted",
            metadata={"session_id": session_id, "artifact_id": artifact.artifact_id},
        )
        toc = archive._load_toc().get("episodes", [])
        self._last_ltm_artifact = artifact.to_dict()
        self._ltm_toc_tail = [dict(item) for item in toc[-24:]]
        return artifact
