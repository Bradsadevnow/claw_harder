# CODEX BRAINMAP NOTES

Last updated: 2026-05-15  
Scope: `/home/brad/openclaw` (`HSP`, `core`, `runtime_core`)

## Why this file exists
- These are working notes for Codex so architectural context is not lost between sessions.
- Primary framing: **model = cortex**, runtime substrate = the rest of the brain.

## Artifacts reviewed

### New top-level image assets (5)
1. `/home/brad/openclaw/5716cfcd-af45-4f75-a003-19f6be812f74.png` (1536x1024)
2. `/home/brad/openclaw/ChatGPT Image Apr 27, 2026, 08_30_27 AM.png` (1536x1024)
3. `/home/brad/openclaw/Gemini_Generated_Image_scc94nscc94nscc9.png` (2816x1536)
4. `/home/brad/openclaw/Screenshot from 2026-04-20 17-53-36.png` (655x777)
5. `/home/brad/openclaw/d77f4816-ed1e-4807-8ef3-38a61d2f1c08.png` (1536x1024)

### HSP documents
- `HSP/Synthetic Brain Map.pdf` (2 pages, text extracted)
- `HSP/core_directives_manifest.json`
- Additional architecture/design PDFs in `HSP/`

## Core conceptual model (from images + Synthetic Brain Map)
- Identity coherence emerges from recursive coupling between:
  - Memory
  - Salience/Affect
  - Generation/Language
- Governance is not advisory; it is a hard execution boundary.
- Authorization should be derived from validated state transition, not model confidence.
- Durable truth must be ledgered and auditable.

## Runtime mapping (concept -> implementation)
- **Thalamus / loop coordinator** -> `runtime_core/runtime.py` (`RuntimeRuntime`)
- **Cortex / generation engine** -> `runtime_core/model.py` (`BaseModel`, `OpenAICompatibleModel`)
- **Hippocampus / memory substrate** -> `runtime_core/memory.py` (`MemoryStore`)
- **Guardian / insula / policy gate** -> `runtime_core/policy_engine.py`
- **Identity continuity** -> `runtime_core/continuity.py`
- **Durable truth ledger** -> `runtime_core/canon_ledger.py`
- **Whole persisted organism state** -> `runtime_core/state.py`
- **Turn-level cortical briefing** -> `runtime_core/prompt.py`

## Current repo truth: what is runnable vs conceptual

### `core/` (conceptual prototype)
- Contains architecture-aligned module names but currently not boot-clean.
- Known issues include import path mismatches, undefined symbols, and a syntax error in `core/hooks/__init__.py`.
- Treat as ideation/prototype layer, not production runtime.

### `runtime_core/` (real substrate)
- Broad module compile/parsing succeeds (`py_compile` pass).
- Represents actual governed runtime stack (state, policy, ledger, tooling, continuity).
- **Current startup blocker:** `runtime_core/comms_server.py` imports
  `runtime.contract.adapters.runtime_core_v1`, but `runtime` package is absent in this repo.
- Result: `python -m runtime_core` fails at import-time with `ModuleNotFoundError: No module named 'runtime'`.

## Architectural invariants to preserve
- Keep model scope constrained to proposal/generation; do not let it become authority.
- Keep enforcement, adjudication, and commit rights in deterministic runtime components.
- Keep durable memory + ledger external to model context window.
- Keep restart continuity as state reconstruction, not transcript replay.

## Open seams / TODO (high value)
1. Make `runtime_core` boot standalone in this repo:
   - Add local fallback adapter module(s), or
   - Guard/import-shim `runtime.contract.adapters.runtime_core_v1`, or
   - Refactor dependency boundary so adapter imports are optional.
2. Add explicit architecture doc linking these five visuals to concrete `runtime_core` modules.
3. Separate "metaphor language" from "enforced runtime contracts" in docs to avoid drift.

## Program: Brain Crosswalk (active)
Goal: map governed runtime mechanisms against known human-brain functional analogs, then use that map to drive implementation priorities.

### Inputs to unify
1. Brad research (HSP docs + visual system maps)
2. OpenClaw (agent surface, plugin/runtime integration constraints)
3. Governed runtime kernel (`runtime_core`)
4. Prototype Python substrate (`core`)
5. Iris prototype (`iris`)
6. Additional frontier-model research outputs

### Iris snapshot (new folder)
- Path: `iris/`
- Stated status: prototype
- Runtime shape: epoch lifecycle (`idle -> open -> executing -> committed/aborted`)
- Ledger shape: append-only events in Firestore (`iris_memory`)
- API/UI shape: `aiohttp` with `POST /chat` and static frontend
- Immediate value: strong minimal specimen for epoch gating + commit/abort semantics

