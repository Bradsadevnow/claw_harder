# Phase 1: Schemas

**Status:** Complete  
**File:** `src/types/manifest.ts`

## Overview

The manifest type system is the ontological foundation of the engine. Every subsequent layer — persistence, arbitration, indexing, context injection — derives its correctness guarantees from these types.

## Design Decisions

### Facts are immutable

Once a `Fact` is written, its fields never change. Refinement creates a *successor* fact with a `supersedes` pointer to the original. This gives us:

- Forensic replayability: reconstruct the full evolution of beliefs
- No silent state rot: every change is a new record with provenance
- Safe concurrent access: readers never see partially-updated state

### Identity keys drive deduplication

Every fact carries a deterministic `identity_key` derived from its type and stable fields (e.g. `contact:alice@example.com`). When a new fact arrives, the arbitration engine looks for active facts with the same `identity_key` and decides: new, converge, supersede, or conflict.

### Confidence scores arbitrate uncertainty

`confidence: 0.0–1.0`. When two facts share an `identity_key` and the confidence delta is `< 0.15`, neither wins — state is set to `conflicted`. When delta `>= 0.15`, higher confidence supersedes lower. Repeated observation increments `observation_count` and strengthens confidence.

### Discovery index enables natural-language addressing

Every fact gets a stable `discovery_index` (1, 2, 3…) assigned at creation. Agents can say "the third contact added this session" and get a deterministic answer.

### Summary staleness propagates in O(1)

`ManifestIndexes.summary_deps` maintains a reverse map from `fact_id → [summary_ids]`. When a fact changes state (superseded, conflicted, invalidated), all dependent summaries are instantly flagged `stale: true` — no scan needed.

## Type Hierarchy

```
Manifest
├── facts: Record<FactId, Fact>
│   ├── identity_key (deduplication handle)
│   ├── fact_hash (SHA256 — exact dedup)
│   ├── state: active | archived | superseded | invalidated | conflicted
│   ├── observations: first_seen / last_seen / observation_count
│   └── lineage: derived_from / supersedes / merged_from
├── references: Record<RefId, Reference>
│   └── pointer to artifact on disk (content_hash for integrity)
├── summaries: Record<SummaryId, Summary>
│   ├── stale: bool (propagated via summary_deps index)
│   └── recommended_next_actions
└── operations: Operation[]  (append-only forensic log)
```

## Extensibility

`FactType` and `SummaryType` include `string` in the union — plugins can define their own types without modifying this package. The arbitration engine uses the `identity_key` strategy registry to handle custom types.

## LM Studio Validation Target (Phase A)

**Model:** `openai/gpt-oss-20b` at `http://192.168.1.129:1234`

**Test:** Warm Start — provide an agent with a pre-populated manifest (no chat history). Verify the agent acts on manifest state directly without re-deriving facts.

See `docs/phases/phase-2-persistence.md` for next steps.
