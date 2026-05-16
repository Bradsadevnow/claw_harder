# Architecture: OPENCLAW II: CLAW HARDER

**Version:** 0.1.0  
**Author:** Brad Bates

---

## What this is

A typed, versioned, append-only state substrate that replaces OpenClaw's naive transcript replay loop.

It plugs into OpenClaw's `plugins.slots.contextEngine` seam — no fork, no modification to OpenClaw. The entire system operates as a clean parasitic takeover through the existing plugin boundary.

---

## The problem it solves

OpenClaw's default context engine replays the full conversation transcript on every model call. This degrades in three ways as sessions grow:

1. **Token cost grows linearly** — every turn costs more than the last
2. **Restarts cause amnesia** — process death = complete context loss
3. **No epistemic structure** — facts, guesses, conflicts, and tool outputs are indistinguishable

The manifest engine replaces transcript replay with **state**. The model receives a structured, bounded operational context on every turn — the same context after a restart as during active use.

---

## Layer map

```
┌─────────────────────────────────────────────────────────────────┐
│  OpenClaw Runtime                                               │
│  plugins.slots.contextEngine: "manifest-engine"                 │
├─────────────────────────────────────────────────────────────────┤
│  ManifestContextEngineAdapter                                   │  ← OpenClaw seam
│  bootstrap | ingest | assemble | afterTurn | compact | dispose  │
├─────────────────────────────────────────────────────────────────┤
│  ContextEngine                                                  │  ← orchestrator
│  buildContext | buildMessages | completeTurn | syncAndReplan    │
├────────────────────┬────────────────────────────────────────────┤
│  SlidingWindow     │  InjectionCompiler                         │
│  bounded history   │  manifest → prompt strata                  │
│  auto-offload      │  select → rank → compress → inject         │
├────────────────────┴────────────────────────────────────────────┤
│  ManifestManager                                                │  ← constitutional layer
│  addFact | addReference | addSummary | proposeRefinement        │
│  queryFacts | commit | reload | recordOperation                 │
├────────────┬──────────────┬──────────────┬──────────────────────┤
│ Persistence│  Arbitration │  Identity    │  Indexing            │
│ atomic I/O │  5-outcome   │  deriveKey   │  7 in-memory maps    │
│ SHA256     │  engine      │  deriveHash  │  rebuilt on load     │
│ opt. lock  │              │  strategies  │                      │
├────────────┴──────────────┴──────────────┴──────────────────────┤
│  Manifest (manifest.json)                                       │
│  facts | references | summaries | operations | revision         │
└─────────────────────────────────────────────────────────────────┘
```

---

## The Manifest

The manifest is the single authoritative document. It is:
- Append-only internally — no in-place mutation, only new records
- Integrity-hashed (SHA256) — detects disk corruption before any access
- Revision-tracked (integer) — enables optimistic locking across process boundaries
- Human-readable JSON — can be inspected, diffed, and version-controlled

```json
{
  "schema_version": "1.0",
  "revision": 7,
  "integrity_hash": "sha256:...",
  "facts": { "FACT_A1B2C3...": { ... } },
  "references": { "REF_0001_00": { ... } },
  "summaries": { "SUM_D4E5F6...": { ... } },
  "operations": [ ... ],
  "last_fact_index": 12
}
```

### Facts

A fact is a typed, arbitrated belief about the world.

```typescript
interface Fact {
  fact_id: FactId;          // FACT_{12-char hash}
  fact_hash: string;        // SHA256(type + canonical_json(value)) — drives dedup
  identity_key: string;     // Deterministic entity address — drives arbitration
  type: FactType;           // constraint | task | credential | context | ...
  value: Record<string, unknown>;
  confidence: number;       // 0.0–1.0, accumulated epistemic support
  state: FactState;         // active | superseded | conflicted | invalidated | archived
  derived_from: FactId[];   // provenance chain
  supersedes?: FactId;      // which fact this replaced
  merged_from?: FactId[];   // which facts were consolidated into this one
  observations: { first_seen, last_seen, observation_count };
  supporting_sources: string[];
}
```

