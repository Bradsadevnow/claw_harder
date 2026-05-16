/**
 * OpenClaw ContextEngine adapter.
 *
 * Wires the manifest engine into OpenClaw's `plugins.slots.contextEngine` seam.
 *
 * Registration pattern:
 *   1. Install this package
 *   2. Set `plugins.slots.contextEngine: "manifest-engine"` in openclaw config
 *   3. That's it — the plugin.json entry handles the rest
 *
 * Lifecycle:
 *   bootstrap  → load or create ManifestManager for the session
 *   ingest     → buffer incoming messages
 *   ingestBatch→ buffer a full turn batch
 *   assemble   → compile manifest + sliding window → ordered AgentMessage[]
 *   afterTurn  → completeTurn, selective commit
 *   compact    → delegate to OpenClaw's built-in runtime compaction
 *   dispose    → commit any pending dirty state
 *
 * AgentMessage content handling:
 *   - string content → used directly
 *   - array content  → text parts joined; non-text parts described
 *   Only "user" and "assistant" roles flow into the sliding window.
 *   "compactionSummary" and other runtime markers are skipped.
 */

import { join, dirname } from "node:path";
import { ManifestManager } from "../manifest/manifest-manager.js";
import { ContextEngine as ManifestContextEngine, type ContextEngineConfig } from "../context/context-engine.js";
import type { TurnMessage } from "../context/sliding-window.js";
import type { AddFactInput, AddReferenceInput, AddSummaryInput, FactId, QueryFactsFilter } from "../types/manifest.js";

// ---------------------------------------------------------------------------
// OpenClaw interface — structural types (avoids hard coupling to openclaw dist)
// ---------------------------------------------------------------------------

// Structural minimum of AgentMessage we need. Matches @earendil-works/pi-agent-core.
interface AgentMessageLike {
  role: string;
  content: string | Array<{ type: string; text?: string; [key: string]: unknown }>;
  timestamp?: number;
}

// Structural minimum of the OpenClaw ContextEngine interface we implement.
// These match src/context-engine/types.ts in openclaw-main exactly.
export interface OpenClawContextEngineInfo {
  id: string;
  name: string;
  version?: string;
  ownsCompaction?: boolean;
  turnMaintenanceMode?: "foreground" | "background";
}

export interface OpenClawAssembleResult {
  messages: AgentMessageLike[];
  estimatedTokens: number;
  systemPromptAddition?: string;
  promptAuthority?: "assembled" | "preassembly_may_overflow";
}

export interface OpenClawCompactResult {
  ok: boolean;
  compacted: boolean;
  reason?: string;
  result?: {
    summary?: string;
    firstKeptEntryId?: string;
    tokensBefore: number;
    tokensAfter?: number;
    details?: unknown;
    sessionId?: string;
    sessionFile?: string;
  };
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface ManifestEngineAdapterConfig {
  /** Engine id registered in OpenClaw's context engine registry. Default: "manifest-engine". */
  engineId: string;
  /** Engine display name. Default: "OpenClaw Manifest Engine". */
  engineName: string;
  /** Version string. */
  version: string;
  /** Directory under which per-session manifests are stored. Default: uses sessionFile dir. */
  manifestRootDir?: string;
  /** ContextEngine config forwarded to the internal engine. */
  contextEngine?: Partial<ContextEngineConfig>;
}

const ADAPTER_DEFAULTS: ManifestEngineAdapterConfig = {
  engineId: "manifest-engine",
  engineName: "OpenClaw Manifest Engine",
  version: "0.1.0",
};

// ---------------------------------------------------------------------------
// Session state
// ---------------------------------------------------------------------------

interface SessionState {
  manager: ManifestManager;
  engine: ManifestContextEngine;
  pendingMessages: TurnMessage[];
}

// ---------------------------------------------------------------------------
// ManifestContextEngineAdapter
// ---------------------------------------------------------------------------

export class ManifestContextEngineAdapter {
  readonly info: OpenClawContextEngineInfo;
  private readonly sessions = new Map<string, SessionState>();
  private readonly adapterConfig: ManifestEngineAdapterConfig;

  constructor(config: Partial<ManifestEngineAdapterConfig> = {}) {
    this.adapterConfig = { ...ADAPTER_DEFAULTS, ...config };
    this.info = {
      id: this.adapterConfig.engineId,
      name: this.adapterConfig.engineName,
      version: this.adapterConfig.version,
      ownsCompaction: false,
      turnMaintenanceMode: "foreground",
    };
  }

