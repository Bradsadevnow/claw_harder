/**
 * Scenario D: Objective Handoff
 *
 * Proves: An agent can resume a task across process boundaries without
 * asking "what were we doing?" The objective is always the first thing
 * the model sees after restart.
 *
 * Behavioral property:
 *   The manifest carries purpose, not just data.
 *   Goal summaries are extracted and surfaced at the top of every context build.
 *   Stale summaries are flagged — the model knows when its map is outdated.
 */

import { describe, expect, it } from "vitest";
import { useTempDir, createSession, reloadSession } from "./harness.js";

describe("Scenario D: Objective Handoff", () => {
  const tmp = useTempDir();

  it("goal summary objective survives restart and leads the context block", async () => {
    const s1 = await createSession(tmp.get());

    s1.engine.addSummary({
      type: "goal",
      scope: "current",
      key_points: [
        "Complete the API gateway migration before the Thursday 18:00 UTC deadline",
        "Current blocker: rate limiter config incompatibility between v1 and v2",
        "Completed: auth middleware, request routing, error normalization",
      ],
      conflicts: [],
      recommended_next_actions: ["Resolve rate limiter config discrepancy with platform team"],
      source_fact_ids: [],
      confidence: 0.91,
    });

    await s1.manager.commit();

    // Cold restart — zero history.
    const s2 = await reloadSession(s1);
    const { operational_memory } = s2.engine.buildContext();

    expect(operational_memory).toContain("**Objective:**");
    expect(operational_memory).toContain("API gateway migration");
    expect(operational_memory).toContain("Thursday");

    // Objective must lead the block.
    const objectiveIdx = operational_memory.indexOf("**Objective:**");
    const summaryIdx = operational_memory.indexOf("## Summaries");
    expect(objectiveIdx).toBeLessThan(summaryIdx > -1 ? summaryIdx : Infinity);
  });

  it("stale goal summary is flagged when source facts change", async () => {
    const s1 = await createSession(tmp.get());

    const taskResult = s1.engine.addFact({
      type: "task",
      value: { goal: "Fix authentication bug in user service" },
      confidence: 0.88,
      source: "user",
    });
    expect(taskResult.ok).toBe(true);
    if (!taskResult.ok) throw new Error();

    s1.engine.addSummary({
      type: "goal",
      scope: "current",
      key_points: ["Fix auth bug — user tokens expiring prematurely"],
      conflicts: [],
      recommended_next_actions: ["Inspect JWT middleware TTL config"],
      source_fact_ids: [taskResult.fact_id],
      confidence: 0.88,
    });

    await s1.manager.commit();

    // New fact supersedes the source — summary becomes stale.
    s1.engine.addFact({
      type: "task",
      value: { goal: "Fix authentication bug in user service", status: "RESOLVED", pr: "#4821" },
      confidence: 0.99,
      source: "github-ci",
    });
    await s1.manager.commit();

    const s2 = await reloadSession(s1);
    const { operational_memory } = s2.engine.buildContext();

    // Stale flag must appear in the objective line or summaries.
    expect(
      operational_memory.includes("[STALE") || operational_memory.includes("STALE"),
    ).toBe(true);
  });

  it("task fact falls back as objective when no goal summary exists", async () => {
    const s1 = await createSession(tmp.get());

    s1.engine.addFact({
      type: "task",
      value: { goal: "Deploy canary release of payment-v2 to 5% of traffic" },
      confidence: 0.92,
      source: "product-manager",
    });

    await s1.manager.commit();

    const s2 = await reloadSession(s1);
    const { operational_memory } = s2.engine.buildContext();

    expect(operational_memory).toContain("**Objective:**");
    expect(operational_memory).toContain("canary release");
  });

  it("multiple sessions share no state — manifests are isolated", async () => {
    const { mkdtemp } = await import("node:fs/promises");
    const { tmpdir } = await import("node:os");
    const { join } = await import("node:path");

    const dirA = await mkdtemp(join(tmpdir(), "claw2-a-"));
    const dirB = await mkdtemp(join(tmpdir(), "claw2-b-"));

    try {
      const sA = await createSession(dirA, "agent-A");
      sA.engine.addFact({ type: "task", value: { goal: "Session A task" }, confidence: 0.9, source: "a" });
      await sA.manager.commit();

      const sB = await createSession(dirB, "agent-B");
      sB.engine.addFact({ type: "task", value: { goal: "Session B task" }, confidence: 0.9, source: "b" });
      await sB.manager.commit();

      const rA = await reloadSession(sA);
      const rB = await reloadSession(sB);

      const memA = rA.engine.buildContext().operational_memory;
      const memB = rB.engine.buildContext().operational_memory;

      expect(memA).toContain("Session A task");
      expect(memA).not.toContain("Session B task");

      expect(memB).toContain("Session B task");
      expect(memB).not.toContain("Session A task");
    } finally {
      const { rm } = await import("node:fs/promises");
      await rm(dirA, { recursive: true, force: true });
      await rm(dirB, { recursive: true, force: true });
    }
  });
});
