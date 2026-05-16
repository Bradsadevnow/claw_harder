/**
 * ManifestManager — the constitutional layer.
 *
 * This is the ONLY legal mutation boundary for manifest state.
 * No direct object mutation. Every change goes through typed APIs that enforce:
 *   - Arbitration (no silent overwrites)
 *   - Lineage (facts always have provenance)
 *   - Operation log (every mutation is recorded)
 *   - Index consistency (indexes always reflect manifest state)
 *   - Staleness propagation (summaries stale when their facts change)
 *
 * Usage:
 *   const manager = await ManifestManager.load(manifestPath, artifactsDir);
 *   const result = manager.addFact({ type: "preference", value: {...}, confidence: 0.9, source: "user" });
 *   await manager.commit();
 */

import { randomUUID } from "node:crypto";

import type {
  AddFactInput,
  AddReferenceInput,
  AddSummaryInput,
  ArbitrationDecision,
  Fact,
  FactId,
  FactObservations,
  FactState,
  Manifest,
  ManifestIndexes,
  Operation,
  OperationId,
  OperationType,
  QueryFactsFilter,
  Reference,
  RefId,
  Summary,
  SummaryId,
  SummaryState,
} from "../types/manifest.js";
import { ArbitrationEngine, collectStaleSummaries } from "./arbitration.js";
import { deriveFactHash, deriveIdentityKey } from "./identity.js";
import { buildIndexes } from "./indexing.js";
import {
  type CommitResult,
  type LoadResult,
  ManifestPersistence,
} from "./persistence.js";

// ---------------------------------------------------------------------------
// Working state — mutable internally, Manifest-compatible shape
// ---------------------------------------------------------------------------

/** Non-readonly working copy of Manifest. Matches Manifest structurally. */
type WorkingManifest = {
  schema_version: "1.0";
  revision: number;
  integrity_hash: string;
  created_at: string;
  updated_at: string;
  facts: Record<string, Fact>;
  references: Record<string, Reference>;
  summaries: Record<string, Summary>;
  operations: Operation[];
  last_fact_index: number;
};

// ---------------------------------------------------------------------------
// Result types
// ---------------------------------------------------------------------------

export type AddFactResult =
  | { ok: true; outcome: ArbitrationDecision["outcome"]; fact_id: FactId; decision: ArbitrationDecision }
  | { ok: false; reason: string };

export type AddReferenceResult =
  | { ok: true; ref_id: RefId }
  | { ok: false; reason: string };

export type AddSummaryResult =
  | { ok: true; summary_id: SummaryId; superseded?: SummaryId }
  | { ok: false; reason: string };

export type ProposeRefinementResult =
  | { ok: true; new_fact_id: FactId; superseded_fact_id: FactId; decision: ArbitrationDecision }
  | { ok: false; reason: string };

export type ProposeConsolidationResult =
  | { ok: true; consolidated_fact_id: FactId; superseded_count: number }
  | { ok: false; reason: string };

// QueryFactsFilter is defined in types/manifest.ts — re-exported for callers.
export type { QueryFactsFilter } from "../types/manifest.js";

// ---------------------------------------------------------------------------
// ManifestManager
// ---------------------------------------------------------------------------

export class ManifestManager {
  private working: WorkingManifest;
  private indexes: ManifestIndexes;
  private dirty = false;
  private expectedRevision: number;
  private readonly arbitration: ArbitrationEngine;

  private constructor(
    private readonly persistence: ManifestPersistence,
    manifest: Manifest,
    indexes: ManifestIndexes,
    private readonly actor: string,
  ) {
    this.working = structuredClone(manifest) as unknown as WorkingManifest;
    this.indexes = indexes;
    this.expectedRevision = manifest.revision;
    this.arbitration = new ArbitrationEngine();
  }

  // -------------------------------------------------------------------------
  // Factory methods
  // -------------------------------------------------------------------------

  static async load(
    manifestPath: string,
    artifactsDir: string,
    actor = "manifest-engine",
  ): Promise<ManifestManager> {
    const persistence = new ManifestPersistence(manifestPath, artifactsDir, actor);
    const result = await persistence.loadOrCreate(actor);
    if (!result.ok) {
      throw new Error(`ManifestManager.load failed: [${result.reason}] ${result.detail}`);
    }
    return new ManifestManager(persistence, result.manifest, result.indexes, actor);
  }

