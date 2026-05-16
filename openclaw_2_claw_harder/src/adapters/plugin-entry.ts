/**
 * OpenClaw plugin entry point.
 *
 * Called by OpenClaw's plugin loader when the plugin is activated.
 * Registers the manifest-engine context engine via the plugin SDK API.
 *
 * To enable:
 *   openclaw plugins install openclaw-manifest-engine
 *   # then in your openclaw config:
 *   plugins:
 *     slots:
 *       contextEngine: manifest-engine
 */

import {
  createManifestContextEngineAdapter,
  type ManifestEngineAdapterConfig,
} from "./openclaw-context-adapter.js";

// Register once at module scope — the plugin loader calls register() once per activation.
let adapter: ReturnType<typeof createManifestContextEngineAdapter> | null = null;

function getOrCreateAdapter(config?: Partial<ManifestEngineAdapterConfig>) {
  adapter ??= createManifestContextEngineAdapter(config);
  return adapter;
}

function parsePositiveInt(value: unknown, minimum: number): number | undefined {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum) {
    return undefined;
  }
  return value;
}

function toAdapterConfig(pluginConfig: unknown): Partial<ManifestEngineAdapterConfig> {
  if (typeof pluginConfig !== "object" || pluginConfig === null) {
    return {};
  }

  const raw = pluginConfig as Record<string, unknown>;
  const manifestRootDir =
    typeof raw["manifestRootDir"] === "string" && raw["manifestRootDir"].trim() !== ""
      ? raw["manifestRootDir"]
      : undefined;

  const maxContextChars = parsePositiveInt(raw["maxContextChars"], 1000);
  const maxFacts = parsePositiveInt(raw["maxFacts"], 1);
  const maxSummaries = parsePositiveInt(raw["maxSummaries"], 1);
  const maxWindowTokens = parsePositiveInt(raw["maxWindowTokens"], 100);
  const maxWindowTurns = parsePositiveInt(raw["maxWindowTurns"], 1);

  const injection: NonNullable<ManifestEngineAdapterConfig["contextEngine"]>["injection"] = {};
  if (maxContextChars !== undefined) injection.max_chars = maxContextChars;
  if (maxFacts !== undefined) injection.max_facts = maxFacts;
  if (maxSummaries !== undefined) injection.max_summaries = maxSummaries;

  const sliding_window: NonNullable<ManifestEngineAdapterConfig["contextEngine"]>["sliding_window"] = {};
  if (maxWindowTokens !== undefined) sliding_window.max_tokens = maxWindowTokens;
  if (maxWindowTurns !== undefined) sliding_window.max_turns = maxWindowTurns;

  const config: Partial<ManifestEngineAdapterConfig> = {};
  if (manifestRootDir !== undefined) {
    config.manifestRootDir = manifestRootDir;
  }
  if (Object.keys(injection).length > 0 || Object.keys(sliding_window).length > 0) {
    config.contextEngine = {};
    if (Object.keys(injection).length > 0) config.contextEngine.injection = injection;
    if (Object.keys(sliding_window).length > 0) config.contextEngine.sliding_window = sliding_window;
  }

  return config;
}

/**
 * OpenClaw plugin module export.
 *
 * The register function receives the plugin API and registers the context engine.
 * This is the synchronous registration path — the engine itself is lazy (bootstrapped
 * per-session).
 */
export function register(api: {
  registerContextEngine: (id: string, factory: () => unknown) => void;
  pluginConfig?: Record<string, unknown>;
}) {
  const engineAdapter = getOrCreateAdapter(toAdapterConfig(api.pluginConfig));
  api.registerContextEngine(engineAdapter.info.id, () => engineAdapter);
}
