/**
 * Persistence layer for the manifest engine.
 *
 * Core guarantees:
 * 1. No partial manifest states — atomic write via tmp + rename.
 * 2. Optimistic locking — commit() fails hard on revision mismatch. No silent merge.
 * 3. Integrity hashing — SHA256 of canonical JSON detects corruption before load.
 * 4. Load validates before exposing state — fail loud, never silently repair.
 * 5. Indexes are always rebuilt from manifest on load, never persisted.
 */

import { createHash, timingSafeEqual } from "node:crypto";
import {
  mkdir,
  open,
  readFile,
  rename,
  stat,
  unlink,
  writeFile,
} from "node:fs/promises";
import { dirname, join } from "node:path";

import type {
  Fact,
  FactId,
  Manifest,
  ManifestIndexes,
  Operation,
  OperationId,
  Reference,
  RefId,
  Summary,
  SummaryId,
} from "../types/manifest.js";
import { buildIndexes, validateIndexes } from "./indexing.js";
import { canonicalJson } from "./serialization.js";

// ---------------------------------------------------------------------------
// Result types
// ---------------------------------------------------------------------------

export type LoadResult =
  | { ok: true; manifest: Manifest; indexes: ManifestIndexes }
  | { ok: false; reason: LoadFailureReason; detail: string };

export type LoadFailureReason =
  | "not_found"
  | "parse_error"
  | "schema_mismatch"
  | "integrity_failure"
  | "lineage_error"
  | "orphan_ref"
  | "io_error";

export type CommitResult =
  | { ok: true; revision: number; integrity_hash: string }
  | { ok: false; reason: CommitFailureReason; detail: string };

export type CommitFailureReason =
  | "revision_conflict"
  | "validation_error"
  | "io_error";

export type ValidationResult =
  | { valid: true }
  | { valid: false; errors: string[] };

// ---------------------------------------------------------------------------
// ManifestPersistence
// ---------------------------------------------------------------------------

export class ManifestPersistence {
  constructor(
    /** Absolute path to manifest.json */
    private readonly manifestPath: string,
    /** Absolute path to artifacts directory */
    private readonly artifactsDir: string,
    /** Actor string stamped on load/commit operations */
    private readonly actor: string = "manifest-engine",
  ) {}

  // -------------------------------------------------------------------------
  // load
  // -------------------------------------------------------------------------

