/**
 * Token-budgeted sliding window for conversation history.
 *
 * Responsibilities:
 * - Maintain a bounded active history of completed turns.
 * - Evict oldest completed turns when budget is exceeded.
 * - Flag large tool results for automatic artifact offloading.
 * - Never modify the manifest — only manages conversation structure.
 *
 * "Completed turn" = one full (User + Assistant + optional ToolResults) cycle.
 * Eviction operates on whole turns, never partial ones.
 */

export interface TurnMessage {
  role: "user" | "assistant" | "tool";
  content: string;
  /** Tool call id, for role=tool messages. */
  tool_call_id?: string;
  /** Model-assigned tool call id, for role=assistant tool-use messages. */
  tool_calls?: unknown[];
}

export interface Turn {
  /** Monotonically incrementing index for stable ordering. */
  index: number;
  messages: TurnMessage[];
  /** Estimated token count for the full turn. Derived via char/token ratio. */
  token_estimate: number;
  /** True when this turn contains a tool result that was offloaded to an artifact. */
  has_offloaded_artifact: boolean;
}

export interface OffloadCandidate {
  turn_index: number;
  message_index: number;
  original_content: string;
  ref_id: string;
  replacement: string;
}

export interface SlidingWindowConfig {
  /** Maximum total tokens in active history. Default: 6000. */
  max_tokens: number;
  /** Maximum number of completed turns to keep. Default: 5. */
  max_turns: number;
  /** Tool result content longer than this is flagged for offloading. Default: 4000 chars. */
  offload_threshold_chars: number;
  /** Conservative char-to-token ratio for estimation. Default: 4. */
  chars_per_token: number;
}

const DEFAULTS: SlidingWindowConfig = {
  max_tokens: 6000,
  max_turns: 5,
  offload_threshold_chars: 4000,
  chars_per_token: 4,
};

export class SlidingWindow {
  private turns: Turn[] = [];
  private nextIndex = 0;
  readonly config: SlidingWindowConfig;

  constructor(config: Partial<SlidingWindowConfig> = {}) {
    this.config = { ...DEFAULTS, ...config };
  }

  // -------------------------------------------------------------------------
  // pushTurn — add a completed turn and enforce budget
  // -------------------------------------------------------------------------

  pushTurn(messages: TurnMessage[]): {
    offloads: OffloadCandidate[];
    evicted: number;
  } {
    const offloads = this.detectOffloads(messages, this.nextIndex);

    // Apply offload replacements to the messages before storing.
    const processedMessages = this.applyOffloads(messages, offloads);

    const turn: Turn = {
      index: this.nextIndex++,
      messages: processedMessages,
      token_estimate: this.estimateTokens(processedMessages),
      has_offloaded_artifact: offloads.length > 0,
    };

    this.turns.push(turn);
    const evicted = this.enforcebudget();

    return { offloads, evicted };
  }

  // -------------------------------------------------------------------------
  // getActiveHistory — returns all messages in active window, ordered
  // -------------------------------------------------------------------------

  getActiveHistory(): TurnMessage[] {
    return this.turns.flatMap((t) => t.messages);
  }

  // -------------------------------------------------------------------------
  // stats
  // -------------------------------------------------------------------------

  get totalTokens(): number {
    return this.turns.reduce((sum, t) => sum + t.token_estimate, 0);
  }

  get turnCount(): number {
    return this.turns.length;
  }

  get oldestTurnIndex(): number | undefined {
    return this.turns[0]?.index;
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  private detectOffloads(messages: TurnMessage[], turnIndex: number): OffloadCandidate[] {
    const candidates: OffloadCandidate[] = [];
    for (let i = 0; i < messages.length; i++) {
      const msg = messages[i]!;
      if (msg.role === "tool" && msg.content.length > this.config.offload_threshold_chars) {
        const ref_id = `REF_${turnIndex.toString().padStart(4, "0")}_${i.toString().padStart(2, "0")}`;
        candidates.push({
          turn_index: turnIndex,
          message_index: i,
          original_content: msg.content,
          ref_id,
          replacement: this.buildOffloadReplacement(ref_id, msg.content),
        });
      }
    }
    return candidates;
  }

  private applyOffloads(messages: TurnMessage[], offloads: OffloadCandidate[]): TurnMessage[] {
    if (offloads.length === 0) return messages;
    const byIndex = new Map(offloads.map((o) => [o.message_index, o]));
    return messages.map((msg, i) => {
      const offload = byIndex.get(i);
      return offload ? { ...msg, content: offload.replacement } : msg;
    });
  }

  private buildOffloadReplacement(ref_id: string, content: string): string {
    const lines = content.split("\n").length;
    const bytes = Buffer.byteLength(content, "utf8");
    return (
      `[Large output offloaded to artifact store]\n` +
      `REF_ID: ${ref_id}\n` +
      `Size: ${bytes} bytes / ${lines} lines\n` +
      `Use: recall_evidence({ ref_id: "${ref_id}", mode: "summary" }) to inspect.\n` +
      `Use: recall_evidence({ ref_id: "${ref_id}", mode: "search", query: "..." }) for targeted lookup.`
    );
  }

  private enforcebudget(): number {
    let evicted = 0;
    while (
      this.turns.length > 0 &&
      (this.turns.length > this.config.max_turns ||
        this.totalTokens > this.config.max_tokens)
    ) {
      this.turns.shift();
      evicted++;
    }
    return evicted;
  }

  private estimateTokens(messages: TurnMessage[]): number {
    const chars = messages.reduce((sum, m) => sum + m.content.length, 0);
    return Math.ceil(chars / this.config.chars_per_token);
  }
}