  // -------------------------------------------------------------------------
  // bootstrap — load or create manifest for session
  // -------------------------------------------------------------------------

  async bootstrap(params: {
    sessionId: string;
    sessionKey?: string;
    sessionFile: string;
  }): Promise<{ bootstrapped: boolean; reason?: string }> {
    const key = params.sessionId;
    if (this.sessions.has(key)) {
      return { bootstrapped: false, reason: "already_bootstrapped" };
    }

    const sessionDir = this.adapterConfig.manifestRootDir
      ? join(this.adapterConfig.manifestRootDir, params.sessionId)
      : dirname(params.sessionFile);

    const manifestPath = join(sessionDir, "manifest.json");
    const artifactsDir = join(sessionDir, "artifacts");

    const manager = await ManifestManager.load(manifestPath, artifactsDir, "manifest-engine");
    const engine = new ManifestContextEngine(manager, {
      auto_commit: true,
      ...this.adapterConfig.contextEngine,
    });

    this.sessions.set(key, { manager, engine, pendingMessages: [] });
    return { bootstrapped: true };
  }

  // -------------------------------------------------------------------------
  // ingest — buffer a single message
  // -------------------------------------------------------------------------

  async ingest(params: {
    sessionId: string;
    sessionKey?: string;
    message: AgentMessageLike;
    isHeartbeat?: boolean;
  }): Promise<{ ingested: boolean }> {
    const state = this.sessions.get(params.sessionId);
    if (!state) return { ingested: false };

    const turn = agentMessageToTurnMessage(params.message);
    if (turn) {
      state.pendingMessages.push(turn);
    }
    return { ingested: turn !== null };
  }

  // -------------------------------------------------------------------------
  // ingestBatch — buffer a full turn batch
  // -------------------------------------------------------------------------

  async ingestBatch(params: {
    sessionId: string;
    sessionKey?: string;
    messages: AgentMessageLike[];
    isHeartbeat?: boolean;
  }): Promise<{ ingestedCount: number }> {
    const state = this.sessions.get(params.sessionId);
    if (!state) return { ingestedCount: 0 };

    let count = 0;
    for (const msg of params.messages) {
      const turn = agentMessageToTurnMessage(msg);
      if (turn) {
        state.pendingMessages.push(turn);
        count++;
      }
    }
    return { ingestedCount: count };
  }

  // -------------------------------------------------------------------------
  // assemble — build context for model call
  // -------------------------------------------------------------------------

  async assemble(params: {
    sessionId: string;
    sessionKey?: string;
    messages: AgentMessageLike[];
    tokenBudget?: number;
    availableTools?: Set<string>;
    model?: string;
    prompt?: string;
  }): Promise<OpenClawAssembleResult> {
    const state = this.sessions.get(params.sessionId);
    if (!state) {
      // No state — pass messages through unchanged as a safe fallback.
      return {
        messages: params.messages,
        estimatedTokens: estimateTokens(params.messages),
        promptAuthority: "assembled",
      };
    }

    // If there's an incoming user prompt, build messages around it.
    const systemPrompt = extractSystemPrompt(params.messages);
    const userPrompt = params.prompt ?? extractLastUserContent(params.messages);

    // Build the manifest-engine message array.
    const compiled = state.engine.buildMessages(
      systemPrompt ?? "",
      userPrompt ?? "",
    );

    // Convert back to AgentMessage shape.
    const assembledMessages = compiled.map(turnMessageToAgentMessage);

    return {
      messages: assembledMessages,
      estimatedTokens: estimateTokens(assembledMessages),
      promptAuthority: "assembled",
    };
  }

  // -------------------------------------------------------------------------
  // afterTurn — commit completed turn state
  // -------------------------------------------------------------------------

  async afterTurn(params: {
    sessionId: string;
    sessionKey?: string;
    sessionFile: string;
    messages: AgentMessageLike[];
    prePromptMessageCount: number;
    isHeartbeat?: boolean;
    tokenBudget?: number;
  }): Promise<void> {
    const state = this.sessions.get(params.sessionId);
    if (!state) return;

    // The turn messages from the run (new messages since prePromptMessageCount).
    const turnMessages = params.messages.slice(params.prePromptMessageCount);
    const converted = turnMessages
      .map(agentMessageToTurnMessage)
      .filter((m): m is TurnMessage => m !== null);

    // Flush any buffered pending messages into this turn.
    const allTurnMessages = [...state.pendingMessages, ...converted];
    state.pendingMessages = [];

    if (allTurnMessages.length === 0) return;

    const result = await state.engine.completeTurn(
      allTurnMessages,
      false, // caller manages fact-producing signals via addFact/addSummary
    );

    if (result.sync_required) {
      await state.engine.syncAndReplan();
    }
  }

