import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Any

from .replay import _load_events


@dataclass
class IdentityProposal:
    source: str
    trait: str
    value: Any
    timestamp: float
    confidence: float = 1.0
    session_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimulationResult:
    trait: str
    resolved_value: Any
    resolved_from: str
    resolution_method: str
    determinism_hash: str
    payload: dict[str, Any]


class SimulationProcessor:
    """
    Collects identity proposals from the event log.
    Resolves them by configured strategy.
    Emits seed.identity_confirmed back to the event log.

    Idempotent: Tracks the last processed proposal via event sequence bounds to prevent duplicate confirmations.
    """

    def __init__(self, log_path: Path, strategy: str = "confidence_weighted_last"):
        self.log_path = log_path
        self.strategy = strategy

    def run(self, session_id: str | None = None) -> list[SimulationResult]:
        events = _load_events(self.log_path)
        proposals = self._collect_proposals(events, session_id)
        if not proposals:
            return []
            
        grouped = self._group_by_trait(proposals)
        confirmed_state = self._get_confirmed_state(events)
        results = []

        # Find the last run_id and seq to attach events appropriately
        last_run_id = ""
        last_cycle = 0
        last_seq = 0
        if events:
            last_run_id = events[-1].get("run_id", "")
            last_seq = events[-1].get("seq", 0)
            last_cycle = events[-1].get("cycle", 0)

        with open(self.log_path, "a", encoding="utf-8") as f:
            for trait, candidates in grouped.items():
                resolved = self._resolve(trait, candidates, confirmed_state)
                if not resolved:
                    continue
                results.append(resolved)
                
                last_seq += 1
                event = {
                    "run_id": last_run_id,
                    "cycle": last_cycle,
                    "seq": last_seq,
                    "kind": "seed.identity_confirmed",
                    "module": "simulation",
                    "level": "info",
                    "msg": f"Resolved identity trait: {trait}",
                    "details": {
                        "type": trait,
                        "item": resolved.payload,
                        "resolved_from": resolved.resolved_from,
                        "resolution_method": resolved.resolution_method,
                        "determinism_hash": resolved.determinism_hash,
                    },
                    "timestamp": time(),
                    "parent_seq": last_seq - 1,
                    "event_id": str(uuid.uuid4()),
                }
                f.write(json.dumps(event) + "\n")

        return results

    def _collect_proposals(self, events: list[dict], session_id: str | None) -> list[IdentityProposal]:
        # Implementation constraint: only process proposals that occurred AFTER the last simulation cycle.
        # We can detect the last simulation cycle for a trait by looking at the last 'seed.identity_confirmed' for that trait.
        # Or simpler: find the maximum timestamp of 'seed.identity_confirmed' proposals-resolved array, 
        # but to be fully robust, let's keep track of proposals that are newer than the last confirmed event for their trait.
        
        last_confirmed_ts: dict[str, float] = {}
        for event in events:
            if event.get("kind") == "seed.identity_confirmed":
                details = event.get("details", {})
                item = details.get("item", {})
                trait = details.get("type", item.get("type"))
                if not trait:
                    continue
                ts = event.get("timestamp", 0.0)
                last_confirmed_ts[trait] = max(last_confirmed_ts.get(trait, 0.0), ts)

        proposals = []
        for event in events:
            if event.get("kind") != "seed.identity_proposed":
                continue
                
            details = event.get("details", {})
            trait = details.get("type")
            if not trait:
                continue

            ts = event.get("timestamp", 0.0)
            
            # IDEMPOTENCY FIX: Watermark check. Skip proposals that happened before or at the time we already confirmed this trait.
            # We add a tiny buffer to avoid skipping exact millisecond proposals, but since events append serially, timestamp is monotonically non-decreasing over log.
            # Actually, just checking if event is before the confirmed event is enough.
            if ts <= last_confirmed_ts.get(trait, -1.0):
                continue
                
            if session_id and event.get("run_id") != session_id:
                continue
                
            item = details.get("item", {})

            proposals.append(IdentityProposal(
                source=event.get("module", "unknown"),
                trait=trait,
                value=item.get("content", ""),
                timestamp=ts,
                confidence=float(item.get("confidence", 1.0)),
                session_id=event.get("run_id", ""),
                payload=item,
            ))
            
        return proposals

    def _get_confirmed_state(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Project the current confirmed identity state from the event log."""
        state = {}
        for event in events:
            if event.get("kind") == "seed.identity_confirmed":
                details = event.get("details", {})
                item = details.get("item", {})
                trait = details.get("type", item.get("type"))
                if trait:
                    state[trait] = item.get("content")
        return state

    def _group_by_trait(self, proposals: list[IdentityProposal]) -> dict[str, list[IdentityProposal]]:
        groups: dict[str, list[IdentityProposal]] = {}
        for p in proposals:
            groups.setdefault(p.trait, []).append(p)
        return groups

    def _resolve(self, trait: str, candidates: list[IdentityProposal], confirmed_state: dict[str, Any]) -> SimulationResult | None:
        from .traits.validator import IDENTITY_TRAIT_CONFIG, merge_identity_trait
        
        config = IDENTITY_TRAIT_CONFIG.get(trait, {})
        is_mutable = config.get("mutable", True)
        is_mergeable = config.get("merge", False)
        
        # 1. Mutability Check: If immutable and already confirmed, ignore candidates
        if not is_mutable and trait in confirmed_state:
            return None
            
        # 2. Sort candidates for determinism
        candidates = sorted(candidates, key=lambda p: (p.timestamp, p.source))
        
        if is_mergeable:
            # 3. Merge Logic
            existing = confirmed_state.get(trait, [])
            merged = existing
            for cand in candidates:
                merged = merge_identity_trait(trait, merged, cand.value)
            
            winner = candidates[-1]
            return SimulationResult(
                trait=trait,
                resolved_value=merged,
                resolved_from=winner.source,
                resolution_method="identity_merge",
                determinism_hash=self._hash(trait, winner),
                payload={**winner.payload, "content": merged},
            )
        else:
            # 4. Standard Resolution (Last Write Wins / Confidence)
            if self.strategy == "last_write_wins":
                winner = candidates[-1]
                method = "last_write_wins"
            elif self.strategy == "confidence_weighted_last":
                max_conf = max(p.confidence for p in candidates)
                top = [p for p in candidates if p.confidence >= max_conf]
                winner = top[-1]
                method = "confidence_weighted_last"
            else:
                raise ValueError(f"Unknown resolution strategy: {self.strategy}")

            return SimulationResult(
                trait=trait,
                resolved_value=winner.value,
                resolved_from=winner.source,
                resolution_method=method,
                determinism_hash=self._hash(trait, winner),
                payload=winner.payload,
            )

    def _hash(self, trait: str, proposal: IdentityProposal) -> str:
        payload = json.dumps({
            "trait": trait,
            "value": proposal.value,
            "source": proposal.source,
            "timestamp": proposal.timestamp,
            "confidence": proposal.confidence,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
