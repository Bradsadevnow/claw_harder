/**
 * Shared test harness for validation scenarios.
 *
 * Creates isolated per-test ManifestManager instances in temp directories.
 * No shared state between scenarios.
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach } from "vitest";
import { ManifestManager } from "../../src/manifest/manifest-manager.js";
import { ContextEngine } from "../../src/context/context-engine.js";
import { RecallTool } from "../../src/tools/recall-tool.js";

export interface TestSession {
  dir: string;
  manifestPath: string;
  artifactsDir: string;
  manager: ManifestManager;
  engine: ContextEngine;
  recall: RecallTool;
}

export async function createSession(dir: string, actor = "test"): Promise<TestSession> {
  const manifestPath = join(dir, "manifest.json");
  const artifactsDir = join(dir, "artifacts");
  const manager = await ManifestManager.load(manifestPath, artifactsDir, actor);
  const engine = new ContextEngine(manager, { auto_commit: false });
  const recall = new RecallTool(manager);
  return { dir, manifestPath, artifactsDir, manager, engine, recall };
}

/** Reload an existing session from disk — simulates process restart. */
export async function reloadSession(existing: TestSession, actor = "test"): Promise<TestSession> {
  return createSession(existing.dir, actor);
}

/** Vitest lifecycle helper — create a temp dir and clean it up after the test. */
export function useTempDir(): { get: () => string } {
  let dir = "";
  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), "claw2-"));
  });
  afterEach(async () => {
    if (dir) await rm(dir, { recursive: true, force: true });
  });
  return { get: () => dir };
}
