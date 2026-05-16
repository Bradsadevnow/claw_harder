/**
 * ContextEngine — the full runtime.
 *
 * Orchestrates:
 * - ManifestManager (state substrate)
 * - SlidingWindow (conversation budget)
 * - InjectionCompiler (manifest → prompt strata)
 * - Selective commit (only after fact-producing turns)
 * - Sync & Replan (revision conflict recovery)
 *
 * Injection contract:
 *   Turn structure: [system_prompt, operational_memory (user), ...history, current_user_message]
 *
 * The operational_memory block is a User Message, not part of the system prompt.
 * This preserves system prompt stability and prevents policy dilution.
 *
 * Selective commit triggers:
 *   - addFact() called with outcome !== "converge"  (new belief written)
 *   - addReference() called                          (artifact stored)
 *   - addSummary() called                            (derived view updated)
 *   - proposeRefinement() or proposeConsolidation()  (explicit adjudication)
 *
 * Non-triggering (no commit):
 *   - queryFacts(), convergence-only addFact()
 *   - recordOperation() for informational events
 *   - Routine turn completion with no new knowledge
 */

import type { ManifestManager } from "../manifest/manifest-manager.js";
import type {
  AddFactInput,
  AddReferenceInput,
  AddSummaryInput,
  CommitResult,
  Fact,
  FactId,
  OperationType,
  QueryFactsFilter,
} from "../types/manifest.js";

// Re-export so callers don't need to reach into internals.
export type { AddFactResult, AddReferenceResult, AddSummaryResult, ProposeRefinementResult } from "../manifest/manifest-manager.js";

import type { CommitResult as PersistCommitResult } from "../manifest/persistence.js";
import { InjectionCompiler, type InjectionConfig } from "./injection.js";
import { SlidingWindow, type SlidingWindowConfig, type TurnMessage } from "./sliding-window.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ContextEngineConfig {
  sliding_window?: Partial<SlidingWindowConfig>;
  injection?: Partial<InjectionConfig>;
  /**
   * Whether to auto-commit after fact-producing events.
   * Default: true. Set false if caller manages commits manually.
   */
  auto_commit: boolean;
}

export interface BuildContextResult {
  /** The operational memory block — inject as a User Message after system prompt. */
  operational_memory: string;
  /** The bounded active history — append current user message after these. */
  history: TurnMessage[];
  /** Snapshot of manifest revision at time of build. */
  revision: number;
}

export interface TurnResult {
  offloaded_ref_ids: string[];
  evicted_turns: number;
  committed: boolean;
  commit_result?: PersistCommitResult;
  sync_required: boolean;
}

export interface SyncReplanResult {
  ok: boolean;
  detail?: string;
}

// ---------------------------------------------------------------------------
// ContextEngine
// ---------------------------------------------------------------------------

export class ContextEngine {
  private readonly window: SlidingWindow;
  private readonly compiler: InjectionCompiler;
  private needsCommit = false;

  constructor(
    private readonly manager: ManifestManager,
    private readonly config: ContextEngineConfig = { auto_commit: true },
  ) {
    this.window = new SlidingWindow(config.sliding_window);
    this.compiler = new InjectionCompiler(config.injection);
  }

  // -------------------------------------------------------------------------
  // buildContext — called before every model invocation
  // -------------------------------------------------------------------------

  buildContext(): BuildContextResult {
    // Reload operational memory fresh from manifest on every turn.
    // Cost is O(facts + summaries) string formatting — acceptable.
    const operational_memory = this.compiler.compile(this.manager.manifest);
    const history = this.window.getActiveHistory();
    const revision = this.manager.getRevision();

    return { operational_memory, history, revision };
  }

