# OPENCLAW II: CLAW HARDER

**A typed, versioned, append-only state substrate for OpenClaw agents.**

Replaces naive transcript replay with durable facts, bounded context, surgical recall, and explicit conflict handling.

---

```yaml
# openclaw config — one line to activate
plugins:
  slots:
    contextEngine: manifest-engine
```

---

## The problem

OpenClaw's default context engine replays the full conversation transcript on every model call. This degrades in three ways:

1. **Token cost grows linearly** — every turn costs more than the last
2. **Restarts cause amnesia** — process death = complete context loss
3. **No epistemic structure** — facts, guesses, conflicts, and tool outputs are indistinguishable text

## The solution

Replace the transcript with a **manifest** — a typed, versioned, integrity-hashed document that stores what the agent actually *believes*, not what it was told.

On every model call, the manifest is compiled into a structured operational memory block:

```
# Current Operational Memory

**Objective:** Deploy payment-v2 to production (60% complete)

## Active Facts

- [FACT_A1B2] constraint (100%): rule: "No deploys 13:00–19:00 UTC"
- [FACT_C3D4] task (95%): goal: "Deploy payment-v2", status: "in-progress", progress: "60%"
- [FACT_E5F6] context (90%): environment: "production", cluster: "us-east-1a"

## Conflicted Beliefs ⚠️

- [STATE: CONFLICTED] [FACT_G7H8] knowledge (78%): entity: "prod-db", port: 5432
- [STATE: CONFLICTED] [FACT_I9J0] knowledge (75%): entity: "prod-db", port: 5433

_These beliefs require resolution before acting on them._

## Summaries

**[SUM_K1L2]** goal:current (91%)
  - Deploy payment-v2 to production (60% complete)
  - Blocked by peak-hour policy until 19:00 UTC
  → Run canary health checks. Deploy after 19:00 UTC.

## Available References

- `REF_0003` — Service mesh health check (847 lines, 42KB, src: tool:probe)

_Use summaries for navigation. Drill into FACT_IDs via recall_evidence for precision._
```

---

## Four properties that matter

### 1. Restart continuity

The manifest survives process death. When an agent restarts, it loads the manifest and has complete operational state — same objective, same facts, same confidence levels — without replaying a single history message.

```typescript
// Session 1: agent does work
engine.addFact({ type: "task", value: { goal: "...", status: "..." }, confidence: 0.95, source: "user" });
await manager.commit();  // persisted to manifest.json

// Session 2: cold start, zero history
const engine2 = await ManifestManager.load("./manifest.json", "./artifacts");
const messages = engine2.buildMessages(systemPrompt, currentMessage);
// → Operational memory contains full state. No replay.
```

### 2. recall_evidence

Large tool outputs are offloaded from context and stored as artifacts. The model sees a REF_ID and a size hint. It retrieves exactly what it needs.

```typescript
// Model sees: REF_0021 — Production API response (340 lines, 28KB)
// Model calls:
const result = await recall.recall({ ref_id: "REF_0021", mode: "search", query: "DEGRADED" });
// → Returns only lines containing "DEGRADED" + ±2 context lines
// → Not 340 lines of JSON dumped into the prompt
```

Six retrieval modes: `summary` | `head` | `tail` | `range` | `full` | `search`

### 3. Conflict handling

When two observations of the same entity have similar confidence, neither wins. Both enter `conflicted` state. The model sees the conflict explicitly — uncertainty is a first-class state, not a hidden failure.

```
## Conflicted Beliefs ⚠️
- [STATE: CONFLICTED] prod-db: port 5432 (wiki, 78%)
- [STATE: CONFLICTED] prod-db: port 5433 (runbook, 75%)
_These beliefs require resolution before acting on them._
```

### 4. Bounded context

The sliding window keeps active history within a token budget. Old turns are evicted. Large tool results are auto-offloaded. Context stays bounded regardless of session length.

```
Turn 1: window=1 turns, 240 tokens
Turn 3: window=3 turns, 1840 tokens  → offloaded REF_0003_02 (tool output > 4000 chars)
Turn 6: window=5 turns, 4200 tokens  → evicted turn 1
```

---

## Installation

```bash
npm install openclaw-2-claw-harder
```

Set in OpenClaw config:

```yaml
plugins:
  slots:
    contextEngine: manifest-engine
```

---

## Standalone usage (without OpenClaw)

```typescript
import { ManifestManager, ContextEngine, RecallTool } from "openclaw-2-claw-harder";

const manager = await ManifestManager.load("./manifest.json", "./artifacts", "my-agent");
const engine = new ContextEngine(manager, { auto_commit: true });
const recall = new RecallTool(manager);

engine.addFact({
  type: "constraint",
  value: { rule: "Zero downtime. Cutover window: 02:00–04:00 UTC." },
  confidence: 1.0,
  source: "ops-policy",
});

const messages = engine.buildMessages(systemPrompt, userMessage);
const result = await engine.completeTurn(turnMessages, factProducing);
if (result.sync_required) await engine.syncAndReplan();
```

---

## Arbitration

Five outcomes when the same entity is observed again:

| Outcome | Condition | Effect |
|---------|-----------|--------|
| `new` | Unknown entity | Written as `active` |
| `converge` | Same entity, same value | `observation_count++`, confidence `+0.02` |
| `supersede` | Incoming wins by ≥ 0.15 confidence | Incoming → `active`, existing → `superseded` |
| `conflict` | Confidence delta < 0.15 | Both → `conflicted` |
| `reject` | Existing wins | Incoming written as `superseded` for lineage |

`reject` is not silence — the observation is recorded. The forensic trail shows: "observed, evaluated, existing belief won."

---

## Validation suite

```bash
npm run test:validation
```

| Scenario | Proves |
|----------|--------|
| A: Warm Start | Manifest state visible on cold start without history replay |
| B: Persistence | Facts survive process death with full fidelity |
| C: Conflict | Ambiguity preserved as conflict state, not forced resolution |
| D: Objective Handoff | Purpose survives restarts, stale summaries flagged |
| E: Evidence-Linked Action | Recall is surgical, bounded, audited on every access |

---

## Demo

```bash
npm run demo
```

Runs all four behavioral properties with real data in an isolated temp directory.

---

## Docs

- [Architecture](docs/ARCHITECTURE.md) — full technical reference
- [Why Manifests, Not Memory?](docs/WHY_MANIFESTS.md) — the design argument
- [Phase docs](docs/phases/) — implementation history, per phase

---

## Constraints

- Node.js 22+, strict TypeScript ESM
- OpenClaw `>=2026.5.0` (optional peer dep — works standalone)
- No external runtime dependencies

---

MIT — Brad Bates
