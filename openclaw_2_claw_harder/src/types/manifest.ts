/**
 * Core type definitions for the OpenClaw Manifest Engine.
 *
 * Design invariants:
 * - Facts are IMMUTABLE once written. Refinement creates successor facts.
 * - Every mutation is traceable via derived_from / supersedes / merged_from lineage.
 * - The manifest is an append-only event log internally; the public API presents a live view.
 * - Schema versioning is mandatory — migrations are explicit, never silent.
 */

// ---------------------------------------------------------------------------
// Identifiers
// ---------------------------------------------------------------------------

export type FactId = `FACT_${string}`;
export type RefId = `REF_${string}`;
export type SummaryId = `SUM_${string}`;
export type OperationId = `OP_${string}`;
export type RevisionId = `REV_${string}`;

// ---------------------------------------------------------------------------
// Fact lifecycle states
// ---------------------------------------------------------------------------

export type FactState =
  | "active"      // current authoritative belief
  | "archived"    // older version kept for reference integrity
  | "superseded"  // replaced by a more precise successor fact
  | "invalidated" // proven false by subsequent evidence
  | "conflicted"; // multiple sources disagree; requires arbitration

// ---------------------------------------------------------------------------
// Fact types — extensible discriminated union
// ---------------------------------------------------------------------------

export type FactType =
  | "preference"   // user preference or stated desire
  | "contact"      // person or entity the user interacts with
  | "task"         // ongoing task or commitment
  | "context"      // situational context (location, time, activity)
  | "credential"   // auth / access token (stored reference only, never plaintext)
  | "service"      // external service or integration
  | "knowledge"    // learned fact about the world or the user's domain
  | "constraint"   // hard boundary on agent behavior
  | "event"        // discrete thing that happened
  | string;        // extensible — plugins may define additional types

// ---------------------------------------------------------------------------
// Core Fact record
// ---------------------------------------------------------------------------

export interface Fact {
  readonly fact_id: FactId;

  /** SHA256 of `type + canonical_json(value)` — drives deduplication. */
  readonly fact_hash: string;

  readonly type: FactType;

  /** The semantic payload. Typed per fact_type at runtime via narrowing. */
  readonly value: Record<string, unknown>;

  /**
   * Deterministic key for identity-based deduplication and arbitration.
   * Derived from type + stable fields in value (e.g. "contact:alice@example.com").
   */
  readonly identity_key: string;

  /** 0.0–1.0. Higher confidence wins arbitration when identity_keys collide. */
  readonly confidence: number;

  /** Agent id, plugin id, tool name, or "user" that produced this fact. */
  readonly source: string;

  /** Parent fact_ids this fact was derived from or that triggered its creation. */
  readonly derived_from: readonly FactId[];

  /** fact_id this fact supersedes (set when this is a refinement). */
  readonly supersedes?: FactId;

  /** fact_ids merged into this fact during consolidation. */
  readonly merged_from?: readonly FactId[];

  readonly state: FactState;

  readonly created_at: string; // ISO 8601

  /** Temporal stability tracking — updated on each confirmation. */
  readonly observations: FactObservations;

  /** Tools or sources that have independently confirmed this fact. */
  readonly supporting_sources: readonly string[];

  /** Stable ordinal index assigned at creation time. Used for natural-language addressing. */
  readonly discovery_index: number;
}

export interface FactObservations {
  readonly first_seen: string;  // ISO 8601
  readonly last_seen: string;   // ISO 8601
  readonly observation_count: number;
}

// ---------------------------------------------------------------------------
// Reference — pointer to a raw artifact stored outside the manifest
// ---------------------------------------------------------------------------

export interface Reference {
  readonly ref_id: RefId;

  /** Tool or agent that produced the artifact. */
  readonly source: string;

  /** Human-readable label for the artifact. */
  readonly label: string;

  /** Absolute filesystem path to the stored artifact. */
  readonly path: string;

  /** SHA256 of the artifact file contents — integrity check on recall. */
  readonly content_hash: string;

  /** Total line count, used by recall_evidence summary mode. */
  readonly line_count: number;

  /** Byte size of the artifact. */
  readonly byte_size: number;

  readonly created_at: string; // ISO 8601

  /** fact_ids this reference is evidence for. */
  readonly related_fact_ids: readonly FactId[];
}

// ---------------------------------------------------------------------------
// Summary — derived view, non-authoritative, tracks freshness
// ---------------------------------------------------------------------------

export type SummaryType =
  | "session"    // overall session state
  | "goal"       // active objective and backlog
  | "contact"    // what we know about a person
  | "task"       // task cluster
  | "conflict"   // unresolved contradictions requiring resolution
  | string;

export type SummaryState = "active" | "superseded" | "archived";

export interface Summary {
  readonly summary_id: SummaryId;
  readonly type: SummaryType;

  /** Scoping key, e.g. "session:abc123" or "contact:alice". */
  readonly scope: string;

  readonly state: SummaryState;

  /** True when any source fact has been superseded, conflicted, or invalidated since generation. */
  readonly stale: boolean;

  /** Weighted average of source fact confidences, capped at 0.80 if conflicts exist. */
  readonly confidence: number;

  readonly generated_at: string; // ISO 8601