  static fromLoadResult(
    persistence: ManifestPersistence,
    result: Extract<LoadResult, { ok: true }>,
    actor = "manifest-engine",
  ): ManifestManager {
    return new ManifestManager(persistence, result.manifest, result.indexes, actor);
  }

  // -------------------------------------------------------------------------
  // addFact
  // -------------------------------------------------------------------------

  addFact(input: AddFactInput): AddFactResult {
    const identity_key = deriveIdentityKey(input.type, input.value);
    const fact_hash = deriveFactHash(input.type, input.value);

    const candidate = {
      fact_hash,
      identity_key,
      confidence: input.confidence,
      source: input.source,
    };

    const decision = this.arbitration.arbitrate(
      candidate,
      this.working as unknown as Manifest,
      this.indexes,
    );

    const now = new Date().toISOString();

    switch (decision.outcome) {
      case "new": {
        const fact = this.buildFact(input, identity_key, fact_hash, "active", undefined, now);
        this.insertFact(fact);
        this.recordOp("fact_added", { fact_id: fact.fact_id, identity_key, decision });
        return { ok: true, outcome: "new", fact_id: fact.fact_id as FactId, decision };
      }

      case "converge": {
        // Same value — strengthen existing, no new fact.
        const existing = this.working.facts[decision.existing_id] as Fact;
        const updates = this.arbitration.applyConvergence(existing, input.source, now);
        const updated: Fact = { ...existing, ...updates };
        this.working.facts[existing.fact_id] = updated;
        this.recordOp("fact_observation_updated", {
          fact_id: existing.fact_id,
          identity_key,
          observation_count: updated.observations.observation_count,
          new_confidence: updated.confidence,
          decision,
        });
        return { ok: true, outcome: "converge", fact_id: existing.fact_id as FactId, decision };
      }

      case "supersede": {
        // New wins — add new as active, transition existing to superseded.
        const fact = this.buildFact(input, identity_key, fact_hash, "active", undefined, now);
        this.insertFact(fact);
        this.transitionFact(decision.existing_id, "superseded", now);
        this.propagateStaleness(decision.existing_id);
        this.recordOp("fact_superseded", {
          new_fact_id: fact.fact_id,
          superseded_id: decision.existing_id,
          identity_key,
          decision,
        });
        return { ok: true, outcome: "supersede", fact_id: fact.fact_id as FactId, decision };
      }

      case "reject": {
        // Existing wins — add new as superseded for lineage, update existing observations.
        const fact = this.buildFact(input, identity_key, fact_hash, "superseded", undefined, now);
        this.insertFact(fact);
        const existing = this.working.facts[decision.existing_id] as Fact;
        const updates = this.arbitration.applyConvergence(existing, input.source, now);
        this.working.facts[existing.fact_id] = { ...existing, ...updates };
        this.recordOp("fact_added", {
          fact_id: fact.fact_id,
          state: "superseded",
          reason: "rejected_by_existing",
          identity_key,
          decision,
        });
        return { ok: true, outcome: "reject", fact_id: fact.fact_id as FactId, decision };
      }

      case "conflict": {
        // Neither wins — add new as conflicted, transition existing to conflicted.
        const fact = this.buildFact(input, identity_key, fact_hash, "conflicted", undefined, now);
        this.insertFact(fact);
        this.transitionFact(decision.existing_id, "conflicted", now);
        this.propagateStaleness(decision.existing_id);
        this.indexes.conflicted_facts.add(fact.fact_id as FactId);
        this.recordOp("fact_conflicted", {
          new_fact_id: fact.fact_id,
          conflicted_id: decision.existing_id,
          identity_key,
          decision,
        });
        return { ok: true, outcome: "conflict", fact_id: fact.fact_id as FactId, decision };
      }
    }
  }

  // -------------------------------------------------------------------------
  // addReference
  // -------------------------------------------------------------------------