  /**
   * Produce the full message array for a model call.
   *
   * Layout:
   *   [0] system_prompt  (caller's system message, untouched)
   *   [1] operational_memory  (this engine, User role)
   *   [2..N] active history
   *   [N+1] current_user_message
   */
  buildMessages(
    systemPrompt: string,
    currentUserMessage: string,
  ): TurnMessage[] {
    const { operational_memory, history } = this.buildContext();
    return [
      { role: "user", content: systemPrompt },        // system as user msg preserves low-entropy
      { role: "user", content: operational_memory },   // operational memory injection
      ...history,
      { role: "user", content: currentUserMessage },
    ];
  }

  // -------------------------------------------------------------------------
  // completeTurn — call after receiving model response + tool results
  // -------------------------------------------------------------------------

  async completeTurn(
    messages: TurnMessage[],
    factProducing: boolean,
  ): Promise<TurnResult> {
    const { offloads, evicted } = this.window.pushTurn(messages);

    // Register offloaded artifacts with the manifest.
    const offloadedIds: string[] = [];
    for (const offload of offloads) {
      const result = await this.manager.addReference({
        source: "sliding-window",
        label: `Auto-offloaded tool result (turn ${offload.turn_index})`,
        content: offload.original_content,
      });
      if (result.ok) offloadedIds.push(result.ref_id);
    }

    if (offloads.length > 0) {
      factProducing = true; // offloads always trigger a commit
    }

    let committed = false;
    let commit_result: PersistCommitResult | undefined;
    let sync_required = false;

    if (this.config.auto_commit && (factProducing || this.manager.isDirty)) {
      commit_result = await this.manager.commit();
      committed = true;
      if (!commit_result.ok && commit_result.reason === "revision_conflict") {
        sync_required = true;
      }
    }

    return {
      offloaded_ref_ids: offloadedIds,
      evicted_turns: evicted,
      committed,
      ...(commit_result !== undefined ? { commit_result } : {}),
      sync_required,
    };
  }

  // -------------------------------------------------------------------------
  // syncAndReplan — call when commit returns revision_conflict
  // -------------------------------------------------------------------------

  async syncAndReplan(): Promise<SyncReplanResult> {
    const result = await this.manager.reload();
    if (!result.ok) {
      return { ok: false, ...(result.detail !== undefined ? { detail: result.detail } : {}) };
    }
    this.manager.recordOperation("manifest_loaded", {
      event: "sync_replan",
      reason: "revision_conflict",
    });
    return { ok: true };
  }

  // -------------------------------------------------------------------------
  // Convenience mutation proxies — track whether commit is needed
  // -------------------------------------------------------------------------

  addFact(input: AddFactInput) {
    const result = this.manager.addFact(input);
    if (result.ok && result.outcome !== "converge") {
      this.needsCommit = true;
    }
    return result;
  }

  async addReference(input: AddReferenceInput) {
    const result = await this.manager.addReference(input);
    if (result.ok) this.needsCommit = true;
    return result;
  }

  addSummary(input: AddSummaryInput) {
    const result = this.manager.addSummary(input);
    if (result.ok) this.needsCommit = true;
    return result;
  }

  proposeRefinement(factId: FactId, input: Parameters<ManifestManager["proposeRefinement"]>[1]) {
    const result = this.manager.proposeRefinement(factId, input);
    if (result.ok) this.needsCommit = true;
    return result;
  }

  queryFacts(filter?: QueryFactsFilter): Fact[] {
    return this.manager.queryFacts(filter);
  }

  recordOperation(type: OperationType, metadata: Record<string, unknown>) {
    return this.manager.recordOperation(type, metadata);
  }

  // -------------------------------------------------------------------------
  // Direct commit — for callers managing their own commit schedule
  // -------------------------------------------------------------------------

  async commit(): Promise<CommitResult> {
    const result = await this.manager.commit();
    if (result.ok) this.needsCommit = false;
    return result as unknown as CommitResult;
  }

  get manifest() {
    return this.manager.manifest;
  }

  get isDirty() {
    return this.manager.isDirty;
  }

  get windowStats() {
    return {
      total_tokens: this.window.totalTokens,
      turn_count: this.window.turnCount,
      oldest_turn_index: this.window.oldestTurnIndex,
    };
  }
}