  /** fact_ids that were inputs to this summary. */
  readonly source_fact_ids: readonly FactId[];

  /** ref_ids that were inputs to this summary. */
  readonly source_ref_ids: readonly RefId[];

  readonly key_points: readonly string[];

  /** Unresolved contradictions surfaced during synthesis. */
  readonly conflicts: readonly string[];

  /** Actions the agent should consider next. */
  readonly recommended_next_actions: readonly string[];
}

// ---------------------------------------------------------------------------
// Operation — forensic audit log entry
// ---------------------------------------------------------------------------

export type OperationType =
  | "fact_added"
  | "fact_superseded"
  | "fact_invalidated"
  | "fact_conflicted"
  | "fact_observation_updated"
  | "fact_consolidated"
  | "ref_added"
  | "summary_added"
  | "summary_staled"
  | "summary_superseded"
  | "manifest_loaded"
  | "manifest_committed"
  | "revision_conflict"
  | "recall_event";

export interface Operation {
  readonly op_id: OperationId;
  readonly type: OperationType;
  readonly actor: string;
  readonly timestamp: string; // ISO 8601
  readonly revision_before: number;
  readonly revision_after: number;

  /** Arbitrary structured metadata about the operation. */
  readonly metadata: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Manifest — the root document
// ---------------------------------------------------------------------------

export interface Manifest {
  readonly schema_version: "1.0";

  /** Monotonically incrementing integer. Used for optimistic locking. */
  readonly revision: number;

  /** SHA256 of canonical_json(facts + references + summaries) at last commit. */
  readonly integrity_hash: string;

  readonly created_at: string;   // ISO 8601
  readonly updated_at: string;   // ISO 8601

  /** Keyed by fact_id. */
  readonly facts: Readonly<Record<string, Fact>>;

  /** Keyed by ref_id. */
  readonly references: Readonly<Record<string, Reference>>;

  /** Keyed by summary_id. */
  readonly summaries: Readonly<Record<string, Summary>>;

  /** Ordered append-only log. */
  readonly operations: readonly Operation[];

  /** Monotonically incrementing — assigned to new facts on creation. */
  readonly last_fact_index: number;
}

// ---------------------------------------------------------------------------
// Index structures — maintained in memory, derived from manifest on load
// ---------------------------------------------------------------------------

export interface ManifestIndexes {
  facts_by_type: Map<FactType, Set<FactId>>;
  facts_by_identity_key: Map<string, Set<FactId>>;
  facts_by_source: Map<string, Set<FactId>>;
  refs_by_source: Map<string, Set<RefId>>;
  /** fact_id → summary_ids that depend on it. Used for O(1) staleness propagation. */
  summary_deps: Map<FactId, Set<SummaryId>>;
  active_facts: Set<FactId>;
  conflicted_facts: Set<FactId>;
}

// ---------------------------------------------------------------------------
// API types — inputs to ManifestManager methods
// ---------------------------------------------------------------------------

export interface AddFactInput {
  type: FactType;
  value: Record<string, unknown>;
  confidence: number;
  source: string;
  derived_from?: FactId[];
  supporting_sources?: string[];
}

export interface AddReferenceInput {
  source: string;
  label: string;
  content: string | Buffer;
  related_fact_ids?: FactId[];
}

export interface AddSummaryInput {
  type: SummaryType;
  scope: string;
  key_points: string[];
  conflicts?: string[];
  recommended_next_actions?: string[];
  source_fact_ids: FactId[];
  source_ref_ids?: RefId[];
  confidence: number;
}

// ---------------------------------------------------------------------------
// Arbitration result — returned by the arbitration engine
// ---------------------------------------------------------------------------

export type ArbitrationDecision =
  | { outcome: "new";        reason: string }
  | { outcome: "converge";   existing_id: FactId; reason: string }
  | { outcome: "supersede";  existing_id: FactId; reason: string }
  | { outcome: "conflict";   existing_id: FactId; reason: string }
  | { outcome: "reject";     existing_id: FactId; reason: string };

// ---------------------------------------------------------------------------
// Query filter — used by ManifestManager.queryFacts and ContextEngine
// ---------------------------------------------------------------------------

export interface QueryFactsFilter {
  type?: string;
  state?: FactState | FactState[];
  identity_key?: string;
  source?: string;
  min_confidence?: number;
  limit?: number;
}

// ---------------------------------------------------------------------------
// Commit result — public surface exposed by ContextEngine.commit()
// ---------------------------------------------------------------------------

export type CommitResult =
  | { ok: true; revision: number; integrity_hash: string }
  | { ok: false; reason: string; detail: string };

// ---------------------------------------------------------------------------
// Recall tool types
// ---------------------------------------------------------------------------

export type RecallMode = "summary" | "head" | "tail" | "range" | "full" | "search";

export interface RecallRequest {
  ref_id: RefId;
  mode: RecallMode;
  start_line?: number;
  end_line?: number;
  query?: string;
}

export interface RecallResult {
  ref_id: RefId;
  mode: RecallMode;
  label: string;
  total_lines: number;
  byte_size: number;
  source: string;
  created_at: string;
  lines?: Array<{ n: number; text: string }>;
  match_count?: number;
}