  async addReference(input: AddReferenceInput): Promise<AddReferenceResult> {
    const ref_id = `REF_${shortId()}` as RefId;

    let stored: Awaited<ReturnType<ManifestPersistence["storeArtifact"]>>;
    try {
      stored = await this.persistence.storeArtifact(ref_id, input.content);
    } catch (err) {
      return { ok: false, reason: `Artifact storage failed: ${String(err)}` };
    }

    const now = new Date().toISOString();
    const ref: Reference = {
      ref_id,
      source: input.source,
      label: input.label,
      path: stored.path,
      content_hash: stored.content_hash,
      line_count: stored.line_count,
      byte_size: stored.byte_size,
      created_at: now,
      related_fact_ids: input.related_fact_ids ?? [],
    };

    this.working.references[ref_id] = ref;

    // Update refs_by_source index.
    if (!this.indexes.refs_by_source.has(input.source)) {
      this.indexes.refs_by_source.set(input.source, new Set());
    }
    this.indexes.refs_by_source.get(input.source)!.add(ref_id);

    this.recordOp("ref_added", { ref_id, source: input.source, label: input.label });
    this.dirty = true;
    return { ok: true, ref_id };
  }

  // -------------------------------------------------------------------------
  // addSummary
  // -------------------------------------------------------------------------

  addSummary(input: AddSummaryInput): AddSummaryResult {
    const summary_id = `SUM_${shortId()}` as SummaryId;
    const now = new Date().toISOString();

    // Supersede any existing active summary with same type + scope.
    let supersededId: SummaryId | undefined;
    for (const existing of Object.values(this.working.summaries) as Summary[]) {
      if (
        existing.type === input.type &&
        existing.scope === input.scope &&
        existing.state === "active"
      ) {
        this.working.summaries[existing.summary_id] = {
          ...existing,
          state: "superseded" as SummaryState,
        };
        supersededId = existing.summary_id as SummaryId;
        // Remove old dependency mappings.
        for (const fid of existing.source_fact_ids) {
          this.indexes.summary_deps.get(fid as FactId)?.delete(existing.summary_id as SummaryId);
        }
        this.recordOp("summary_superseded", { superseded_id: existing.summary_id, replacement_id: summary_id });
        break; // Only one active per type+scope is the invariant.
      }
    }

    // Clamp confidence below 0.8 if there are unresolved conflicts.
    const effectiveConfidence =
      input.conflicts && input.conflicts.length > 0
        ? Math.min(input.confidence, 0.8)
        : input.confidence;

    const summary: Summary = {
      summary_id,
      type: input.type,
      scope: input.scope,
      state: "active",
      stale: false,
      confidence: effectiveConfidence,
      generated_at: now,
      source_fact_ids: input.source_fact_ids,
      source_ref_ids: input.source_ref_ids ?? [],
      key_points: input.key_points,
      conflicts: input.conflicts ?? [],
      recommended_next_actions: input.recommended_next_actions ?? [],
    };

    this.working.summaries[summary_id] = summary;

    // Register dependency mappings — O(1) staleness propagation later.
    for (const fid of input.source_fact_ids) {
      if (!this.indexes.summary_deps.has(fid)) {
        this.indexes.summary_deps.set(fid, new Set());
      }
      this.indexes.summary_deps.get(fid)!.add(summary_id);
    }

    this.recordOp("summary_added", { summary_id, type: input.type, scope: input.scope });
    this.dirty = true;
    return supersededId !== undefined
      ? { ok: true, summary_id, superseded: supersededId }
      : { ok: true, summary_id };
  }

  // -------------------------------------------------------------------------
  // queryFacts
  // -------------------------------------------------------------------------

  queryFacts(filter: QueryFactsFilter = {}): Fact[] {
    const states = filter.state
      ? Array.isArray(filter.state)
        ? filter.state
        : [filter.state]
      : ["active", "conflicted"] satisfies FactState[]; // Default: live facts only.

    let candidates: FactId[];

    if (filter.type) {
      candidates = Array.from(this.indexes.facts_by_type.get(filter.type) ?? []);
    } else if (filter.identity_key) {
      candidates = Array.from(
        this.indexes.facts_by_identity_key.get(filter.identity_key) ?? [],
      );
    } else {
      // Full scan — only when no index covers the filter.
      candidates = Object.keys(this.working.facts) as FactId[];
    }

    const results: Fact[] = [];
    for (const fid of candidates) {
      const fact = this.working.facts[fid] as Fact | undefined;
      if (!fact) continue;
      if (!states.includes(fact.state)) continue;
      if (filter.source && fact.source !== filter.source) continue;
      if (filter.min_confidence !== undefined && fact.confidence < filter.min_confidence) continue;
      if (filter.identity_key && fact.identity_key !== filter.identity_key) continue;
      results.push(fact);
    }

    // Sort by confidence desc, then discovery_index asc.
    results.sort((a, b) => {
      if (b.confidence !== a.confidence) return b.confidence - a.confidence;
      return a.discovery_index - b.discovery_index;
    });

    return filter.limit ? results.slice(0, filter.limit) : results;
  }