  async load(): Promise<LoadResult> {
    let raw: string;
    try {
      raw = await readFile(this.manifestPath, "utf8");
    } catch (err: unknown) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === "ENOENT") {
        return { ok: false, reason: "not_found", detail: this.manifestPath };
      }
      return {
        ok: false,
        reason: "io_error",
        detail: `readFile failed: ${String(err)}`,
      };
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      return {
        ok: false,
        reason: "parse_error",
        detail: `Invalid JSON: ${String(err)}`,
      };
    }

    // Schema version gate — fail before anything else.
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      (parsed as Record<string, unknown>)["schema_version"] !== "1.0"
    ) {
      return {
        ok: false,
        reason: "schema_mismatch",
        detail: `Expected schema_version "1.0", got: ${String((parsed as Record<string, unknown>)?.["schema_version"])}`,
      };
    }

    const manifest = parsed as Manifest;

    // Integrity check.
    const expected = await this.computeIntegrityHash(manifest);
    if (!timingSafeEqualStrings(expected, manifest.integrity_hash)) {
      return {
        ok: false,
        reason: "integrity_failure",
        detail: `Integrity hash mismatch. Expected ${expected}, stored ${manifest.integrity_hash}`,
      };
    }

    // Lineage consistency — every referenced id must exist.
    const lineageResult = validateLineage(manifest);
    if (!lineageResult.valid) {
      return {
        ok: false,
        reason: "lineage_error",
        detail: lineageResult.errors.join("; "),
      };
    }

    // Orphan ref check — artifact files should exist on disk.
    const orphanResult = await this.validateRefs(manifest);
    if (!orphanResult.valid) {
      return {
        ok: false,
        reason: "orphan_ref",
        detail: orphanResult.errors.join("; "),
      };
    }

    const indexes = buildIndexes(manifest);

    // Debug guard: index/manifest divergence should never happen, but fail loudly if it does.
    const indexCheck = validateIndexes(manifest, indexes);
    if (!indexCheck.ok) {
      return {
        ok: false,
        reason: "lineage_error",
        detail: `Index validation failed (bug in buildIndexes): ${indexCheck.errors.join("; ")}`,
      };
    }

    return { ok: true, manifest, indexes };
  }

  // -------------------------------------------------------------------------
  // loadOrCreate — returns a fresh manifest when none exists
  // -------------------------------------------------------------------------

  async loadOrCreate(sessionId: string): Promise<LoadResult> {
    const result = await this.load();
    if (result.ok || result.reason !== "not_found") return result;

    const now = new Date().toISOString();
    const fresh: Omit<Manifest, "integrity_hash"> = {
      schema_version: "1.0",
      revision: 0,
      created_at: now,
      updated_at: now,
      facts: {},
      references: {},
      summaries: {},
      operations: [
        makeOperation("manifest_loaded", this.actor, 0, 0, {
          session_id: sessionId,
          event: "created",
        }),
      ],
      last_fact_index: 0,
    };

    const hash = await this.computeIntegrityHash(fresh as Manifest);
    const manifest: Manifest = { ...fresh, integrity_hash: hash } as Manifest;

    await this.commitRaw(manifest);
    const indexes = buildIndexes(manifest);
    return { ok: true, manifest, indexes };
  }

  // -------------------------------------------------------------------------
  // commit — optimistic locking, atomic write
  // -------------------------------------------------------------------------

  async commit(
    manifest: Manifest,
    expectedRevision: number,
  ): Promise<CommitResult> {
    // Read current revision from disk before writing.
    let diskRevision: number;
    try {
      diskRevision = await this.readRevision();
    } catch (err) {
      return {
        ok: false,
        reason: "io_error",
        detail: `Could not read current revision: ${String(err)}`,
      };
    }

    if (diskRevision !== expectedRevision) {
      return {
        ok: false,
        reason: "revision_conflict",
        detail: `Expected revision ${expectedRevision}, disk has ${diskRevision}. Reload and replan.`,
      };
    }

    const nextRevision = diskRevision + 1;
    const now = new Date().toISOString();

    const commitOp = makeOperation(
      "manifest_committed",
      this.actor,
      diskRevision,
      nextRevision,
      { ts: now },
    );

    const next: Manifest = {
      ...manifest,
      revision: nextRevision,
      updated_at: now,
      operations: [...manifest.operations, commitOp],
      integrity_hash: "", // will be replaced below
    } as unknown as Manifest;

    const hash = await this.computeIntegrityHash(next);
    const final: Manifest = { ...next, integrity_hash: hash };

    try {
      await this.commitRaw(final);
    } catch (err) {
      return {
        ok: false,
        reason: "io_error",
        detail: `Atomic write failed: ${String(err)}`,
      };
    }

    return { ok: true, revision: nextRevision, integrity_hash: hash };
  }

  // -------------------------------------------------------------------------
  // computeIntegrityHash — SHA256 of canonical JSON, excluding integrity_hash
  // and operations (operations log is a derivative; state is what we hash)
  // -------------------------------------------------------------------------

  async computeIntegrityHash(manifest: Manifest | Omit<Manifest, "integrity_hash">): Promise<string> {
    const { integrity_hash: _omit, operations: _ops, ...state } = manifest as Manifest & { integrity_hash?: string };
    return sha256(canonicalJson(state));
  }

  // -------------------------------------------------------------------------
  // validate — synchronous surface for external callers
  // -------------------------------------------------------------------------

  async validate(manifest: Manifest): Promise<ValidationResult> {
    const errors: string[] = [];

    if (manifest.schema_version !== "1.0") {
      errors.push(`Unknown schema_version: ${manifest.schema_version}`);
    }

    const hash = await this.computeIntegrityHash(manifest);
    if (!timingSafeEqualStrings(hash, manifest.integrity_hash)) {
      errors.push("Integrity hash mismatch");
    }

    const lineage = validateLineage(manifest);
    if (!lineage.valid) errors.push(...lineage.errors);

    const refs = await this.validateRefs(manifest);
    if (!refs.valid) errors.push(...refs.errors);

    return errors.length === 0 ? { valid: true } : { valid: false, errors };
  }

  // -------------------------------------------------------------------------
  // storeArtifact — write raw content to artifacts dir, return path + hash
  // -------------------------------------------------------------------------

  async storeArtifact(
    refId: string,
    content: string | Buffer,
  ): Promise<{ path: string; content_hash: string; line_count: number; byte_size: number }> {
    await mkdir(this.artifactsDir, { recursive: true });
    const buf = typeof content === "string" ? Buffer.from(content, "utf8") : content;
    const hash = sha256(buf.toString("utf8"));
    const filePath = join(this.artifactsDir, `${refId}.artifact`);
    await writeFile(filePath, buf);
    const text = buf.toString("utf8");
    const parts = text.split("\n");
    const lineCount = parts.length > 0 && parts[parts.length - 1] === "" ? parts.length - 1 : parts.length;
    return {
      path: filePath,
      content_hash: hash,
      line_count: lineCount,
      byte_size: buf.byteLength,
    };
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  private async commitRaw(manifest: Manifest): Promise<void> {
    await mkdir(dirname(this.manifestPath), { recursive: true });
    const tmp = `${this.manifestPath}.tmp`;
    const serialized = JSON.stringify(manifest, null, 2);

    // Write to temp file, fsync, then atomic rename.
    const fd = await open(tmp, "w");
    try {
      await fd.writeFile(serialized, "utf8");
      await fd.sync();
    } finally {
      await fd.close();
    }

    await rename(tmp, this.manifestPath);
  }

  private async readRevision(): Promise<number> {
    let raw: string;
    try {
      raw = await readFile(this.manifestPath, "utf8");
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      // If file doesn't exist yet, revision is -1 (will match expectedRevision=-1 on first commit).
      if (code === "ENOENT") return -1;
      throw err;
    }
    const parsed = JSON.parse(raw) as { revision?: number };
    if (typeof parsed.revision !== "number") {
      throw new Error("Manifest on disk has no numeric revision field");
    }
    return parsed.revision;
  }

  private async validateRefs(manifest: Manifest): Promise<ValidationResult> {
    const errors: string[] = [];
    for (const ref of Object.values(manifest.references) as Reference[]) {
      try {
        await stat(ref.path);
      } catch {
        errors.push(`Orphan ref ${ref.ref_id}: artifact not found at ${ref.path}`);
      }
    }
    return errors.length === 0 ? { valid: true } : { valid: false, errors };
  }
}

