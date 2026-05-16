/**
 * Index builder and validator.
 *
 * Indexes are NEVER persisted — always rebuilt from the authoritative manifest
 * on load. This keeps the manifest portable and eliminates index/manifest
 * divergence as a failure mode.
 *
 * Hot paths:
 *   facts_by_identity_key — hit on every addFact() for arbitration
 *   facts_by_type         — hit by queryFacts({ type }) — most common filter
 *   summary_deps          — hit on every fact state transition for staleness propagation
 *   active_facts          — hit by injection compiler and getConflictedFacts()
 *   conflicted_facts      — hit by injection compiler and getConflictedFacts()
 *
 * Cooler paths (diagnostics / completeness):
 *   facts_by_source       — provenance queries
 *   refs_by_source        — diagnostic / recall queries
 */

import type {
  Fact,
  FactId,
  FactState,
  Manifest,
  ManifestIndexes,
  RefId,
  SummaryId,
} from "../types/manifest.js";

// ---------------------------------------------------------------------------
// Build
// ---------------------------------------------------------------------------

export function buildIndexes(manifest: Manifest): ManifestIndexes {
  const indexes: ManifestIndexes = {
    facts_by_type: new Map(),
    facts_by_identity_key: new Map(),
    facts_by_source: new Map(),
    refs_by_source: new Map(),
    summary_deps: new Map(),
    active_facts: new Set(),
    conflicted_facts: new Set(),
  };

  for (const fact of Object.values(manifest.facts) as Fact[]) {
    indexFact(indexes, fact);
  }

  for (const ref of Object.values(manifest.references) as Array<{ source: string; ref_id: RefId }>) {
    addToSetMap(indexes.refs_by_source, ref.source, ref.ref_id);
  }

  for (const summary of Object.values(manifest.summaries) as Array<{ summary_id: SummaryId; source_fact_ids: readonly FactId[] }>) {
    for (const fid of summary.source_fact_ids) {
      addToSetMap(indexes.summary_deps, fid, summary.summary_id);
    }
  }

  return indexes;
}

// ---------------------------------------------------------------------------
// Incremental update helpers — called by ManifestManager after mutations
// ---------------------------------------------------------------------------

/**
 * Add a newly created fact to all indexes.
 */
export function indexFact(indexes: ManifestIndexes, fact: Fact): void {
  const fid = fact.fact_id as FactId;

  addToSetMap(indexes.facts_by_type, fact.type, fid);
  addToSetMap(indexes.facts_by_identity_key, fact.identity_key, fid);
  addToSetMap(indexes.facts_by_source, fact.source, fid);

  if (fact.state === "active") indexes.active_facts.add(fid);
  if (fact.state === "conflicted") indexes.conflicted_facts.add(fid);
}

/**
 * Update state-tracking sets when a fact's state changes.
 * Called after arbitration transitions.
 */
export function reindexFactState(
  indexes: ManifestIndexes,
  factId: FactId,
  previousState: FactState,
  newState: FactState,
): void {
  if (previousState === newState) return;

  if (previousState === "active") indexes.active_facts.delete(factId);
  if (previousState === "conflicted") indexes.conflicted_facts.delete(factId);

  if (newState === "active") indexes.active_facts.add(factId);
  if (newState === "conflicted") indexes.conflicted_facts.add(factId);
}

/**
 * Register a summary's dependencies in the dep index.
 * Called when a new summary is added.
 */
export function indexSummaryDeps(
  indexes: ManifestIndexes,
  summaryId: SummaryId,
  sourceFactIds: readonly FactId[],
): void {
  for (const fid of sourceFactIds) {
    addToSetMap(indexes.summary_deps, fid, summaryId);
  }
}

// ---------------------------------------------------------------------------
// Validation — called after buildIndexes to detect divergence
// ---------------------------------------------------------------------------

export interface IndexValidationResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
}

/**
 * Validate that built indexes are consistent with manifest state.
 *
 * This is defensive programming at load time. Catches any case where:
 *   - A fact appears in an index but not the manifest
 *   - A fact's state is wrong in the active/conflicted sets
 *   - summary_deps references a fact that doesn't exist
 *   - refs_by_source has orphaned entries
 *
 * In normal operation this should always pass. It's a bug detector, not
 * a hot path — only called on load.
 */