**Key distinction:**
- `identity_key` — who/what this belief is about (stable across versions)
- `fact_hash` — the specific value observed (changes when value changes)
- `confidence` — how much we believe this value (changes via convergence/decay)

### References

A reference is a pointer to a raw artifact stored outside the manifest.

```typescript
interface Reference {
  ref_id: RefId;            // REF_{turn}_{msg}
  source: string;           // what produced this artifact
  label: string;            // human description
  path: string;             // absolute filesystem path
  content_hash: string;     // SHA256 — integrity on every recall
  line_count: number;
  byte_size: number;
  related_fact_ids: FactId[];
}
```

Large tool outputs, API responses, search results — anything the model might need to reference but shouldn't carry in active context — becomes a reference.

### Summaries

A summary is a derived navigational view over facts and references.

```typescript
interface Summary {
  summary_id: SummaryId;
  type: "goal" | "session" | "conflict" | "task" | "contact" | string;
  scope: string;
  stale: boolean;           // true the moment any source fact changes state
  confidence: number;       // capped at 0.80 when conflicts exist
  key_points: string[];
  conflicts: string[];
  recommended_next_actions: string[];
  source_fact_ids: FactId[];
}
```

Summaries are **navigational**, not authoritative. When a model needs precise execution context, it drills into FACT_IDs and REF_IDs — not the summary.

---

## Identity and Arbitration

The arbitration system is the most important behavioral component. It answers: *"When we see this entity again, what do we do?"*

### Identity key derivation

Every fact type has a deterministic strategy for computing what entity this fact is about:

| Type | Identity key |
|------|-------------|
| `contact` | `contact:{name}` or `contact:{email}` |
| `task` | `task:{goal_hash}` or `task:{id}` |
| `credential` | `credential:{username}@{service}` — never includes the credential value |
| `preference` | `preference:{category}:{key}` |
| `service` | `service:{host}:{port}` or `service:{name}` |
| `knowledge` | `knowledge:{entity}:{hash(value)}` |
| Unknown | `{type}:{sha256(canonical_json(value)).slice(0,8)}` |

### The five arbitration outcomes

When `addFact()` is called, arbitration fires before any write:

| Outcome | Condition | Effect |
|---------|-----------|--------|
| `new` | No existing fact with this identity_key | Fact written as `active` |
| `converge` | Same identity_key + same fact_hash | `observation_count++`, confidence `+0.02`, no new fact |
| `supersede` | Same identity_key, incoming confidence > existing by ≥ 0.15 | Incoming → `active`, existing → `superseded` |
| `conflict` | Same identity_key, confidence delta < 0.15 | Both → `conflicted`, summaries staled |
| `reject` | Same identity_key, existing confidence superior | Incoming written as `superseded` (lineage preserved) |

**"Reject" is not silence.** The system writes the rejected fact as `superseded`. This preserves the forensic record: "we observed this, evaluated it, and the existing belief won." That record is replayable, auditable, and available for future RL training.

### Confidence threshold

`CONFIDENCE_SUPERIORITY_THRESHOLD = 0.15`

A delta below 0.15 is genuine ambiguity — the system cannot determine which belief is more reliable. Rather than guess, it puts both into `conflicted` state. The agent sees the conflict explicitly.

---

## The Context Engine Pipeline

The context engine is a **compiler**, not a serializer. It does not dump the manifest.

```
manifest → select → rank → compress → inject → reference → defer
```

### Injection strata (cognitive priority order)

```
1. Objective           — what are we trying to do (goal summary or highest-confidence task)
2. Active Facts        — ranked by type priority, then confidence
3. Conflicted Beliefs  — explicit uncertainty, always visible
4. Summaries           — navigational map, stale-flagged when stale
5. Recent Events       — last N operations from the log tail
6. Available References — REF_IDs only, never content
[drill-down contract]  — summaries are navigational; drill via FACT_IDs
```