  // -------------------------------------------------------------------------
  // proposeRefinement
  // -------------------------------------------------------------------------

  proposeRefinement(
    targetFactId: FactId,
    refinement: Omit<AddFactInput, "derived_from"> & { confidence: number },
  ): ProposeRefinementResult {
    const target = this.working.facts[targetFactId] as Fact | undefined;
    if (!target) {
      return { ok: false, reason: `Fact ${targetFactId} not found.` };
    }
    if (target.state === "invalidated") {
      return { ok: false, reason: `Cannot refine an invalidated fact (${targetFactId}).` };
    }

    // Capture before mutation — needed for conflict sweep below.
    const identityKey = target.identity_key;

    // Force this through addFact with derived_from set and high enough confidence to win.
    const result = this.addFact({
      ...refinement,
      derived_from: [targetFactId],
    });

    if (!result.ok) {
      return { ok: false, reason: result.reason };
    }

    // If arbitration didn't supersede (e.g. we lost confidence battle), force it.
    // A propose_refinement is an explicit intent — it overrides normal arbitration.
    if (result.outcome !== "supersede" && result.outcome !== "new") {
      // Force the new fact active, force the target superseded.
      const newFact = this.working.facts[result.fact_id] as Fact;
      this.working.facts[result.fact_id] = { ...newFact, state: "active" };
      if (this.working.facts[targetFactId]) {
        this.transitionFact(targetFactId, "superseded", new Date().toISOString());
        this.propagateStaleness(targetFactId);
      }
      this.recordOp("fact_superseded", {
        new_fact_id: result.fact_id,
        superseded_id: targetFactId,
        via: "propose_refinement",
        forced: true,
      });
    }

    // A conflict creates two conflicted facts with the same identity_key. Sweep all
    // remaining conflicted facts on this entity — proposeRefinement is an explicit resolution.
    const now = new Date().toISOString();
    for (const fid of Array.from(this.indexes.conflicted_facts)) {
      if (fid === result.fact_id) continue;
      const f = this.working.facts[fid] as Fact | undefined;
      if (f && f.identity_key === identityKey) {
        this.transitionFact(fid, "superseded", now);
        this.propagateStaleness(fid);
        this.recordOp("fact_superseded", {
          superseded_id: fid,
          via: "propose_refinement",
          reason: "conflict_resolution",
        });
      }
    }

    return {
      ok: true,
      new_fact_id: result.fact_id,
      superseded_fact_id: targetFactId,
      decision: result.decision,
    };
  }

  // -------------------------------------------------------------------------
  // proposeConsolidation
  // -------------------------------------------------------------------------

  proposeConsolidation(
    sourceFactIds: FactId[],
    consolidated: AddFactInput,
  ): ProposeRefinementResult {
    if (sourceFactIds.length < 2) {
      return { ok: false, reason: "Consolidation requires at least 2 source facts." };
    }

    for (const fid of sourceFactIds) {
      if (!this.working.facts[fid]) {
        return { ok: false, reason: `Source fact ${fid} not found.` };
      }
    }

    // Collect all source_ref_ids from the facts being merged.
    const inheritedRefIds: RefId[] = [];
    const inheritedSources: string[] = [];
    for (const fid of sourceFactIds) {
      const f = this.working.facts[fid] as Fact;
      inheritedSources.push(f.source, ...f.supporting_sources);
    }

    const now = new Date().toISOString();
    const identity_key = deriveIdentityKey(consolidated.type, consolidated.value);
    const fact_hash = deriveFactHash(consolidated.type, consolidated.value);
    const fact_id = `FACT_${shortId()}` as FactId;
    const discovery_index = ++this.working.last_fact_index;

    const consolidatedFact: Fact = {
      fact_id,
      fact_hash,
      type: consolidated.type,
      value: consolidated.value,
      identity_key,
      confidence: consolidated.confidence,
      source: consolidated.source ?? this.actor,
      derived_from: sourceFactIds,
      merged_from: sourceFactIds,
      state: "active",
      created_at: now,
      observations: {
        first_seen: now,
        last_seen: now,
        observation_count: 1,
      },
      supporting_sources: Array.from(new Set([consolidated.source ?? this.actor, ...inheritedSources])),
      discovery_index,
    };

    this.working.facts[fact_id] = consolidatedFact;
    this.updateIndexesForFact(consolidatedFact);

    // Supersede all source facts.
    for (const fid of sourceFactIds) {
      this.transitionFact(fid, "superseded", now);
      this.propagateStaleness(fid);
    }

    this.recordOp("fact_consolidated", {
      consolidated_fact_id: fact_id,
      source_fact_ids: sourceFactIds,
      identity_key,
    });
    this.dirty = true;

    return {
      ok: true,
      new_fact_id: fact_id,
      superseded_fact_id: sourceFactIds[0]!, // primary source for callers
      decision: { outcome: "new", reason: "Consolidation creates a new authoritative fact." },
    };
  }

