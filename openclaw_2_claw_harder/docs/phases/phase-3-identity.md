# Phase 3: Identity Strategy & Arbitration

**Status:** Complete  
**Files:** `src/manifest/identity.ts`, `src/manifest/arbitration.ts`

## The Core Distinction

> identity ≠ equality

Two observations can refer to the same entity while making contradictory claims. The system must model this explicitly — not by overwriting, not by ignoring, not by averaging.

`identity_key` identifies **what entity** is being observed.  
`fact_hash` identifies **what value** was observed about that entity.

Same `identity_key` + same `fact_hash` = same thing seen twice → converge.  
Same `identity_key` + different `fact_hash` = same thing seen differently → arbitrate.

## Identity Key Strategies

Each fact type has a registered strategy that extracts the stable identifying fields from `value`. Crucially: these fields identify the **entity**, not the observation.

| Type | Identity anchor | Example key |
|------|----------------|-------------|
| `preference` | topic/key | `preference:preferred-language` |
| `contact` | email → id → username → name | `contact:alice@example.com` |
| `task` | id → title | `task:deploy-staging` |
| `context` | scope + key | `context:location:current` |
| `credential` | username@service | `credential:brad@github` |
| `service` | name + endpoint | `service:github:api.github.com` |
| `knowledge` | domain + key | `knowledge:typescript:module-resolution` |
| `constraint` | scope + key | `constraint:global:no-external-calls` |
| `event` | type + id (or hash if no id) | `event:login:evt_abc123` |

**Unknown types** fall back to `{type}:{sha256(value)[:8]}` — identical values deduplicate, different values never falsely conflict. Conservative, safe.

Custom types register strategies via `registerStrategy(type, fn)` — plugins extend without modifying core.

## Arbitration Decisions

When `addFact` is called and an existing active/conflicted fact shares the same `identity_key`:

```
incoming.fact_hash === existing.fact_hash
  → "converge": don't add new fact, strengthen existing (observation_count++, confidence += 0.02)

incoming.fact_hash !== existing.fact_hash:
  delta = incoming.confidence - existing.confidence

  delta >= +0.15  → "supersede": new fact wins, existing → superseded
  delta <= -0.15  → "reject": existing wins, new fact added as superseded (for lineage)
  |delta| < 0.15  → "conflict": neither wins, both → conflicted
```

**Uncertainty is a first-class state.** Agents don't get a forced answer when evidence is genuinely ambiguous. The system holds the contradiction and surfaces it as `[STATE: CONFLICTED]` in context injection.

## Convergence Bonus

Each additional corroborating observation adds `+0.02` confidence to the existing fact (capped at `1.0`). This means:

- Facts seen once by one source start weak.
- Facts independently confirmed by multiple sources strengthen over time.
- The system naturally gravitates toward well-corroborated beliefs without requiring explicit confidence management.

## Staleness Propagation

`collectStaleSummaries(factId, indexes)` returns all summary IDs that depend on a changed fact via the `summary_deps` reverse index. Cost is O(1) — no manifest scan. Called by `ManifestManager` whenever a fact transitions state.

## LM Studio Validation Target (Phase C)

**Model:** `openai/gpt-oss-20b` at `http://192.168.1.129:1234`

**Test:** Fingerprint Conflict — populate manifest with three contradictory facts about the same entity at varying confidence levels. Verify the agent:
- Identifies the highest-confidence fact as authoritative
- Correctly labels the near-tie as `[STATE: CONFLICTED]`
- Does not force a guess when confidence delta is below threshold
- Resolves conflict via `propose_refinement` with higher-confidence new observation

See `docs/phases/phase-4-indexes.md` for next steps.