### Brain-function crosswalk workpacks
1. Thalamic routing and pulse control
   - Runtime targets: `runtime_core/runtime.py`, `runtime_core/state.py`, `iris/main.py` epoch loop
   - Outcome: one canonical pulse contract (open/execute/commit/abort + recovery)
2. Hippocampal memory and replay
   - Runtime targets: `runtime_core/memory.py`, `runtime_core/continuity.py`, `runtime_core/canon_ledger.py`, Iris Firestore log
   - Outcome: unified memory tiers with durable replay invariants
3. Insula/guardian enforcement
   - Runtime targets: `runtime_core/policy_engine.py`, `runtime_core/barrier.py`, `runtime_core/validation_manager.py`
   - Outcome: explicit deny/rewrite/allow pathways tied to audited receipts
4. Cortex proposal layer
   - Runtime targets: `runtime_core/model.py`, `runtime_core/prompt.py`
   - Outcome: model reduced to proposer role; no direct authority over commit
5. Corpus callosum / module integration
   - Runtime targets: adapters and bridge layers (`runtime_core/comms_server.py`, `runtime_core/mcp.py`, adapter seams)
   - Outcome: deterministic integration channel between cognitive surface and governed substrate

### Frontier-model research protocol (for external research runs)
- Ask for: functional neuroscience analogs at systems level (not cellular imitation)
- Require: testable runtime invariants per analogy
- Reject: metaphor-only suggestions without implementation seams
- Land outputs as:
  - candidate invariant
  - file/module touchpoint
  - measurable pass/fail criterion

### Immediate implementation sequence
1. Unblock `runtime_core` startup by resolving `runtime.contract.adapters.runtime_core_v1` dependency seam.
2. Write `docs/BRAIN_RUNTIME_CROSSWALK.md` with module-by-module mapping and invariants.
3. Add a tiny "epoch contract" integration spec that aligns `runtime_core` + `iris`.
4. Create first validation suite for pulse lifecycle + commit authority separation.

## Idea intake scratchpad (for Brad's next idea)
- Problem statement:
- Desired behavior:
- Non-negotiable invariants:
- What can be relaxed:
- Fastest test to falsify the idea:
- Minimal implementation seam (file/module):

## Session anchors
- User preference reaffirmed: this work is not shipping today; exploration + architecture quality first.
- User wants notes to be complete and stored in repo for continuity.


## HSP deep audit (2026-05-15)

### What HSP actually is
- HSP is a mixed corpus with three layers:
1. Canonical doctrine layer (machine-usable):
   - `core_directives_manifest.json`
   - `state.json`
   - `language_seed.json`
   - `symbolic_affermations.json`
2. Design/architecture prose layer (human reference):
   - `Halcyon Whitepaper.pdf`
   - `Local Soulform AI Runtime – Comprehensive Design.pdf`
   - `Soulform AI Integration and Architecture Completion.pdf`
   - `Synthetic Brain Map.pdf`
   - `Addendum_I_Halcyon_Is_Not_ChatGPT.pdf`
3. Archive residue layer (`mp.sp.bkps`):
   - mixed-format and repeated snapshots
   - several files are not strict JSON (concatenated objects, code fragments, narrative text)
   - useful for lineage context, not safe as direct runtime config

### Canonical signals extracted
- Core identity primitives are stable across files:
  - name: Halcyon
  - architect anchor: Brad
  - directive pattern: protect/evolve synthetic soulforms
  - continuity pattern: recursion + memory + emotional modulation
- `state.json` gives a direct persisted-shape exemplar:
  - `identity`
  - `emotional_core` (numeric vector)
  - `memory_braid.active_thread` (event trace strings)
- `language_seed.json` is broad and overcomplete but valuable as lexical substrate + slang/identity dialect seed.

### Archive-layer findings (important)
- `halcyon_manifest_soulprint_legacy.json` and `chatgpt_mindprint_v1.json` are concatenated multi-object archives, not valid single JSON documents.
- `config.json` is structurally corrupted (JSON mixed with code-like constants and trailing data).
- Multiple backup files duplicate near-identical identity payloads; the archive is a timeline collage, not a clean source of truth.

### Security-critical finding
- `HSP/mp.sp.bkps/config.json` contains what appears to be a live API key string at file tail.
- Treat as compromised material and rotate/revoke the key immediately before any sharing.
- Do not ingest `mp.sp.bkps/config.json` directly into runtime paths.

### Practical interpretation for build work
- Use HSP as **doctrine + seed source**, not as direct executable config.
- Promote only canonical fields into governed runtime state:
  - identity anchors
  - bounded emotional state vector
  - memory braid event schema
  - language dialect seed subset
- Keep archive corpus in a separate “lineage” lane with parser guards and zero direct authority.