// ---------------------------------------------------------------------------
// Lineage validation — pure, synchronous
// ---------------------------------------------------------------------------

function validateLineage(manifest: Manifest): ValidationResult {
  const errors: string[] = [];
  const factIds = new Set(Object.keys(manifest.facts));
  const refIds = new Set(Object.keys(manifest.references));

  for (const fact of Object.values(manifest.facts) as Fact[]) {
    for (const parentId of fact.derived_from) {
      if (!factIds.has(parentId)) {
        errors.push(`Fact ${fact.fact_id}: derived_from refs unknown ${parentId}`);
      }
    }
    if (fact.supersedes !== undefined && !factIds.has(fact.supersedes)) {
      errors.push(`Fact ${fact.fact_id}: supersedes refs unknown ${fact.supersedes}`);
    }
    for (const mergedId of fact.merged_from ?? []) {
      if (!factIds.has(mergedId)) {
        errors.push(`Fact ${fact.fact_id}: merged_from refs unknown ${mergedId}`);
      }
    }
  }

  for (const summary of Object.values(manifest.summaries) as Summary[]) {
    for (const fid of summary.source_fact_ids) {
      if (!factIds.has(fid)) {
        errors.push(`Summary ${summary.summary_id}: source_fact_ids refs unknown ${fid}`);
      }
    }
    for (const rid of summary.source_ref_ids) {
      if (!refIds.has(rid)) {
        errors.push(`Summary ${summary.summary_id}: source_ref_ids refs unknown ${rid}`);
      }
    }
  }

  return errors.length === 0 ? { valid: true } : { valid: false, errors };
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function sha256(input: string): string {
  return createHash("sha256").update(input, "utf8").digest("hex");
}

function timingSafeEqualStrings(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  return timingSafeEqual(Buffer.from(a, "utf8"), Buffer.from(b, "utf8"));
}

let opCounter = 0;

function makeOperation(
  type: Operation["type"],
  actor: string,
  revisionBefore: number,
  revisionAfter: number,
  metadata: Record<string, unknown>,
): Operation {
  const op_id = `OP_${Date.now()}_${(++opCounter).toString().padStart(4, "0")}` as OperationId;
  return {
    op_id,
    type,
    actor,
    timestamp: new Date().toISOString(),
    revision_before: revisionBefore,
    revision_after: revisionAfter,
    metadata,
  };
}
