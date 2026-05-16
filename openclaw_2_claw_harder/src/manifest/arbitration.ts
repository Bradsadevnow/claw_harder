/**
 * Arbitration engine — deterministic belief management.
 *
 * This is where the manifest stops being storage and becomes cognition infrastructure.
 *
 * Core insight: identity ≠ equality.
 * Two observations can refer to the same entity while making contradictory claims.
 * The arbitration engine handles that explicitly, not by overwriting or ignoring.
 *
 * Decision outcomes:
 *
 *   "new"       — no existing fact with this identity_key; add as active.
 *
 *   "converge"  — existing fact has identical fact_hash (same value).
 *                 Don't add a new fact. Update existing: increment observation_count,
 *                 update last_seen, add new source to supporting_sources.
 *                 Confidence rises slightly via convergence bonus.
 *
 *   "supersede" — existing fact has different value; new confidence is
 *                 significantly higher (delta >= SUPERIORITY_THRESHOLD).
 *                 Add new fact as active. Mark existing as superseded.
 *
 *   "reject"    — existing fact has different value; existing confidence is
 *                 significantly higher (delta >= SUPERIORITY_THRESHOLD).
 *                 Add new fact as superseded (for lineage), existing stays active.
 *                 Existing observation_count incremented anyway (we saw something).
 *
 *   "conflict"  — existing fact has different value; confidence delta is small
 *                 (< SUPERIORITY_THRESHOLD in either direction).
 *                 Add new fact as conflicted. Mark existing as conflicted.
 *                 Neither claim wins. Requires explicit resolution.
 *
 * Uncertainty is a first-class state. Agents are taught to remain uncertain
 * in the presence of contradictory evidence rather than forcing a guess.
 */

import type {
  ArbitrationDecision,
  Fact,
  FactId,
  FactObservations,
  FactState,
  Manifest,
  ManifestIndexes,
} from "../types/manifest.js";

// ---------------------------------------------------------------------------
// Thresholds
// ---------------------------------------------------------------------------

/** Confidence delta required for one observation to supersede another. */
export const CONFIDENCE_SUPERIORITY_THRESHOLD = 0.15;

/** Confidence bonus applied per additional convergent observation. */
export const CONVERGENCE_BONUS = 0.02;

/** Maximum confidence value. */
export const CONFIDENCE_MAX = 1.0;

// ---------------------------------------------------------------------------
// ArbitrationEngine
// ---------------------------------------------------------------------------

export class ArbitrationEngine {
  constructor(
    private readonly superiorityThreshold = CONFIDENCE_SUPERIORITY_THRESHOLD,
  ) {}

  /**
   * Determine what to do when a new observation arrives.
   *
   * @param incoming  The proposed new fact (not yet in manifest).
   * @param manifest  Current manifest state.
   * @param indexes   Live indexes for O(1) identity_key lookup.
   */
  arbitrate(
    incoming: Pick<Fact, "fact_hash" | "identity_key" | "confidence" | "source">,
    manifest: Manifest,
    indexes: ManifestIndexes,
  ): ArbitrationDecision {
    const existingIds = indexes.facts_by_identity_key.get(incoming.identity_key);

    if (!existingIds || existingIds.size === 0) {
      return { outcome: "new", reason: "No existing fact with this identity_key." };
    }

    // Find the current authoritative fact — the active or conflicted one.
    // If there are multiple (shouldn't happen in a healthy manifest but possible
    // during conflict state), pick the highest-confidence one.
    const authoritative = this.findAuthoritativeFact(existingIds, manifest);
    if (!authoritative) {
      // All existing facts for this key are archived/superseded/invalidated.
      return { outcome: "new", reason: "All prior facts for this identity_key are retired." };
    }

    // Exact match — same hash means same value.
    if (incoming.fact_hash === authoritative.fact_hash) {
      return {
        outcome: "converge",
        existing_id: authoritative.fact_id as FactId,
        reason: `Identical value already observed (hash match). Strengthening confidence via convergence.`,
      };
    }

    // Different value — arbitrate by confidence.
    const delta = incoming.confidence - authoritative.confidence;

    if (delta >= this.superiorityThreshold) {
      return {
        outcome: "supersede",
        existing_id: authoritative.fact_id as FactId,
        reason: `Incoming confidence ${incoming.confidence.toFixed(2)} exceeds existing ${authoritative.confidence.toFixed(2)} by ${delta.toFixed(2)} (threshold: ${this.superiorityThreshold}). Superseding.`,
      };
    }

    if (-delta >= this.superiorityThreshold) {
      return {
        outcome: "reject",
        existing_id: authoritative.fact_id as FactId,
        reason: `Existing confidence ${authoritative.confidence.toFixed(2)} exceeds incoming ${incoming.confidence.toFixed(2)} by ${(-delta).toFixed(2)} (threshold: ${this.superiorityThreshold}). Rejecting incoming. Recording as superseded for lineage.`,
      };
    }

    // Delta is small in both directions — genuine uncertainty.
    return {
      outcome: "conflict",
      existing_id: authoritative.fact_id as FactId,
      reason: `Confidence delta ${Math.abs(delta).toFixed(2)} is below superiority threshold ${this.superiorityThreshold}. Sources "${authoritative.source}" and "${incoming.source}" disagree. Marking both as conflicted.`,
    };
  }

