# Phase 5: ManifestManager — The Constitutional Layer

**Status:** Complete  
**File:** `src/manifest/manifest-manager.ts`

## Overview

ManifestManager is the only legal mutation boundary for manifest state. You cannot modify facts, references, or summaries except through its typed API. This is intentional: it's where the belief engine becomes a governed runtime.

Every mutation path enforces:
- **Arbitration** — no silent overwrites, ever
- **Lineage** — every fact has provenance (`derived_from`, `supersedes`, `merged_from`)
- **Operations log** — every change is recorded with actor + revision + metadata
- **Index consistency** — indexes are updated incrementally on every mutation
- **Staleness propagation** — summaries are marked stale the moment their source facts change

## API Surface

```ts
// Lifecycle
ManifestManager.load(manifestPath, artifactsDir, actor?)  // static factory
manager.commit()                                          // atomic write to disk
manager.reload()                                          // sync & replan from disk

// Mutations
manager.addFact(input)                   // arbitrated fact insertion
manager.addReference(input)              // artifact storage + pointer
manager.addSummary(input)                // derived view with staleness tracking
manager.proposeRefinement(factId, input) // explicit supersession intent
manager.proposeConsolidation(ids, input) // merge N facts into one authoritative
manager.supersedeFact(factId, reason)    // manual lifecycle transition
manager.invalidateFact(factId, reason)   // mark false by subsequent evidence
manager.recordOperation(type, metadata)  // log non-mutation events

// Queries
manager.queryFacts(filter)       // live facts by type/state/source/confidence
manager.getFact(factId)
manager.getReference(refId)
manager.getSummary(summaryId)
manager.getActiveSummaries(type?)
manager.getConflictedFacts()
```

## Key Behaviors

### addFact outcomes

All five arbitration outcomes are handled with correct semantics:

| Outcome | What happens |
|---------|-------------|
| `new` | Fact added as `active` |
| `converge` | No new fact; existing `observation_count++`, confidence `+0.02` |
| `supersede` | New fact `active`, existing → `superseded`, summaries staled |
| `reject` | New fact added as `superseded` (lineage preserved), existing strengthened |
| `conflict` | Both facts → `conflicted`, summaries staled |

### proposeRefinement forces intent

`proposeRefinement()` runs normal arbitration first. If arbitration doesn't supersede (confidence battle lost), it forces the refinement through anyway. A propose_refinement is explicit operator intent — it overrides normal arbitration. The overridden decision is logged with `forced: true` for audit.

### proposeConsolidation preserves lineage

When N facts are merged into one authoritative fact:
- The new fact has `merged_from: [id1, id2, ...]` and `derived_from: [id1, id2, ...]`
- All source facts transition to `superseded`
- The new fact inherits all `supporting_sources` from the merged facts
- Staleness propagates from every superseded source

### commit() is idempotent on clean state

If `isDirty === false`, `commit()` returns immediately with current revision. No I/O, no revision increment.

### reload() is the Sync & Replan entry point

When `commit()` returns `{ ok: false, reason: "revision_conflict" }`:

```ts
// Sync & Replan pattern
const commitResult = await manager.commit();
if (!commitResult.ok && commitResult.reason === "revision_conflict") {
  await manager.reload();
  // Rebuild operational context, then re-apply mutations.
}
```

## What Phase 4 Hardening Will Address

Building ManifestManager revealed the actual index access patterns:

1. `facts_by_type` — hit by `queryFacts({ type })`, the most common filter
2. `facts_by_identity_key` — hit by every `addFact` for arbitration
3. `summary_deps` — hit by every state transition for staleness propagation
4. `active_facts` / `conflicted_facts` — hit by `getConflictedFacts()` and default query

What we **don't** actually need in Phase 4:
- `facts_by_host` — Chelys-specific, no analog in personal-agent context
- `refs_by_source` — only used for diagnostics, not hot paths

Phase 4 hardening should focus on: index validation on load, index/manifest divergence detection, and efficient bulk queries.

## LM Studio Validation Target (Phase D)

**Model:** `openai/gpt-oss-20b` at `http://192.168.1.129:1234`

**Test:** Objective Handoff — start a ManifestManager with a pre-populated manifest (objective + backlog). Simulate process restart (new ManifestManager.load()). Verify the agent reads operational state from the manifest and resumes without re-deriving context from history.

See `docs/phases/phase-4-indexes.md` (hardening pass) and `docs/phases/phase-6-context-engine.md` for next steps.
