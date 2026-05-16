# Memory Topology

Status: draft canonical design

## Core Topology

```text
STM  -> active cognition window
MTM  -> active session continuity
LTM  -> compressed operational history
```

## Architectural Invariant

```text
No memory enters active context unless admitted by deterministic governance.
```

## STM

Purpose:
- active prompt continuity only
- unresolved references and current operational focus

Rules:
- rolling last 6 epochs
- epoch-bounded, not token-bounded
- epochs preserved losslessly
- no partial epoch truncation
- overflow handled only through whole-epoch eviction

Operational note:
- STM is bounded by chronology, not token austerity.

## MTM

Purpose:
- full-fidelity continuity for the active session
- append-only event/interaction ledger

Rules:
- append-only
- full fidelity
- never bulk-injected into active context
- retrieved selectively through deterministic lookup

## LTM

Purpose:
- compressed episodic history across closed sessions

Storage:
- `ltm.jsonl` (episode records)
- `ltm_toc.json` (deterministic table of contents)

Rules:
- session-end compression cycle only
- lineage/provenance preserved
- deterministic retrieval boundaries

## Semantic Sidecar

Purpose:
- cross-session associative discovery only

Rules:
- optional
- disabled by default
- candidate generator only
- rebuildable/disposable
- never authoritative

## Session Lifecycle

1. Session starts with empty/primed STM + active MTM ledger.
2. Each epoch appends to MTM and updates rolling STM window.
3. On session close, compression produces LTM episode artifact(s) + TOC updates.
4. New sessions do not auto-hydrate full MTM/LTM into prompt; they retrieve by admissible need.

## Replay Expectations

- STM epoch eviction must be deterministic.
- MTM must replay losslessly.
- LTM compression outputs must be reproducible for same source session + policy version.
- Any context inclusion event must be auditable.

## Consolidation Boundaries

- Consolidation happens at session boundary by default.
- Mid-session consolidation is allowed only if explicitly policy-gated and audited.
