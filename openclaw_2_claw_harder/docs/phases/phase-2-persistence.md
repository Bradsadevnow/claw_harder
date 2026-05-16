# Phase 2: Persistence

**Status:** Complete  
**Files:** `src/manifest/persistence.ts`, `src/manifest/serialization.ts`, `src/manifest/indexing.ts` (stub)

## Overview

Persistence is sacred infrastructure. If this layer is sloppy, the entire governed-agents vision collapses under race conditions and phantom state. Every invariant here is a hard guarantee, not a best-effort.

## Core Invariants

### 1. No Partial Manifest States

The write path is always:

```
manifest.json.tmp  →  fsync(fd)  →  rename(tmp, manifest.json)
```

`rename(2)` on POSIX is atomic with respect to readers — a reader either sees the old complete file or the new complete file, never a half-written state. Node.js `fs.promises.rename` uses `MoveFileExW(MOVEFILE_REPLACE_EXISTING)` on Windows, which provides the same guarantee.

### 2. Optimistic Locking

`commit(manifest, expectedRevision)` reads the current revision off disk before writing. If `diskRevision !== expectedRevision`, it returns:

```ts
{ ok: false, reason: "revision_conflict", detail: "Expected 5, disk has 7. Reload and replan." }
```

No auto-merge. No silent overwrite. The caller must reload the manifest, rebuild their operational context, and replan. This is how concurrent agents (e.g. a cron job and an interactive session) experience contention explicitly rather than silently corrupting shared state.

### 3. Integrity Hashing

The integrity hash covers `facts + references + summaries + revision + schema_version + last_fact_index` — the authoritative state. It explicitly excludes:

- `integrity_hash` itself (circular)
- `operations` (the operations log is a record of how we got here, not the state itself)

Canonical JSON serialization (`src/manifest/serialization.ts`) sorts all object keys lexicographically at every depth, ensuring the hash is deterministic regardless of insertion order. Uses `timingSafeEqual` for comparison to prevent timing attacks on hash comparison.

### 4. Fail Loud, Never Silently Repair

The load path validates in sequence:

1. Schema version gate — wrong version = immediate failure
2. Integrity hash — mismatch = quarantine, do not proceed
3. Lineage consistency — every `derived_from`, `supersedes`, `merged_from` must resolve to a known fact
4. Orphan ref check — every `Reference` must have its artifact file present on disk

If any check fails, `load()` returns `{ ok: false, reason, detail }`. The caller decides whether to quarantine, alert, or initialize fresh. We never silently repair corrupt state — governed systems die the moment they auto-patch corruption.

### 5. Indexes Rebuilt on Load, Never Persisted

`buildIndexes(manifest)` derives all lookup structures from the authoritative manifest. They live only in memory. This means:

- The manifest file is the single source of truth
- No possibility of index/manifest divergence
- Manifests are portable across machines with no migration overhead

## Key Design Decisions

**`storeArtifact()`** writes large content to `artifacts/{refId}.artifact` on disk and returns a `Reference`-compatible payload (path, content_hash, line_count, byte_size). This is how large tool outputs stay out of the manifest while remaining fully retrievable via `recall_evidence`.

**`loadOrCreate()`** is the standard entry point for agents — returns an existing manifest or initializes a fresh one with revision 0 and a `manifest_loaded` operation in the log.

**Operation IDs** use `OP_{timestamp}_{counter}` — monotonic, human-readable, never UUID to keep logs scannable.

## LM Studio Validation Target (Phase B)

**Model:** `openai/gpt-oss-20b` at `http://192.168.1.129:1234`

**Test:** Persistence Lifecycle — verify that a manifest written in one process is loaded correctly in a fresh process (no shared memory), integrity check passes, and the agent resumes from manifest state without re-deriving facts from history.

See `docs/phases/phase-3-identity.md` for next steps.