export function validateIndexes(
  manifest: Manifest,
  indexes: ManifestIndexes,
): IndexValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];

  // Every fact_id in every type bucket must exist in the manifest.
  for (const [type, idSet] of indexes.facts_by_type) {
    for (const fid of idSet) {
      if (!manifest.facts[fid]) {
        errors.push(`facts_by_type[${type}] contains ${fid} which is not in manifest.facts`);
      }
    }
  }

  // Every fact in the manifest must appear in its type bucket.
  for (const [fid, fact] of Object.entries(manifest.facts)) {
    const typeBucket = indexes.facts_by_type.get((fact as Fact).type);
    if (!typeBucket?.has(fid as FactId)) {
      errors.push(`manifest.facts[${fid}] not found in facts_by_type[${(fact as Fact).type}]`);
    }
  }

  // active_facts / conflicted_facts must match manifest state.
  for (const fid of indexes.active_facts) {
    const fact = manifest.facts[fid] as Fact | undefined;
    if (!fact) {
      errors.push(`active_facts contains ${fid} which is not in manifest.facts`);
    } else if (fact.state !== "active") {
      errors.push(`active_facts contains ${fid} but manifest state is "${fact.state}"`);
    }
  }

  for (const fid of indexes.conflicted_facts) {
    const fact = manifest.facts[fid] as Fact | undefined;
    if (!fact) {
      errors.push(`conflicted_facts contains ${fid} which is not in manifest.facts`);
    } else if (fact.state !== "conflicted") {
      errors.push(`conflicted_facts contains ${fid} but manifest state is "${fact.state}"`);
    }
  }

  // Verify all active facts are in the set.
  for (const [fid, fact] of Object.entries(manifest.facts)) {
    const f = fact as Fact;
    if (f.state === "active" && !indexes.active_facts.has(fid as FactId)) {
      errors.push(`manifest.facts[${fid}] is active but missing from active_facts index`);
    }
    if (f.state === "conflicted" && !indexes.conflicted_facts.has(fid as FactId)) {
      errors.push(`manifest.facts[${fid}] is conflicted but missing from conflicted_facts index`);
    }
  }

  // summary_deps: every source_fact_id must exist in manifest.
  for (const [fid, summarySet] of indexes.summary_deps) {
    if (!manifest.facts[fid]) {
      warnings.push(
        `summary_deps[${fid}] references a fact not in manifest.facts (orphan dep, summaries: [${[...summarySet].join(",")}])`,
      );
    }
  }

  // refs_by_source: every ref_id must exist in manifest.
  for (const [source, refSet] of indexes.refs_by_source) {
    for (const rid of refSet) {
      if (!manifest.references[rid]) {
        errors.push(`refs_by_source[${source}] contains ${rid} which is not in manifest.references`);
      }
    }
  }

  return {
    ok: errors.length === 0,
    errors,
    warnings,
  };
}

// ---------------------------------------------------------------------------
// Lineage validation — called by persistence.load()
// ---------------------------------------------------------------------------

export interface LineageValidationResult {
  ok: boolean;
  errors: string[];
}

/**
 * Validate fact lineage references are internally consistent.
 *
 * Checks:
 *   - derived_from references exist in the manifest
 *   - supersedes references exist in the manifest
 *   - merged_from references exist in the manifest
 *   - No circular supersession (A supersedes B, B supersedes A)
 */
export function validateLineage(manifest: Manifest): LineageValidationResult {
  const errors: string[] = [];

  for (const [fid, rawFact] of Object.entries(manifest.facts)) {
    const fact = rawFact as Fact;

    for (const parentId of fact.derived_from) {
      if (!manifest.facts[parentId]) {
        errors.push(`Fact ${fid}: derived_from[${parentId}] does not exist in manifest`);
      }
    }

    if (fact.supersedes && !manifest.facts[fact.supersedes]) {
      errors.push(`Fact ${fid}: supersedes[${fact.supersedes}] does not exist in manifest`);
    }

    for (const mergedId of fact.merged_from ?? []) {
      if (!manifest.facts[mergedId]) {
        errors.push(`Fact ${fid}: merged_from[${mergedId}] does not exist in manifest`);
      }
    }
  }

  // Check for circular supersession (shallow — one level).
  for (const [fid, rawFact] of Object.entries(manifest.facts)) {
    const fact = rawFact as Fact;
    if (!fact.supersedes) continue;
    const superseded = manifest.facts[fact.supersedes] as Fact | undefined;
    if (superseded?.supersedes === fid) {
      errors.push(`Circular supersession: ${fid} ↔ ${fact.supersedes}`);
    }
  }

  return { ok: errors.length === 0, errors };
}

/**
 * Check for orphaned references — ref_ids in manifest.references with no
 * corresponding artifact path (not a file-existence check, just ref integrity).
 */
export function findOrphanedRefs(manifest: Manifest): string[] {
  const factRefIds = new Set<string>();
  for (const fact of Object.values(manifest.facts) as Fact[]) {
    for (const src of fact.supporting_sources) {
      factRefIds.add(src);
    }
  }

  const orphans: string[] = [];
  for (const refId of Object.keys(manifest.references)) {
    // A ref is orphaned if nothing in facts or summaries references it.
    const inSummaries = Object.values(manifest.summaries).some(
      (s) => ((s as { source_ref_ids: readonly string[] }).source_ref_ids ?? []).includes(refId),
    );
    if (!factRefIds.has(refId) && !inSummaries) {
      orphans.push(refId);
    }
  }
  return orphans;
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function addToSetMap<K, V>(map: Map<K, Set<V>>, key: K, value: V): void {
  let s = map.get(key);
  if (!s) {
    s = new Set<V>();
    map.set(key, s);
  }
  s.add(value);
}
