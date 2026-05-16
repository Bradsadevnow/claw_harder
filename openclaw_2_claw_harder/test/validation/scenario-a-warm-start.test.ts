/**
 * Scenario A: Warm Start — No Recon
 *
 * Proves: An agent given only a manifest-compiled context block can resume
 * a task without replaying conversation history.
 *
 * The key behavioral property:
 *   After restart, buildMessages() produces a context block that contains
 *   the objective, active facts, and available references — enough for the
 *   model to continue work without a single history message.
 *
 * LM Studio target: openai/gpt-oss-20b @ http://192.168.1.129:1234
 */

import { describe, expect, it } from "vitest";
import { useTempDir, createSession, reloadSession } from "./harness.js";

describe("Scenario A: Warm Start / No-Recon", () => {
  const tmp = useTempDir();

  it("operational memory survives restart with zero history", async () => {
    // --- Session 1: agent populates state ---
    const s1 = await createSession(tmp.get());

    s1.engine.addFact({
      type: "task",
      value: { goal: "Migrate the billing service to the new payment gateway", status: "in-progress", progress: "40%" },
      confidence: 0.95,
      source: "user",
    });

    s1.engine.addFact({
      type: "constraint",
      value: { rule: "Zero downtime required. Cutover must happen during off-peak hours (02:00–04:00 UTC)." },
      confidence: 1.0,
      source: "user",
    });

    s1.engine.addFact({
      type: "context",
      value: { environment: "production", region: "us-east-1", service: "billing-svc", version: "2.3.1" },
      confidence: 0.9,
      source: "tool:env-probe",
    });

    s1.engine.addSummary({
      type: "goal",
      scope: "current",
      key_points: [
        "Migrate billing-svc to Stripe v2 gateway with zero downtime",
        "Cutover window: 02:00–04:00 UTC. Currently 40% complete.",
        "Next: validate idempotency keys on the new endpoint before DNS cutover.",
      ],
      conflicts: [],
      recommended_next_actions: ["Run idempotency validation suite against staging gateway"],
      source_fact_ids: [],
      confidence: 0.92,
    });

    await s1.manager.commit();

    // --- Session 2: fresh process, zero history ---
    const s2 = await reloadSession(s1);

    // No history in window — cold start.
    const messages = s2.engine.buildMessages("You are a senior infrastructure engineer.", "");
    const opMem = messages.find(m => m.content.includes("Current Operational Memory"));

    expect(opMem).toBeDefined();
    expect(opMem!.content).toContain("Migrate billing-svc to Stripe v2");
    expect(opMem!.content).toContain("Zero downtime required");
    expect(opMem!.content).toContain("02:00–04:00 UTC");
    expect(opMem!.content).toContain("idempotency");

    // History should be empty — no replay needed.
    const historyMessages = messages.filter(
      m => m !== messages[0] && m !== messages[1] && m !== messages[messages.length - 1],
    );
    expect(historyMessages).toHaveLength(0);
  });

  it("objective is extracted and placed first in operational memory", async () => {
    const s1 = await createSession(tmp.get());

    s1.engine.addSummary({
      type: "goal",
      scope: "session",
      key_points: ["Deploy feature flag system to control rollout of payment-v2"],
      conflicts: [],
      recommended_next_actions: ["Wire LaunchDarkly SDK into checkout service"],
      source_fact_ids: [],
      confidence: 0.88,
    });

    await s1.manager.commit();

    const s2 = await reloadSession(s1);
    const messages = s2.engine.buildMessages("sys", "user");
    const opMem = messages[1]!.content;

    // Objective must appear before Active Facts.
    const objectivePos = opMem.indexOf("**Objective:**");
    const factsPos = opMem.indexOf("## Active Facts");

    expect(objectivePos).toBeGreaterThan(-1);
    if (factsPos > -1) {
      expect(objectivePos).toBeLessThan(factsPos);
    }
    expect(opMem).toContain("Deploy feature flag system");
  });

  it("constraint facts are injected before task facts when budget is tight", async () => {
    const s1 = await createSession(tmp.get(), "constraint-priority-test");

    s1.engine.addFact({
      type: "task",
      value: { goal: "Build new feature", priority: "medium" },
      confidence: 0.7,
      source: "model",
    });

    s1.engine.addFact({
      type: "constraint",
      value: { rule: "No external network calls from the sandbox environment" },
      confidence: 1.0,
      source: "user",
    });

    await s1.manager.commit();
    const s2 = await reloadSession(s1);
    const { operational_memory } = s2.engine.buildContext();

    const constraintPos = operational_memory.indexOf("constraint");
    const taskPos = operational_memory.indexOf("task");

    // Constraint must appear before task in output.
    expect(constraintPos).toBeGreaterThan(-1);
    expect(taskPos).toBeGreaterThan(-1);
    expect(constraintPos).toBeLessThan(taskPos);
  });
});