### Fact type priority (when budget is tight)

```
constraint  100   hard limits — always first, no exceptions
task         90   active work
credential   80   auth state (slot reference only, NEVER values)
context      70   situational awareness
contact      60   people
preference   50   behavioral modifiers
knowledge    40   background
service      30   infrastructure
event        20   history — lowest priority, most ephemeral
```

### Bounds

| Parameter | Default | Effect |
|-----------|---------|--------|
| `max_chars` | 8000 | Hard cap on operational memory block |
| `max_facts` | 20 | Top-N facts by priority + confidence |
| `max_summaries` | 4 | Priority: goal > session > conflict > task |
| `max_recent_ops` | 5 | Tail of filtered operations log |
| `max_refs` | 8 | Most recently created first |

### Message layout

```
[0] system_prompt        (caller's system message, User role)
[1] operational_memory   (manifest-compiled, User role)
[2..N] active history    (sliding window, bounded)
[N+1] current_user_msg   (this turn's user message)
```

The operational memory is a **User message**, not part of the system prompt. This preserves system prompt stability and prevents behavioral drift from injected context.

---

## Sliding Window

The active history is bounded by token count and turn count. Eviction operates on whole turns — never partial turns.

```
max_tokens:               6000   token budget for active history
max_turns:                5      max completed turns kept
offload_threshold_chars:  4000   tool results larger than this get offloaded
chars_per_token:          4      conservative estimation ratio
```

### Auto-offload

When a tool result message exceeds 4000 chars, it is automatically:
1. Stored as a Reference artifact via `ManifestManager.addReference()`
2. Replaced in the active turn with a compact pointer:

```
[Large output offloaded to artifact store]
REF_ID: REF_0003_01
Size: 12431 bytes / 340 lines
Use: recall_evidence({ ref_id: "REF_0003_01", mode: "summary" }) to inspect.
```

The model can recall any artifact at will. It never needs to carry the full content.

---

## Recall Tool

Six retrieval modes, all line-numbered for stable follow-up addressing:

| Mode | Returns | Use case |
|------|---------|----------|
| `summary` | Metadata + head/tail preview | "What's in this artifact?" |
| `head` | First N lines (default 40) | "Show me the start of this output" |
| `tail` | Last N lines (default 40) | "Show me the end / conclusion" |
| `range` | Lines [start, end] inclusive | "Show me lines 120–180" |
| `full` | Full content, capped (2000 lines / 200KB) | "Give me the whole thing" |
| `search` | Line-by-line grep + ±2 context lines | "Find the rate limit error" |

Every recall is logged as a `recall_event` operation — auditable and replayable.

---

## Persistence Contract

### Atomic writes

```
1. Write to {manifest}.tmp (O_WRONLY | O_CREAT)
2. fsync()
3. rename(tmp → manifest)  ← atomic on POSIX systems
```

No partial manifest states. Either the new manifest exists or the old one does.

### Optimistic locking

```
commit(manifest, expectedRevision):
  diskRevision = readRevision()
  if diskRevision !== expectedRevision:
    return { ok: false, reason: "revision_conflict" }
  write(manifest with revision+1)
```

Two processes writing simultaneously: one wins, one gets a conflict. The conflict triggers Sync & Replan: reload the fresh state, rebuild context, re-apply mutations.

### Integrity hash

```
computeIntegrityHash(manifest):
  state = { ...manifest, integrity_hash: omit, operations: omit }
  return sha256(canonicalJson(state))
```

**Why exclude `operations`?** The operations log is forensic provenance — an append-only audit trail. The integrity hash covers **authoritative state**: what we believe, what we've stored, what we've summarized. These are different cryptographic domains. Treating them as one would prevent log entries from being appended between commits.

### Credential safety

Credential facts are **never injected as values**. The injection compiler produces:

```
[credential slot] user@service — use identity_key to reference
```

The identity_key (e.g. `credential:alice@database-prod`) is the reference handle. The actual credential value lives in the artifact store, retrievable only via explicit `recall_evidence` with clear intent.

