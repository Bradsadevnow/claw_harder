# Phase 6: ContextEngine — The Compiler Layer

**Status:** Complete  
**Files:**
- `src/context/sliding-window.ts`
- `src/context/injection.ts`
- `src/context/context-engine.ts`

## Overview

The context engine is not a serializer. It does not dump the manifest into the prompt. It is a **compiler, scheduler, and attention router**.

Pipeline: **select → rank → compress → inject → reference → defer**

Three components compose the runtime:
- **SlidingWindow** — bounded active history, auto-offloads large tool results
- **InjectionCompiler** — manifest → structured prompt strata
- **ContextEngine** — orchestrator; owns the full turn lifecycle

## Injection Contract

```
[0] system_prompt       (caller's system message, injected as User role)
[1] operational_memory  (User role — stable, rebuilt every turn)
[2..N] active history
[N+1] current_user_message
```

The operational memory block is a **User Message**, not part of the system prompt. This preserves system prompt stability and prevents policy dilution.

System prompt is also injected as a User message. This is intentional — it preserves low-entropy positioning. The model cannot distinguish system-as-user from user, which is fine; the invariant is that system instructions lead.

## Semantic Strata (injection order = cognitive priority)

| Stratum | What it carries | Why this priority |
|---------|----------------|-------------------|
| 1. Objective | Single sentence: what we're trying to do | Agent orientation on every turn |
| 2. Active Facts | Ranked, bounded, typed beliefs | What is known and authoritative |
| 3. Conflicted Beliefs | Uncertainty, visible not hidden | Prevents confident action on contested state |
| 4. Summaries | Navigational map, stale-flagged | Where things stand, not what things are |
| 5. Recent Events | Last N ops from the log tail | What just happened |
| 6. Available References | REF_IDs only, no content | What can be recalled |

**Drill-down contract** (always last): summaries are navigational. Precise execution requires drilling into FACT_IDs via `recall_evidence`.

## Fact Type Priority (budget allocation)

When context budget is tight, facts are injected in type priority order:

```
constraint  100  — hard limits, always first
task         90  — active work, agent's primary focus
credential   80  — auth state (slot only, NEVER values)
context      70  — situational awareness
contact      60  — people involved
preference   50  — behavioral modifiers
knowledge    40  — background
service      30  — infrastructure
event        20  — history, most ephemeral
```

Credentials are rendered as `[credential slot] user@service — use identity_key to reference`. Values are never injected. This is not a style choice — it's a security invariant.

## SlidingWindow

**Default config:**
```ts
max_tokens: 6000        // total token budget for active history
max_turns: 5            // max completed turns to keep
offload_threshold_chars: 4000  // tool results larger than this get offloaded
chars_per_token: 4      // conservative char/token ratio for estimation
```

**Offload behavior:** Tool result messages exceeding `offload_threshold_chars` are replaced with a compact pointer:
```
[Large output offloaded to artifact store]
REF_ID: REF_0003_01
Size: 12431 bytes / 340 lines
Use: recall_evidence({ ref_id: "REF_0003_01", mode: "summary" }) to inspect.
```

The original content is stored as an artifact via `ManifestManager.addReference()`. The agent can recall it surgically rather than carrying it in active history.

**Eviction:** Operates on whole turns, never partial. Oldest turn evicted first when either `max_turns` or `max_tokens` is exceeded.

## Selective Commit

Commits only when knowledge actually changes. Not every turn.

**Triggers commit:**
- `addFact()` with `outcome !== "converge"` (new belief written)
- `addReference()` (artifact stored)
- `addSummary()` (derived view updated)
- `proposeRefinement()` (explicit adjudication)
- Auto-offloads from sliding window

**Does NOT trigger commit:**
- `queryFacts()` — read-only
- `addFact()` with `outcome === "converge"` — existing belief strengthened, no structural change
- `recordOperation()` for informational events
- Routine turn completion with no new knowledge

## Sync & Replan

When `commit()` returns `revision_conflict` (another process wrote the manifest between our read and write):

```ts
async syncAndReplan(): Promise<SyncReplanResult> {
  const result = await this.manager.reload();  // fresh disk read, index rebuild
  if (!result.ok) return { ok: false, detail: result.detail };
  this.manager.recordOperation("manifest_loaded", {
    event: "sync_replan",
    reason: "revision_conflict",
  });
  return { ok: true };
}
```

After `syncAndReplan()`, the caller rebuilds operational context and re-applies mutations against the fresh state.

## Turn Lifecycle

```ts
// Before model call
const { operational_memory, history, revision } = engine.buildContext();
const messages = engine.buildMessages(systemPrompt, userMessage);

// During turn — as facts are discovered
engine.addFact({ type: "task", value: { goal: "..." }, source: "model", confidence: 0.85 });
engine.addSummary({ type: "session", scope: "current", key_points: [...] });

// After model response + tool results
const result = await engine.completeTurn(messages, factProducing);
if (result.sync_required) {
  await engine.syncAndReplan();
}
```

## InjectionCompiler Limits

```ts
max_chars: 8000     // hard cap — truncates with notice, never silently drops
max_facts: 20       // top-N by type priority + confidence
max_summaries: 4    // goal > session > conflict > task > contact
max_recent_ops: 5   // tail of filtered operations log
include_refs: true  // up to 8, most recently created first
```

When the body exceeds `max_chars`, the compiler appends:
```
[Operational context truncated at 8000 chars. Use recall_evidence for full state.]
```

This is a last resort — the budget is designed so this rarely triggers. If it does trigger consistently, add more summaries to compress facts into navigation.

## Objective Extraction

Priority order:
1. First key_point of an active `goal`-type summary
2. Highest-confidence active `task` fact (`title` → `goal` → `id` field)
3. Nothing — objective section omitted entirely

## What Phase 7 Will Address

Phase 6 revealed two things worth hardening:

1. **Schema versioning** — manifest currently has `schema_version: "1.0"` but there's no migration hook if the schema evolves. Phase 7 should define the serialization contract formally.
2. **Recall tool** — the injection compiler emits REF_IDs and instructs the agent to call `recall_evidence`. That tool is not yet built. It's next.

## LM Studio Validation Target (Phase D)

**Model:** `openai/gpt-oss-20b` at `http://192.168.1.129:1234`

**Test:** Objective Handoff — build a `ContextEngine` with a pre-populated manifest (objective + task facts + a summary). Simulate process restart. On the new `ContextEngine`, call `buildMessages()` and verify:
- The operational memory block correctly surfaces the objective
- Active facts are ranked by type priority
- No history (fresh window) — model must act purely from manifest state
- Model continues task without asking "what were we doing?"

See `docs/phases/phase-7-serialization.md` (schema versioning) and the recall tool implementation next.