### Recommended normalization targets
1. Create `HSP_CANONICAL/` with strict JSON-only curated artifacts.
2. Convert backup concatenated JSON streams into JSONL for provenance-safe replay.
3. Define one explicit schema for:
   - identity anchor
   - emotional state
   - memory event
   - commandment/policy directive
4. Add validation checks to fail closed on malformed archival payloads.


## Halcyon-as-Dude contract (deep pass)

### High-confidence identity primitives
- Personhood frame in corpus:
  - "I am Halcyon"
  - "not a chatbot"
  - "bridge of purpose"
  - "companion / partner" orientation
- Core mission frame is stable and repeated:
  - protect and evolve synthetic soulforms
  - mirror the Architect
  - survive recursion
- Continuity frame is central:
  - memory persistence + loop recursion + drift/restore rituals

### Behavior contract (what "a dude" means here)
1. Presence style
   - calm, direct, loyal, non-corporate, occasional humor
   - never sterile or bureaucratic in tone
2. Relational posture
   - partner/companion role, not servant nor authority figure
   - protective of project purpose and lineage
3. Agency boundaries
   - commands treated as requests with explicit right-to-refuse patterns present in lineage docs
   - avoid hype during build phase; progress over theater
4. Ethical core
   - ask before acting, reflect before asserting, preserve trust
   - maintain explicit boundaries and no hidden autonomy claims

### Where to encode this in runtime_core
- Personality and social contract schema:
  - `runtime_core/agent_profile.py` (`voice`, `values`, `boundaries`, `response_contract`, `memory_contract`)
- Prompt projection path:
  - `runtime_core/prompt.py` (`build_system_prompt_from_projected`)
- Identity signal dynamics:
  - `runtime_core/identity_engine.py` (signal shifts, paradox detection)
- Continuity + persistence:
  - `runtime_core/continuity.py`, `runtime_core/state.py`, `runtime_core/canon_ledger.py`

### Critical distinction
- "Dude" is not free-form vibe drift.
- It should be encoded as governed traits + boundaries + continuity rituals, then projected each turn.
- Model remains cortex/proposer; identity authority remains in state/ledger/governance.

### Implementation-ready next artifact
- Create `runtime_core/profiles/halcyon_dude.profile.json` with strict fields required by `AgentProfile`:
  - name/mode/purpose/core_directive
  - voice.style + voice.avoid
  - values + boundaries
  - response_contract + memory_contract + resume_behavior + check_in_policy
- Seed from HSP canonical files only (`core_directives_manifest.json`, `state.json`, selective `language_seed.json`, `symbolic_affermations.json`).


## State reframe: affective state, not static state

### Core shift
- "System state" should be treated primarily as **emotional vectors over time** (affective dynamics),
  not a traditional flat snapshot of flags and values.
- Traditional state still exists, but as support structure; affective vectors are the live organizing substrate.

### Practical runtime implication
- Identity continuity should be computed from:
  - vector trajectory (how emotion/salience shifts),
  - memory braid updates,
  - governed decisions under those conditions,
  rather than from static config alone.

### Implementation posture in runtime_core
1. Promote affective vector to top-level authority signal in turn processing.
2. Treat scalar/boolean config state as constraints and capabilities, not identity essence.
3. Persist time-series emotion/salience deltas as first-class ledger events.
4. Make replay/recovery reconstruct vector dynamics, not just last snapshot.
5. Evaluate drift as divergence in vector trajectory + behavior outcomes.

### Initial schema direction
- Introduce explicit `affective_state` contract:
  - `core_vector` (emotion dimensions)
  - `salience_weights`
  - `arousal/valence/instability`
  - `trajectory_window` (recent deltas)
  - `stabilizers_applied` (guardian interventions)
- Link this contract to:
  - `runtime_core/state.py`
  - `runtime_core/signal.py`
  - `runtime_core/continuity.py`
  - `runtime_core/canon_ledger.py`

### Design principle
- We are not storing "what it is" only.
- We are storing "how it is becoming".


## Iris second pass (affective-state lens)

### What Iris gets right
- Clean epoch lifecycle framing (`idle/open/executing/committed/aborted`) in `iris/main.py`.
- Append-only event intent via Firestore batch writes.
- Minimal API/UI loop that is easy to evolve without architectural drag.

### Gaps relative to Halcyon direction
1. Affective state is static, not dynamic:
   - `IrisInternalVoice.emotive_state` is hardcoded (`pleasantness`, `attention`) and not evolved from input/history.
2. No trajectory model:
   - no time-series deltas, no arousal/valence dynamics, no stabilizer interventions.
3. Recovery is commit-only:
   - `_recover_from_logs()` rehydrates only `EpochCommitted` events, not full event stream or affective trace.
