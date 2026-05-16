# claw_harder

This is an open fork of OpenClaw and related runtime experiments by Brad Bates.  
You are free to download, use, and modify this work under the MIT license.

## Open Source Scope

- This repo is intentionally open and usable.
- The long-term `runtime_swarm_stack` direction is planned separately and is not fully published here.
- License: see [LICENSE](/home/brad/claw_harder/LICENSE).

## What This Repo Contains

- [openclaw-main](/home/brad/claw_harder/openclaw-main): OpenClaw base code mirror/fork surface.
- [openclaw_2_claw_harder](/home/brad/claw_harder/openclaw_2_claw_harder): OpenClaw II manifest-engine plugin work.
- [runtime_core](/home/brad/claw_harder/runtime_core): governed runtime substrate prototype.
- [docs](/home/brad/claw_harder/docs): project docs.
- [CODEX_BRAINMAP_NOTES.md](/home/brad/claw_harder/CODEX_BRAINMAP_NOTES.md): architecture and research continuity notes.
- [WORKSTREAM_SNAPSHOT_2026-05-16.md](/home/brad/claw_harder/WORKSTREAM_SNAPSHOT_2026-05-16.md): current workstream snapshot.

## Work Completed

- Established the OpenClaw II manifest-engine direction and documentation.
- Captured architecture notes for the brain crosswalk (HSP, Iris, runtime_core, OpenClaw integration).
- Framed organism-centric state direction (`identity`, `affective_state`, `stm`) as a core architecture path.
- Captured memory-system topology decisions for STM/MTM/LTM plus admissibility boundaries.

## Work In Progress

- Formalizing memory contracts before deeper runtime behavior wiring.
- Cleaning runtime seams so startup and replay are deterministic.
- Converging affective-state continuity and governance interfaces.

## Ongoing Plan

1. Memory contracts first:
   - STM as rolling epoch window
   - MTM as session ledger
   - LTM as compressed artifacts + deterministic TOC
   - semantic recall as nomination only, governance as admission authority
2. Runtime seam cleanup:
   - remove brittle adapter dependencies
   - enforce explicit state ownership and replay-safe initialization
3. Live modulation wiring:
   - governed `signal.shift` flow
   - trajectory replay parity tests
   - Truth API organism projection hardening

## Quick Start (OpenClaw II Plugin)

```bash
cd /home/brad/claw_harder/openclaw_2_claw_harder
npm run build
openclaw plugins install --link /home/brad/claw_harder/openclaw_2_claw_harder
openclaw config set plugins.slots.contextEngine "manifest-engine"
openclaw plugins list
openclaw config get plugins.slots.contextEngine
openclaw
```