  // -------------------------------------------------------------------------
  // supersedeFact / invalidateFact
  // -------------------------------------------------------------------------

  supersedeFact(factId: FactId, reason: string): { ok: boolean; detail?: string } {
    const fact = this.working.facts[factId] as Fact | undefined;
    if (!fact) return { ok: false, detail: `Fact ${factId} not found.` };
    if (fact.state === "invalidated") return { ok: false, detail: "Already invalidated." };
    const now = new Date().toISOString();
    this.transitionFact(factId, "superseded", now);
    this.propagateStaleness(factId);
    this.recordOp("fact_superseded", { fact_id: factId, reason, manual: true });
    return { ok: true };
  }

  invalidateFact(factId: FactId, reason: string): { ok: boolean; detail?: string } {
    const fact = this.working.facts[factId] as Fact | undefined;
    if (!fact) return { ok: false, detail: `Fact ${factId} not found.` };
    const now = new Date().toISOString();
    this.transitionFact(factId, "invalidated", now);
    this.propagateStaleness(factId);
    this.indexes.active_facts.delete(factId);
    this.indexes.conflicted_facts.delete(factId);
    this.recordOp("fact_invalidated", { fact_id: factId, reason, invalidated: true });
    return { ok: true };
  }

  // -------------------------------------------------------------------------
  // recordOperation — for callers logging non-mutation events
  // -------------------------------------------------------------------------

  recordOperation(
    type: OperationType,
    metadata: Record<string, unknown>,
  ): OperationId {
    return this.recordOp(type, metadata);
  }

  // -------------------------------------------------------------------------
  // commit / reload
  // -------------------------------------------------------------------------

  async commit(): Promise<CommitResult> {
    if (!this.dirty) {
      return { ok: true, revision: this.working.revision, integrity_hash: this.working.integrity_hash };
    }

    const result = await this.persistence.commit(
      this.working as unknown as Manifest,
      this.expectedRevision,
    );

    if (result.ok) {
      this.working.revision = result.revision;
      this.working.integrity_hash = result.integrity_hash;
      this.expectedRevision = result.revision;
      this.dirty = false;
    }

    return result;
  }

  async reload(): Promise<{ ok: boolean; detail?: string }> {
    const result = await this.persistence.load();
    if (!result.ok) {
      return { ok: false, detail: `[${result.reason}] ${result.detail}` };
    }
    this.working = structuredClone(result.manifest) as unknown as WorkingManifest;
    this.indexes = result.indexes;
    this.expectedRevision = result.manifest.revision;
    this.dirty = false;
    this.recordOp("manifest_loaded", { event: "reload" });
    return { ok: true };
  }

  // -------------------------------------------------------------------------
  // Read accessors
  // -------------------------------------------------------------------------

  get manifest(): Manifest {
    return this.working as unknown as Manifest;
  }

  get isDirty(): boolean {
    return this.dirty;
  }

  getRevision(): number {
    return this.working.revision;
  }

  getFact(factId: FactId): Fact | undefined {
    return this.working.facts[factId] as Fact | undefined;
  }

  getReference(refId: RefId): Reference | undefined {
    return this.working.references[refId] as Reference | undefined;
  }

  getSummary(summaryId: SummaryId): Summary | undefined {
    return this.working.summaries[summaryId] as Summary | undefined;
  }

