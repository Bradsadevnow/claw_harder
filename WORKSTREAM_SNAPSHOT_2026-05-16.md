# Workstream Snapshot — May 16, 2026

Scope: `/home/brad/openclaw` (`runtime_core`, `iris`, `HSP`, notes)

## Why this document exists
This is a continuity checkpoint so we can resume quickly without drift.

## Completed Work (confirmed)
1. Unified organism persistence model in `runtime_core/state.py`.
2. Canonical state shape introduced:
   - `organism.identity`
   - `organism.affective_state`
   - `organism.stm`
3. Backward compatibility preserved:
   - legacy mirrors (`identity`, `signal`, `memory`) are still emitted.
   - loader precedence is `organism.*` first, then legacy fields.
4. Schema version bumped to `6`.
5. Added regression tests for organism schema:
   - canonical organism emission
   - organism-over-legacy precedence
   - legacy fallback load
6. Extended `TruthAPI` to expose organism externally:
   - `get_identity()` now includes `organism` alongside legacy `identity` and `signal`.
   - new `get_organism()` endpoint-style accessor for canonical organism projection.
7. Added bounded affective trajectory projection in `TruthAPI`:
   - derived from event-log `signal.shift` and `signal.decay`
   - includes causal/provenance metadata (`seq`, operation IDs, cause, delta, projected state).
8. Added replay/live parity tests around affective evolution path and endpoint equivalence.

## Current Architectural Direction
We are explicitly separating two layers:

1. Manifest layer (epistemic layer)
   - facts, evidence, lineage, arbitration, summaries.
   - source of truth handling and conflict semantics.

2. Organism layer (runtime self layer)
   - identity, affective modulation, short-term cognition.
   - active behavioral context under governance constraints.

This avoids “everything becomes memory” collapse and keeps truth lineage separate from transient modulation.

## Current State of the System
1. Organism is now canonical for persistence and truth projection.
2. External introspection is now possible without scraping internal runtime structures.
3. Affective trajectory can be inspected as bounded transition history, not just snapshot state.
4. Tests currently pass for:
   - organism schema behavior
   - trajectory bounds/causality shape
   - replay/live parity checks in the new test scope.

## Open Questions (intentionally open)
1. Live modulation wiring:
   - Should `_pulse_logic` actively call `identity_engine.analyze_monologue(...)` and emit `signal.shift` every turn now, or later?
2. Causality taxonomy:
   - Finalize canonical `cause` tags (interaction, governance, reflection, conflict, recall, self_regulation, etc.).
3. Provenance depth:
   - How much operation linkage should be exposed by default vs. privileged/debug views?
4. TruthAPI exposure policy:
   - Decide which organism fields are always externally visible and which require governance filtering.
5. Trajectory contract:
   - Lock max window defaults and payload guarantees for downstream consumers.

## Guardrails to Preserve
1. Keep firewall sacred:
   - Manifest truth lineage must not be rewritten by affective state.
2. Organism may modulate planning/priority, but should not mutate evidence directly.
3. Maintain deterministic serialization and bounded projections.
4. Keep backward compatibility until all known consumers converge.

## Suggested Next Moves (when resuming)
1. Freeze an explicit TruthAPI organism schema contract doc (`docs/ORGANISM_TRUTH_CONTRACT.md`).
2. Define trajectory cause vocabulary + provenance policy in one place.
3. Decide the live `signal.shift` wiring question (deferred on purpose).
4. Add one integration test that compares replayed organism trajectory against a recorded multi-turn fixture log.

## Resume Anchor
If restarting cold, begin with:
1. Read this file.
2. Read `CODEX_BRAINMAP_NOTES.md` latest sections.
3. Run:
   - `python3 -m unittest discover -s runtime_core/tests -p "test_*.py"`
4. Continue from the open question on live modulation wiring.

---
Owner intent: preserve continuity and avoid drift during long architectural work.

## Execution Order Update (2026-05-16, later)

Chosen sequence (architecturally locked):
1. Memory contracts (STM/MTM/LTM + admissibility)
2. Runtime seam cleanup (`runtime.contract...` boot blockers)
3. Live `signal.shift` wiring

### Phase 1 status
Implemented in `runtime_core`:
- STM epoch model with whole-epoch eviction (`max_epochs=6` default).
- MTM append-only session ledger with monotonic sequence IDs.
- LTM compression artifacts (`ltm.jsonl`) plus deterministic TOC (`ltm_toc.json`).
- Lifecycle events recorded in MTM (`epoch_closed`, `session_closed`, `compression_started`, `compression_completed`, `ltm_promoted`, `stm_epoch_evicted`).
- Admissibility surface with explicit policy string:
  - `Associative Recall Is Non-Authoritative`
  - semantic candidates reject by default without deterministic governance admission.
- Optional semantic nomination sidecar API (`SemanticNominationIndex`) that nominates only.

Validation:
- `python3 -m unittest discover -s runtime_core/tests -p "test_*.py"` => pass
- New test file: `runtime_core/tests/test_memory_contracts.py`

