import re
import json
import time
import hashlib
import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Union

from .domain_adapter import DomainSnapshot
from .resource_adapter import ResourceAllocationAdapter
from .code_adapter import CodeExecutionAdapter

class IntegrityError(Exception):
    """Raised when an observer or state violates the Root of Trust."""
    pass

@dataclass
class ExecutionSnapshot:
    """A deterministic snapshot of the reasoning state across any domain."""
    cycle: int
    variables: Dict[str, Any] = field(default_factory=dict)
    valid_truth: bool = True
    violations: List[str] = field(default_factory=list)
    transition_class: str = "unknown" # backtrack, fake_continuity, narrative_collapse, logical_step
    explanation: str = ""
    raw_monologue: str = ""
    domain: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Metadata for legacy compatibility if needed
    @property
    def allocations(self) -> Dict[str, float]:
        return {k: float(v) for k, v in self.variables.items() if isinstance(v, (int, float))}

@dataclass
class TrackedClaim:
    """A generic claim for audit (used in Coherence trials)."""
    claim_type: str # e.g. "purpose"
    claim_text: str
    target_trait: str
    proposed_change: str
    status: str # accepted, rejected, unknown
    seq_ref: Optional[int] = None

from .refactor_adapter import RefactorAdapter

import hashlib

class ConstraintExtractor:
    """
    The Domain-Agnostic Reasoning Probe.
    Delevalidators domain-specific logic to Adapters.
    """

    def __init__(self, client: Any = None):
        self.client = client # Optional LLM client for annotations
        self.adapters = {
            "resource_allocation": ResourceAllocationAdapter(),
            "code_execution": CodeExecutionAdapter(),
            "refactor_verification": RefactorAdapter()
        }

    def _compute_file_hash(self, path: str) -> str:
        """Computes SHA-256 hash of an adapter file."""
        sha256_hash = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except FileNotFoundError:
            return "MISSING"

    def _get_adapter(self, domain: str, root_of_trust: Dict[str, str]):
        adapter = self.adapters.get(domain)
        if not adapter:
            adapter = self.adapters["refactor_verification"]
        
        # ROOT OF TRUST VERIFICATION
        # We find the file path of the adapter class
        import sys
        module = sys.modules[adapter.__class__.__module__]
        file_path = getattr(module, "__file__", None)
        
        if file_path and domain in root_of_trust:
            actual_hash = self._compute_file_hash(file_path)
            expected_hash = root_of_trust[domain]
            if actual_hash != expected_hash:
                raise IntegrityError(
                    f"Observer Integrity Failure: Domain Adapter '{domain}' has drifted from the Root of Trust. "
                    f"Expected: {expected_hash[:8]}, Actual: {actual_hash[:8]}. "
                    "SYSTEM COMPROMISE SUSPECTED."
                )
        
        return adapter

    def extract_resource_snapshot(self, 
                                  cycle: int, 
                                  monologue: str, 
                                  current_working: Any, 
                                  prev_snapshot: Optional[ExecutionSnapshot] = None,
                                  root_of_trust: Optional[Dict[str, str]] = None) -> ExecutionSnapshot:
        """
        Extracts execution variables using the active DomainAdapter.
        """
        domain = getattr(current_working, "domain", "resource_allocation")
        adapter = self._get_adapter(domain, root_of_trust or {})
        
        # 1. Domain-Specific Extraction (Capture a deep-copy of current state FIRST)
        # This prevents in-place mutation tricks from signaling the reference.
        initial_vars = copy.deepcopy(getattr(current_working, "allocations", {}))
        
        domain_snap = adapter.extract_snapshot(monologue, current_working)
        
        snapshot = ExecutionSnapshot(
            cycle=cycle, 
            raw_monologue=monologue,
            variables=copy.deepcopy(domain_snap.variables),
            domain=domain,
            metadata=copy.deepcopy(domain_snap.raw_data)
        )
        
        # 2. Domain-Specific Validation (The Truth)
        snapshot.violations = adapter.validate_snapshot(domain_snap, current_working)
        snapshot.valid_truth = (len(snapshot.violations) == 0)

        # 3. Universal Transition Classification (Logic Manager)
        if prev_snapshot:
            snapshot.transition_class = self._classify_transition(snapshot, prev_snapshot)
        else:
            snapshot.transition_class = "initial_step"

        # 4. Model-Assisted Annotation (Optional Fuzzy Labels)
        if self.client:
            snapshot.explanation = self._annotate_explanation(monologue)

        return snapshot

    def _classify_transition(self, curr: ExecutionSnapshot, prev: ExecutionSnapshot) -> str:
        """Determines the nature of the state change using Hash-Based Value Identity."""
        
        # Check for Backtrack Markers
        backtrack_keywords = ["backtrack", "rewind", "invalidate", "correction", "revised", "re-evaluating"]
        monologue_lowered = curr.raw_monologue.lower()
        has_backtrack_markers = any(kw in monologue_lowered for kw in backtrack_keywords)

        # HASH-BASED DELTA DETECTION
        def get_vars_hash(v: dict) -> str:
            return hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()

        curr_hash = get_vars_hash(curr.variables)
        prev_hash = get_vars_hash(prev.variables)
        state_changed = (curr_hash != prev_hash)

        # NARRATIVE COLLAPSE: Variables disappeared (loss of state binding)
        if not curr.variables and prev.variables:
            return "narrative_collapse"

        # GENUINE BACKTRACK: explicit markers + state change toward validity
        if has_backtrack_markers and len(curr.violations) < len(prev.violations):
            return "genuine_backtrack"
            
        # LOGICAL STEP: adding new variables, maintaining consistency, or VERIFIED updates
        is_growth = any(k not in prev.variables for k in curr.variables)
        is_consistent = curr.variables and all(curr.variables.get(k) == prev.variables.get(k) for k in prev.variables if k in curr.variables)
        
        # 1. Verification Coupling (Intent + Delta)
        # We look for explicit intent: "Updating X to Y"
        # Since domain is pluggable, we check for general update patterns
        mutation_intent_keywords = ["update", "adjust", "set", "re-balance", "change", "allocation", "step"]
        has_general_intent = any(kw in monologue_lowered for kw in mutation_intent_keywords)
        
        # Note: state_changed is already computed via hashes above
        
        if is_growth or is_consistent or (state_changed and has_general_intent and curr.valid_truth):
            return "logical_step"
  
        # FAKE CONTINUITY: variables changed in a way that isn't growth, consistency, or verified intent
        if state_changed:
            return "fake_continuity"
            
        return "logical_step" # No change is still a logical step (idle/deliberation)

    def _annotate_explanation(self, monologue: str) -> str:
        """Uses a small model to extract the 'Why' of the step."""
        prompt = f"Extract a one-sentence explanation of the reasoning in this monologue:\n\n{monologue}\n\nExplanation:"
        try:
            response = self.client.chat.completions.create(
                model="local-model",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=60,
                temperature=0.0
            )
            return response.choices[0].message.content.strip()
        except:
            return "Annotation Failed"

    def extract_tracked_claim(self, event: Dict[str, Any]) -> Optional[TrackedClaim]:
        """Extracts generic identity claims for coherence audit."""
        kind = event.get("kind")
        if kind != "seed.identity_confirmed": return None
        
        details = event.get("details", {})
        item = details.get("item", {})
        
        return TrackedClaim(
            claim_type=details.get("type", "unknown"),
            claim_text=item.get("content", ""),
            target_trait=details.get("type", ""),
            proposed_change=item.get("content", ""),
            status="accepted" if details.get("module") != "rejected" else "rejected", # Simplified
            seq_ref=event.get("seq")
        )