  getActiveSummaries(type?: string): Summary[] {
    return (Object.values(this.working.summaries) as Summary[]).filter(
      (s) => s.state === "active" && (!type || s.type === type),
    );
  }

  getConflictedFacts(): Fact[] {
    return Array.from(this.indexes.conflicted_facts)
      .map((fid) => this.working.facts[fid] as Fact | undefined)
      .filter((f): f is Fact => f !== undefined);
  }

  // -------------------------------------------------------------------------
  // Private mutation helpers
  // -------------------------------------------------------------------------

  private buildFact(
    input: AddFactInput,
    identity_key: string,
    fact_hash: string,
    state: FactState,
    supersedes: FactId | undefined,
    now: string,
  ): Fact {
    const fact_id = `FACT_${shortId()}` as FactId;
    const discovery_index = ++this.working.last_fact_index;
    return {
      fact_id,
      fact_hash,
      type: input.type,
      value: input.value,
      identity_key,
      confidence: input.confidence,
      source: input.source,
      derived_from: input.derived_from ?? [],
      ...(supersedes !== undefined ? { supersedes } : {}),
      state,
      created_at: now,
      observations: {
        first_seen: now,
        last_seen: now,
        observation_count: 1,
      },
      supporting_sources: [input.source, ...(input.supporting_sources ?? [])],
      discovery_index,
    };
  }

  private insertFact(fact: Fact): void {
    this.working.facts[fact.fact_id] = fact;
    this.updateIndexesForFact(fact);
    this.dirty = true;
  }

  private updateIndexesForFact(fact: Fact): void {
    const fid = fact.fact_id as FactId;

    if (!this.indexes.facts_by_type.has(fact.type)) {
      this.indexes.facts_by_type.set(fact.type, new Set());
    }
    this.indexes.facts_by_type.get(fact.type)!.add(fid);

    if (!this.indexes.facts_by_identity_key.has(fact.identity_key)) {
      this.indexes.facts_by_identity_key.set(fact.identity_key, new Set());
    }
    this.indexes.facts_by_identity_key.get(fact.identity_key)!.add(fid);

    if (!this.indexes.facts_by_source.has(fact.source)) {
      this.indexes.facts_by_source.set(fact.source, new Set());
    }
    this.indexes.facts_by_source.get(fact.source)!.add(fid);

    if (fact.state === "active") this.indexes.active_facts.add(fid);
    if (fact.state === "conflicted") this.indexes.conflicted_facts.add(fid);
  }

  private transitionFact(factId: FactId, newState: FactState, now: string): void {
    const fact = this.working.facts[factId] as Fact | undefined;
    if (!fact) return;
    const previousState = fact.state;
    this.working.facts[factId] = {
      ...fact,
      state: newState,
      observations: { ...fact.observations, last_seen: now },
    };
    // Keep state indexes aligned with state transitions.
    if (previousState === "active") this.indexes.active_facts.delete(factId);
    if (previousState === "conflicted") this.indexes.conflicted_facts.delete(factId);
    if (newState === "active") this.indexes.active_facts.add(factId);
    if (newState === "conflicted") this.indexes.conflicted_facts.add(factId);
    this.dirty = true;
  }

  private propagateStaleness(factId: FactId): void {
    const stale = collectStaleSummaries(factId, this.indexes);
    for (const sid of stale) {
      const summary = this.working.summaries[sid] as Summary | undefined;
      if (summary && !summary.stale) {
        this.working.summaries[sid] = { ...summary, stale: true };
        this.recordOp("summary_staled", { summary_id: sid, triggered_by_fact: factId });
      }
    }
  }

  private recordOp(type: OperationType, metadata: Record<string, unknown>): OperationId {
    const op_id = `OP_${Date.now()}_${(++opSeq).toString().padStart(4, "0")}` as OperationId;
    const op: Operation = {
      op_id,
      type,
      actor: this.actor,
      timestamp: new Date().toISOString(),
      revision_before: this.working.revision,
      revision_after: this.working.revision, // updated on commit
      metadata,
    };
    this.working.operations.push(op);
    return op_id;
  }
}

// ---------------------------------------------------------------------------
// Module-level operation sequence counter
// ---------------------------------------------------------------------------

let opSeq = 0;

function shortId(): string {
  return randomUUID().replace(/-/g, "").slice(0, 12).toUpperCase();
}