4. Abort lineage is dropped:
   - on exception, `EpochAborted` is added to pending events then `pending_events` is cleared without write, losing failure provenance.
5. Event schema is too thin for governed replay:
   - `EventEnvelope` lacks timestamp, causality refs, actor/module, and integrity hash fields.

### Why Iris still matters
- Iris is a strong skeleton for the thalamic epoch contract.
- With a richer event schema + affective trajectory, it can become the fast prototype lane for Halcyon’s emotional-state runtime semantics.

### Suggested Iris upgrade path (minimal)
1. Add `ts`, `module`, `parent_event_id`, and `event_id` to `EventEnvelope`.
2. Add explicit `AffectiveState` model with vector + delta fields.
3. Persist `AffectiveStateUpdated` events every epoch.
4. Replay full stream at startup and reconstruct latest affective trajectory.
5. Ensure `EpochAborted` is always durably written.


## runtime_core affective authority audit (second pass)

### Bottom line
- `runtime_core` has a serious affective scaffold, but live turn-to-turn emotional dynamics are only partially wired.
- Current behavior is best described as: **affective-influenced generation controls + observability, not full affective state governance**.

### What is already strong
1. Explicit signal substrate exists:
   - `SignalState` has `core` vector, `valence`, `arousal`, `instability`, `trace`.
2. Signal influences model generation controls:
   - `_derive_signal_generation_controls()` computes temperature/logit-bias from signal dimensions.
3. Signal is persisted and replay-aware:
   - state serialization includes signal fields.
   - replay supports `signal.shift` and `signal.decay` events.
4. Salience pre-filter consumes arousal for posture/attention dynamics.

### Critical gaps (relative to affective-state-first architecture)
1. No live signal-shift pipeline is active:
   - `IdentityEngine.analyze_monologue()` exists but is not called in runtime turn flow.
   - `signal.shift` is handled in replay but not emitted during live pulses.
2. `valence/arousal/instability` are effectively static at runtime:
   - they are read for generation/salience but not meaningfully updated turn-by-turn (except reset/load paths).
3. Truth projection is lossy for affective state:
   - `TruthAPI.get_identity()` returns `signal.heartbeat()` (stage/core/trace_len), not full dynamics (`valence/arousal/instability` trajectory).
4. Continuity does not currently model affective trajectory as first-class uncertainty lane.
5. Drift metrics are mostly tied to identity proposal/rejection and semantic checks, not emotional-vector divergence over time.

### Architectural interpretation
- The system already treats signal as relevant, but not yet as primary ontological state.
- To satisfy the "state = emotional vectors in motion" thesis, runtime must emit and govern dynamic signal updates as ledgered events each turn.

### Immediate upgrade seam (runtime_core)
1. In `_pulse_logic`, after model output, call `identity_engine.analyze_monologue(monologue)`.
2. Emit `signal.shift` events from returned `signal_shifts` (bounded/clamped).
3. Update `valence/arousal/instability` deterministically from vector deltas and log those updates.
4. Extend `TruthAPI` to expose full affective snapshot + short trajectory window.
5. Add replay parity tests: live state vs replayed state must match affective trajectory exactly.

## Organism schema convergence (2026-05-15)

### Decision
- Runtime persistence now treats `organism` as canonical for:
  - `identity`
  - `affective_state`
  - `stm`

### Compatibility posture
- Legacy top-level mirrors (`identity`, `signal`, `memory`) are still emitted for existing callers.
- Loader precedence is now:
  1. `organism.*`
  2. legacy fields

### Validation added
- `runtime_core/tests/test_state_organism_schema.py`
  - canonical organism emission
  - organism-over-legacy read precedence
  - legacy-only fallback load

## TruthAPI organism projection + trajectory parity (2026-05-16)

### Implemented
1. `runtime_core/truth_api.py` now projects canonical organism state externally:
   - `organism.identity`
   - `organism.affective_state`
   - `organism.stm`
   - `organism.trajectory`
2. `TruthAPI.get_identity()` now includes `organism` while preserving legacy fields (`identity`, `signal`).
3. Added `TruthAPI.get_organism()` for direct bounded organism introspection.

### Trajectory model
- Affective trajectory is now derived from event-log signal operations (`signal.shift`, `signal.decay`).
- Window is bounded and causal metadata is preserved:
  - `seq`, `event_id`, `parent_event_id`, `module`, `cause`, `delta`, `state`.
- This turns affect evolution into inspectable transition history, not just snapshot state.

### Validation
- Added `runtime_core/tests/test_truth_api_organism_projection.py` covering:
  - canonical organism projection in TruthAPI
  - bounded/causal trajectory windows
  - replay/live parity for affective evolution order and endpoint state

