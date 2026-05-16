/**
 * openclaw-manifest-engine — public API surface.
 *
 * Three surfaces:
 *   1. Core runtime — ManifestManager, ContextEngine, RecallTool
 *   2. Types — everything in src/types/manifest.ts
 *   3. OpenClaw adapter — ManifestContextEngineAdapter (wires to plugin slot)
 *
 * Usage without OpenClaw (standalone):
 *   import { ManifestManager, ContextEngine } from "openclaw-manifest-engine";
 *
 * Usage with OpenClaw (plugin mode):
 *   Set plugins.slots.contextEngine: "manifest-engine" in openclaw config.
 *   The plugin entry at ./plugin handles registration automatically.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type {
  Fact,
  FactId,
  FactState,
  FactType,
  FactObservations,
  Reference,
  RefId,
  Summary,
  SummaryId,
  SummaryState,
  SummaryType,
  Operation,
  OperationId,
  OperationType,
  RevisionId,
  Manifest,
  ManifestIndexes,
  AddFactInput,
  AddReferenceInput,
  AddSummaryInput,
  ArbitrationDecision,
  QueryFactsFilter,
  RecallMode,
  RecallRequest,
  RecallResult,
  CommitResult,
} from "./types/manifest.js";

// ---------------------------------------------------------------------------
// Manifest layer
// ---------------------------------------------------------------------------

export { ManifestManager } from "./manifest/manifest-manager.js";
export type {
  AddFactResult,
  AddReferenceResult,
  AddSummaryResult,
  ProposeRefinementResult,
} from "./manifest/manifest-manager.js";

export { ArbitrationEngine, CONFIDENCE_SUPERIORITY_THRESHOLD, CONVERGENCE_BONUS } from "./manifest/arbitration.js";
export { buildIndexes } from "./manifest/indexing.js";
export { deriveIdentityKey, deriveFactHash, registerStrategy } from "./manifest/identity.js";

// ---------------------------------------------------------------------------
// Context layer
// ---------------------------------------------------------------------------

export { ContextEngine } from "./context/context-engine.js";
export type {
  BuildContextResult,
  ContextEngineConfig,
  TurnResult,
  SyncReplanResult,
} from "./context/context-engine.js";

export { SlidingWindow } from "./context/sliding-window.js";
export type {
  SlidingWindowConfig,
  TurnMessage,
  Turn,
  OffloadCandidate,
} from "./context/sliding-window.js";

export { InjectionCompiler } from "./context/injection.js";
export type { InjectionConfig } from "./context/injection.js";

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

export { RecallTool, createRecallTool, formatRecallResult } from "./tools/recall-tool.js";
export type {
  RecallConfig,
  RecallError,
  RecallResponse,
} from "./tools/recall-tool.js";

// ---------------------------------------------------------------------------
// OpenClaw adapter
// ---------------------------------------------------------------------------

export {
  ManifestContextEngineAdapter,
  createManifestContextEngineAdapter,
} from "./adapters/openclaw-context-adapter.js";
export type {
  ManifestEngineAdapterConfig,
  OpenClawContextEngineInfo,
  OpenClawAssembleResult,
  OpenClawCompactResult,
} from "./adapters/openclaw-context-adapter.js";
