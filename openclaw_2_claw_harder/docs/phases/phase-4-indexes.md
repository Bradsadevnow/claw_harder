# Phase 4: Index Hardening

**Status:** Complete  
**File:** `src/manifest/indexing.ts`

## Overview

Phase 4 was intentionally deferred until after ManifestManager (Phase 5) existed, so index design could be driven by actual access patterns rather than abstract planning.

Completing Phase 5 revealed the real hot paths:

| Index | Hit by | Frequency |
|-------|--------|-----------|
| `facts_by_identity_key` | Every `addFact()` — arbitration lookup | Very hot |
| `facts_by_type` | `queryFacts({ type })` — most common filter | Hot |
| `summary_deps` | Every fact state transition — staleness propagation | Hot |
| `active_facts` | Injection compiler, `getConflictedFacts()` | Hot |
| `conflicted_facts` | Injection compiler, `getConflictedFacts()` | Hot |
| `facts_by_source` | Provenance queries | Cool |
| `refs_by_source` | Diagnostic / recall queries | Cool |

## What Phase 4 Added

### 1. Incremental update helpers

Rather than rebuilding all indexes on every mutation (O(n)), Phase 4 provides per-fact helpers for incremental updates:

```ts
indexFact(indexes, fact)                                // add a new fact to all indexes
reindexFactState(indexes, factId, prevState, newState)  // state transition — O(1) set ops
indexSummaryDeps(indexes, summaryId, sourceFactIds)     // register summary dependency map
```

ManifestManager uses these for O(1) incremental maintenance. Full rebuild (`buildIndexes`) only happens on `load()` and `reload()`.

### 2. Index validation

`validateIndexes(manifest, indexes)` is a debug guard that catches any case where:
- A fact appears in an index but not the manifest
- A fact's state is wrong in the active/conflicted sets
- `summary_deps` references a non-existent fact
- `refs_by_source` contains orphaned entries

This should never fail in production — it detects bugs in `buildIndexes` or `indexFact`, not user data corruption. Called inside `persistence.load()` after index build.

### 3. Lineage validation (enhanced)

`validateLineage(manifest)` now also detects circular supersession:
```
Circular supersession: FACT_001 ↔ FACT_002
```

The persistence layer's `validateLineage` checks `derived_from / supersedes / merged_from` across all facts and summaries. The indexing module adds circular reference detection.

### 4. Orphan reference detection

`findOrphanedRefs(manifest)` identifies `ref_id`s in `manifest.references` with no corresponding fact or summary linking them. Returned as warnings, not errors — orphaned refs are valid artifacts that may be awaiting association.

## Design Decision: No Index Persistence

Indexes are **never serialized to disk**. They are always rebuilt from the authoritative manifest on load.

This was the right call. Reasons:
1. **No divergence possible** — the index is always a pure function of the manifest, computed fresh.
2. **Portability** — manifest files can be moved, copied, or diffed without stale index state.
3. **Restart robustness** — process crash between index update and disk write cannot corrupt state.
4. **Build cost is acceptable** — O(facts + summaries + refs) at load time. For session-scale manifests (hundreds of facts, not millions), this is microseconds.

If manifests grow to millions of facts (a different problem domain), an explicit index persistence layer with invalidation tokens would be the right upgrade path. That's not this project's constraint.

## Index/Manifest Invariant

At any point during a session, the following must hold:

```
indexes.active_facts    ≡  { f.fact_id | f ∈ manifest.facts ∧ f.state = "active" }
indexes.conflicted_facts ≡  { f.fact_id | f ∈ manifest.facts ∧ f.state = "conflicted" }
indexes.facts_by_type[T] ≡  { f.fact_id | f ∈ manifest.facts ∧ f.type = T }
indexes.facts_by_identity_key[K] ≡  { f.fact_id | f ∈ manifest.facts ∧ f.identity_key = K }
```

`validateIndexes()` asserts this invariant. If it ever fires, the bug is in `buildIndexes()` or in a `ManifestManager` mutation path that forgot to call `reindexFactState()`.

## Phase 4 vs Phase 5 Sequencing

Phase 4 was scoped after Phase 5 because:
- Phase 5 revealed which indexes are hot (via actual query patterns)
- Phase 4 provides the incremental tools Phase 5 needs
- Building Phase 4 first would have required guessing access patterns

The `facts_by_host` index from the Chelys Security Runtime has no analog here — personal-agent context doesn't have host-based routing. Excluded correctly.

## LM Studio Validation Target

Index performance is implicitly validated by all other validation tests. No dedicated index test needed — the invariants are structural guarantees, not performance questions at session scale.
