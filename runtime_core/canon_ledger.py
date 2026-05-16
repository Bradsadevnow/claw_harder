from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Dict, Literal
import uuid

# Alignment Sources
SYSTEM_INIT = "SYSTEM_INIT"
USER_NEGOTIATION = "USER_NEGOTIATION"
RCW_EVENT = "RCW_EVENT"
SYSTEM_GENERATED = "SYSTEM_GENERATED"

AlignmentSource = Literal["SYSTEM_INIT", "USER_NEGOTIATION", "RCW_EVENT", "SYSTEM_GENERATED"]

@dataclass
class Revision:
    """A single atomic update to the Institutional Truth."""
    revision_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    origin_id: str | None = None # Lineage trace to progenitor fragment
    epoch_effective: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    alignment_source: AlignmentSource = SYSTEM_GENERATED
    continuity_flags: List[str] = field(default_factory=list) # REINTERPRETED, CONTRADICTION_RESOLVED, etc.
    claims: List[Dict[str, Any]] = field(default_factory=list) # Structured machine-readable assertions
    edges: List[Dict[str, str]] = field(default_factory=list) # { "type": "contradiction", "target": "rev_id" }
    content: str = "" # The human-readable doctrine

    def to_dict(self) -> Dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "origin_id": self.origin_id,
            "epoch_effective": self.epoch_effective,
            "timestamp": self.timestamp,
            "alignment_source": self.alignment_source,
            "continuity_flags": self.continuity_flags,
            "claims": self.claims,
            "edges": self.edges,
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Revision:
        return cls(**data)

@dataclass
class CanonLedger:
    """An append-only memory graph of Revision objects."""
    revisions: List[Revision] = field(default_factory=list)
    
    def append(self, revision: Revision) -> None:
        self.revisions.append(revision)

    def get_revision(self, revision_id: str) -> Revision | None:
        for r in self.revisions:
            if r.revision_id == revision_id:
                return r
        return None

    def to_list(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.revisions]

    @classmethod
    def from_list(cls, data: List[Dict[str, Any]]) -> CanonLedger:
        return cls(revisions=[Revision.from_dict(d) for d in data])