---

## Selective Commit

Not every turn writes to disk. A commit is triggered only when knowledge changes:

**Triggers commit:**
- `addFact()` with `outcome !== "converge"` (new belief written)
- `addReference()` (artifact stored)
- `addSummary()` (derived view updated)
- `proposeRefinement()` (explicit adjudication)
- Sliding window auto-offload

**Does not trigger commit:**
- `queryFacts()` — read-only
- `addFact()` with `outcome === "converge"` — existing belief strengthened, no structural change
- `recordOperation()` for informational events
- Routine turns with no new knowledge

---

## Sync & Replan

When `commit()` returns `revision_conflict`:

```typescript
const result = await engine.completeTurn(messages, factProducing);
if (result.sync_required) {
  await engine.syncAndReplan();
  // rebuild context, re-apply pending mutations
}
```

`syncAndReplan()` reloads the manifest from disk (fresh state, new indexes), records a `manifest_loaded` operation, and returns. The caller rebuilds operational context against the fresh state.

---

## The OpenClaw Plugin Seam

```
config.yaml:
  plugins:
    slots:
      contextEngine: manifest-engine
```

That's the entire installation. The adapter:
1. Registers `"manifest-engine"` in OpenClaw's context engine registry at plugin activation
2. On `bootstrap()` — loads or creates a `ManifestManager` for the session
3. On `assemble()` — compiles the manifest into the message array via `buildMessages()`
4. On `afterTurn()` — calls `completeTurn()`, commits if knowledge changed
5. On `compact()` — delegates to OpenClaw's built-in runtime compaction

The adapter is structurally typed against OpenClaw's interface. No hard dependency on OpenClaw's internal types — only the structural contract required by the plugin system.

---

## Index Design

Indexes are never persisted. They are rebuilt from the manifest on every `load()` and `reload()`.

| Index | Purpose | Why |
|-------|---------|-----|
| `facts_by_identity_key` | Arbitration lookup on every `addFact()` | Hottest path |
| `facts_by_type` | `queryFacts({ type })` — most common filter | Hot |
| `summary_deps` | O(1) staleness propagation on state transition | Hot |
| `active_facts` | Injection compiler, conflict queries | Hot |
| `conflicted_facts` | Injection compiler, conflict queries | Hot |
| `facts_by_source` | Provenance queries | Cool |
| `refs_by_source` | Diagnostic / recall | Cool |

After every `buildIndexes()`, `validateIndexes()` verifies the invariant:
```
active_facts ≡ { f | f.state = "active" }
conflicted_facts ≡ { f | f.state = "conflicted" }
```

This is a bug detector, not a production gate. It should never fire.

---

## Lineage and Governance

Every fact carries its full provenance chain:

```
FACT_A (supersedes FACT_B)
  derived_from: [FACT_C, FACT_D]
  merged_from: [FACT_E, FACT_F]
  supporting_sources: ["user", "tool:verify", "ci-system"]
```

This makes the manifest replayable and auditable. Given only the manifest and the operations log, you can reconstruct the exact sequence of beliefs the agent held at any point in time.

`proposeRefinement()` forces a supersession when the operator has explicit intent — it overrides normal arbitration confidence battles. The override is logged with `forced: true` so the governance trail distinguishes natural epistemic evolution from deliberate operator adjudication.

---

## What This Is Not

**Not a memory system.** Memory systems summarize conversation history. This system stores typed, arbitrated, provenance-tracked beliefs about the world. Summaries exist to help navigation, not to replace state.

**Not a RAG pipeline.** RAG retrieves documents by similarity. This retrieves beliefs by identity and references by ID. Retrieval is deterministic, not probabilistic.

**Not a knowledge graph.** No ontology, no SPARQL, no inference engine. Just facts with types, confidence, and lineage — enough to support agent cognition without a PhD to operate.

**Not a database.** No transactions, no SQL, no concurrent reads. The manifest is designed for single-agent, single-session use with cross-process coordination via optimistic locking.