  // -------------------------------------------------------------------------
  // compact — delegate to OpenClaw runtime
  // -------------------------------------------------------------------------

  async compact(params: {
    sessionId: string;
    sessionKey?: string;
    sessionFile: string;
    tokenBudget?: number;
    force?: boolean;
    currentTokenCount?: number;
    compactionTarget?: "budget" | "threshold";
    customInstructions?: string;
    runtimeContext?: Record<string, unknown>;
  }): Promise<OpenClawCompactResult> {
    // We don't own compaction — delegate to OpenClaw's built-in runtime.
    // This is the same pattern as delegateCompactionToRuntime() in openclaw core.
    const runtimeContext = params.runtimeContext ?? {};
    const delegateFn = (runtimeContext as Record<string, unknown>)["delegateCompaction"];
    if (typeof delegateFn === "function") {
      return delegateFn(params) as Promise<OpenClawCompactResult>;
    }
    // No delegate available — report no-op.
    return { ok: true, compacted: false, reason: "no_delegate_available" };
  }

  // -------------------------------------------------------------------------
  // dispose — flush dirty state
  // -------------------------------------------------------------------------

  async dispose(): Promise<void> {
    const sessions = Array.from(this.sessions.values());
    await Promise.allSettled(
      sessions.map(async (state) => {
        if (state.manager.isDirty) {
          await state.manager.commit();
        }
      }),
    );
    this.sessions.clear();
  }

  // -------------------------------------------------------------------------
  // Manifest mutation proxies — called by the model/tooling layer
  // -------------------------------------------------------------------------

  addFact(sessionId: string, input: AddFactInput) {
    return this.sessions.get(sessionId)?.engine.addFact(input);
  }

  async addReference(sessionId: string, input: AddReferenceInput) {
    return this.sessions.get(sessionId)?.engine.addReference(input);
  }

  addSummary(sessionId: string, input: AddSummaryInput) {
    return this.sessions.get(sessionId)?.engine.addSummary(input);
  }

  queryFacts(sessionId: string, filter?: QueryFactsFilter) {
    return this.sessions.get(sessionId)?.engine.queryFacts(filter) ?? [];
  }

  getEngine(sessionId: string): ManifestContextEngine | undefined {
    return this.sessions.get(sessionId)?.engine;
  }
}

// ---------------------------------------------------------------------------
// Conversion helpers
// ---------------------------------------------------------------------------

function extractTextContent(
  content: AgentMessageLike["content"],
): string {
  if (typeof content === "string") return content;
  return content
    .map((part) => {
      if (part.type === "text" && typeof part.text === "string") return part.text;
      return `[${part.type}]`;
    })
    .join(" ");
}

function agentMessageToTurnMessage(msg: AgentMessageLike): TurnMessage | null {
  const role = msg.role;
  if (role !== "user" && role !== "assistant" && role !== "tool") {
    return null; // skip compactionSummary and other runtime markers
  }
  return {
    role: role as "user" | "assistant" | "tool",
    content: extractTextContent(msg.content),
  };
}

function turnMessageToAgentMessage(msg: TurnMessage): AgentMessageLike {
  return {
    role: msg.role,
    content: msg.content,
    timestamp: Date.now(),
  };
}

function extractSystemPrompt(messages: AgentMessageLike[]): string | undefined {
  // In OpenClaw the "system" context is typically the first user message
  // or a dedicated system slot. We surface a best-effort extraction.
  const first = messages[0];
  if (!first) return undefined;
  if (first.role === "system" || first.role === "user") {
    return extractTextContent(first.content);
  }
  return undefined;
}

function extractLastUserContent(messages: AgentMessageLike[]): string | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === "user") {
      return extractTextContent(messages[i]!.content);
    }
  }
  return undefined;
}

function estimateTokens(messages: AgentMessageLike[]): number {
  return Math.ceil(
    messages.reduce((sum, m) => sum + extractTextContent(m.content).length, 0) / 4,
  );
}

// ---------------------------------------------------------------------------
// Factory — called by OpenClaw plugin system
// ---------------------------------------------------------------------------

export function createManifestContextEngineAdapter(
  config?: Partial<ManifestEngineAdapterConfig>,
): ManifestContextEngineAdapter {
  return new ManifestContextEngineAdapter(config);
}