  /**
   * Apply convergence to an existing fact:
   * - Increment observation_count
   * - Update last_seen
   * - Add source to supporting_sources (deduplicated)
   * - Apply confidence bonus (capped at CONFIDENCE_MAX)
   */
  applyConvergence(
    existing: Fact,
    incomingSource: string,
    now: string,
  ): Partial<Fact> {
    const newCount = existing.observations.observation_count + 1;
    const newConfidence = Math.min(
      CONFIDENCE_MAX,
      existing.confidence + CONVERGENCE_BONUS,
    );
    const sources = Array.from(
      new Set([...existing.supporting_sources, incomingSource]),
    );

    return {
      confidence: newConfidence,
      observations: {
        ...existing.observations,
        last_seen: now,
        observation_count: newCount,
      } satisfies FactObservations,
      supporting_sources: sources,
    };
  }

  /**
   * Determine the state a new incoming fact should have based on arbitration outcome.
   */
  incomingStateForOutcome(
    outcome: ArbitrationDecision["outcome"],
  ): FactState {
    switch (outcome) {
      case "new":
      case "supersede":
        return "active";
      case "converge":
        // Converge means we don't add a new fact at all — caller handles this.
        return "active";
      case "conflict":
        return "conflicted";
      case "reject":
        return "superseded";
    }
  }

  /**
   * Determine the new state of the EXISTING fact after arbitration.
   */
  existingStateAfterOutcome(
    outcome: ArbitrationDecision["outcome"],
    currentState: FactState,
  ): FactState {
    switch (outcome) {
      case "supersede":
        return "superseded";
      case "conflict":
        return "conflicted";
      case "new":
      case "converge":
      case "reject":
        // Existing stays as-is.
        return currentState;
    }
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  private findAuthoritativeFact(
    ids: Set<FactId>,
    manifest: Manifest,
  ): Fact | null {
    let best: Fact | null = null;

    for (const id of ids) {
      const fact = manifest.facts[id] as Fact | undefined;
      if (!fact) continue;
      if (fact.state === "archived" || fact.state === "invalidated") continue;
      // Prefer active over conflicted; among ties, prefer higher confidence.
      if (
        !best ||
        (fact.state === "active" && best.state !== "active") ||
        (fact.state === best.state && fact.confidence > best.confidence)
      ) {
        best = fact;
      }
    }

    return best;
  }
}

// ---------------------------------------------------------------------------
// Staleness propagation — called whenever a fact changes state
// ---------------------------------------------------------------------------

/**
 * Returns summary_ids that should be marked stale because their source fact changed.
 * O(1) via the summary_deps index.
 */
export function collectStaleSummaries(
  factId: FactId,
  indexes: ManifestIndexes,
): Set<string> {
  return new Set(indexes.summary_deps.get(factId) ?? []);
}
