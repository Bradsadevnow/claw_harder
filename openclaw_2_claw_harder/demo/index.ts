/**
 * OPENCLAW II: CLAW HARDER — Demo
 *
 * Demonstrates four core behavioral properties:
 *   1. Restart continuity   — manifest survives process death
 *   2. recall_evidence      — surgical artifact retrieval
 *   3. Conflict handling    — uncertainty as first-class state
 *   4. Bounded context      — sliding window + auto-offload
 *
 * Run: node --loader ts-node/esm demo/index.ts
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ManifestManager } from "../src/manifest/manifest-manager.js";
import { ContextEngine } from "../src/context/context-engine.js";
import { RecallTool, formatRecallResult } from "../src/tools/recall-tool.js";
import type { TurnMessage } from "../src/context/sliding-window.js";

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

const hr = (title: string) => console.log(`\n${"═".repeat(60)}\n  ${title}\n${"═".repeat(60)}`);
const log = (label: string, val: string) => {
  console.log(`\n▶ ${label}:`);
  console.log(val.split("\n").map(l => `  ${l}`).join("\n"));
};

async function makeSession(dir: string, actor = "demo") {
  const manager = await ManifestManager.load(
    join(dir, "manifest.json"),
    join(dir, "artifacts"),
    actor,
  );
  const engine = new ContextEngine(manager, { auto_commit: false });
  const recall = new RecallTool(manager);
  return { manager, engine, recall };
}

// ---------------------------------------------------------------------------
// Demo 1: Restart Continuity
// ---------------------------------------------------------------------------

async function demoRestartContinuity(dir: string) {
  hr("DEMO 1: Restart Continuity");
  console.log("  An agent populates state, 'dies', and restarts.");
  console.log("  The manifest carries the session forward — no replay needed.\n");

  // Session 1: agent does work
  const s1 = await makeSession(dir, "session-1");

  s1.engine.addFact({
    type: "task",
    value: { goal: "Deploy payment-v2 to production", status: "in-progress", progress: "60%" },
    confidence: 0.95,
    source: "product-manager",
  });

  s1.engine.addFact({
    type: "constraint",
    value: { rule: "No deploys during peak hours (13:00–19:00 UTC). Today is Thursday." },
    confidence: 1.0,
    source: "ops-policy",
  });

  s1.engine.addFact({
    type: "context",
    value: { environment: "production", cluster: "us-east-1a", replicas: 6 },
    confidence: 0.9,
    source: "kubectl",
  });

  s1.engine.addSummary({
    type: "goal",
    scope: "current",
    key_points: [
      "Deploy payment-v2 to production (60% complete)",
      "Blocked by peak-hour policy until 19:00 UTC",
      "Remaining: canary validation + DNS cutover",
    ],
    conflicts: [],
    recommended_next_actions: ["Run canary health checks. Deploy after 19:00 UTC."],
    source_fact_ids: [],
    confidence: 0.91,
  });

  await s1.manager.commit();
  console.log("  [Session 1] Agent committed state. Revision:", s1.manager.getRevision());
  console.log("  [Session 1] Simulating process exit...\n");

  // Session 2: cold restart, zero history
  const s2 = await makeSession(dir, "session-2");
  console.log("  [Session 2] New process. No history. Loading manifest...");
  console.log("  [Session 2] Revision on disk:", s2.manager.getRevision());

  const messages = s2.engine.buildMessages(
    "You are a senior infrastructure engineer.",
    "What should we do next?",
  );

  const opMem = messages[1]!.content;
  log("Operational memory on cold start", opMem);

  console.log(`\n  History messages in window: ${messages.length - 3} (expected: 0)`);
  console.log("  ✓ Objective, constraints, and context available without replay.");
}

// ---------------------------------------------------------------------------
// Demo 2: recall_evidence
// ---------------------------------------------------------------------------

async function demoRecallEvidence(dir: string) {
  hr("DEMO 2: recall_evidence — Surgical Retrieval");
  console.log("  Large tool outputs are offloaded. The model sees REF_IDs, not walls of JSON.");
  console.log("  recall_evidence retrieves exactly what's needed.\n");

  const { manager, engine, recall } = await makeSession(dir, "recall-demo");

  // Simulate a large API response
  const apiResponse = [
    "HTTP/1.1 200 OK",
    "Content-Type: application/json",
    "X-Rate-Limit-Remaining: 847",
    "",
    JSON.stringify({
      services: Array.from({ length: 50 }, (_, i) => ({
        id: `svc-${i.toString().padStart(3, "0")}`,
        status: i % 7 === 0 ? "DEGRADED" : "HEALTHY",
        latency_p99_ms: Math.round(50 + Math.random() * 300),
        error_rate: i % 7 === 0 ? 0.043 : 0.001,
      })),
    }, null, 2),
  ].join("\n");

  const refResult = await engine.addReference({
    source: "tool:service-mesh-probe",
    label: "Service mesh health check — all 50 services",
    content: apiResponse,
  });

  if (!refResult.ok) throw new Error("addReference failed");

  const { operational_memory } = engine.buildContext();
  console.log("  Context block size:", operational_memory.length, "chars (full API response is", apiResponse.length, "chars)");
  console.log("  REF_ID in context:", operational_memory.includes(refResult.ref_id));
  console.log("  Full content in context:", operational_memory.includes('"latency_p99_ms"'));

  // Search for degraded services
  const searchResult = await recall.recall({
    ref_id: refResult.ref_id,
    mode: "search",
    query: "DEGRADED",
  });

  log(`recall_evidence (search: "DEGRADED")`, formatRecallResult(searchResult));

  // Summary mode
  const summaryResult = await recall.recall({ ref_id: refResult.ref_id, mode: "summary" });
  log("recall_evidence (summary)", formatRecallResult(summaryResult));
}

// ---------------------------------------------------------------------------
// Demo 3: Conflict Handling
// ---------------------------------------------------------------------------

async function demoConflictHandling(dir: string) {
  hr("DEMO 3: Conflict Handling — Uncertainty as First-Class State");
  console.log("  Two observations of the same entity with similar confidence.");
  console.log("  Neither wins. Both enter conflicted state. The model sees it explicitly.\n");

  const { engine } = await makeSession(dir, "conflict-demo");

  engine.addFact({
    type: "knowledge",
    value: { entity: "prod-db", host: "db-primary.internal", port: 5432 },
    confidence: 0.78,
    source: "ops-wiki",
  });

  const r2 = engine.addFact({
    type: "knowledge",
    value: { entity: "prod-db", host: "db-replica.internal", port: 5433 },
    confidence: 0.75,
    source: "recent-runbook",
  });

  console.log("  Arbitration outcome:", r2.ok ? r2.outcome : "error");

  const { operational_memory } = engine.buildContext();

  const hasConflict = operational_memory.includes("Conflicted Beliefs");
  const notInActive = !operational_memory.split("## Conflicted")[0]!.includes("db-primary");

  console.log("  'Conflicted Beliefs' section present:", hasConflict);
  console.log("  Conflicted facts excluded from Active Facts:", notInActive);

  const conflictStart = operational_memory.indexOf("## Conflicted Beliefs");
  const conflictEnd = operational_memory.indexOf("\n## ", conflictStart + 1);
  const conflictSection = operational_memory.slice(
    conflictStart,
    conflictEnd > -1 ? conflictEnd : undefined,
  );
  log("Conflicted Beliefs section", conflictSection);

  // Operator resolves via proposeRefinement
  const conflicted = engine.queryFacts({ state: "conflicted" });
  if (conflicted.length > 0) {
    const resolution = engine.proposeRefinement(conflicted[0]!.fact_id, {
      type: "knowledge",
      value: {
        entity: "prod-db",
        host: "db-primary.internal",
        port: 5432,
        note: "primary for writes; replica on 5433 for reads",
        verified_by: "DBA-team-2026-05-15",
      },
      confidence: 0.97,
      source: "dba-confirmation",
    });
    console.log("\n  proposeRefinement result:", resolution.ok ? "✓ resolved" : "✗ failed");
    console.log("  Conflicted facts remaining:", engine.queryFacts({ state: "conflicted" }).length);
  }
}

// ---------------------------------------------------------------------------
// Demo 4: Bounded Context
// ---------------------------------------------------------------------------

async function demoBoundedContext(dir: string) {
  hr("DEMO 4: Bounded Context — Sliding Window + Auto-Offload");
  console.log("  The window evicts old turns as sessions grow.");
  console.log("  Large tool outputs are offloaded automatically.\n");

  const { engine } = await makeSession(dir, "window-demo");

  // Push 6 turns (window max = 5)
  for (let i = 1; i <= 6; i++) {
    const largeToolOutput = i === 3
      ? "metric: " + "x".repeat(5000) // > 4000 chars threshold
      : `Tool result for turn ${i}`;

    const turn: TurnMessage[] = [
      { role: "user", content: `Task ${i}: do something` },
      { role: "assistant", content: `Completed task ${i}` },
      { role: "tool", content: largeToolOutput },
    ];

    const result = await engine.completeTurn(turn, false);

    const offloads = result.offloaded_ref_ids.length > 0
      ? `→ offloaded ${result.offloaded_ref_ids[0]}`
      : "";
    const evictions = result.evicted_turns > 0
      ? `→ evicted ${result.evicted_turns} turn(s)`
      : "";

    console.log(`  Turn ${i}: window=${engine.windowStats.turn_count} turns, ${engine.windowStats.total_tokens} tokens ${offloads} ${evictions}`);
  }

  const stats = engine.windowStats;
  console.log("\n  Final window stats:");
  console.log(`    turns: ${stats.turn_count} (max 5)`);
  console.log(`    tokens: ${stats.total_tokens}`);
  console.log(`    oldest turn: ${stats.oldest_turn_index}`);
  console.log("  ✓ Window bounded. Large output offloaded. Old turns evicted.");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const dir = await mkdtemp(join(tmpdir(), "claw2-demo-"));

  try {
    console.log("\n  OPENCLAW II: CLAW HARDER");
    console.log("  Manifest-driven state substrate for OpenClaw agents\n");

    await demoRestartContinuity(join(dir, "demo1"));
    await demoRecallEvidence(join(dir, "demo2"));
    await demoConflictHandling(join(dir, "demo3"));
    await demoBoundedContext(join(dir, "demo4"));

    console.log("\n" + "═".repeat(60));
    console.log("  All demos complete.");
    console.log("═".repeat(60) + "\n");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
