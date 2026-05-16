/**
 * Scenario B: Persistence Lifecycle
 *
 * Proves: Facts survive process death. The manifest is the durable truth.
 * A new ManifestManager loaded from the same path recovers full epistemic state.
 *
 * Behavioral property:
 *   Restart ≠ amnesia. The agent knows what it knew before shutdown.
 *   Confidence, lineage, observation counts, and summaries all survive.
 */

import { describe, expect, it } from "vitest";
import { useTempDir, createSession, reloadSession } from "./harness.js";

describe("Scenario B: Persistence Lifecycle", () => {
  const tmp = useTempDir();

  it("facts survive commit and reload with full fidelity", async () => {
    const s1 = await createSession(tmp.get());

    const r1 = s1.engine.addFact({
      type: "knowledge",
      value: { entity: "billing-service", fact: "Uses Stripe API v1 with idempotency keys on all charge calls" },
      confidence: 0.88,
      source: "code-analysis-tool",
    });

    expect(r1.ok).toBe(true);
    if (!r1.ok) throw new Error("addFact failed");
    const factId = r1.fact_id;

    await s1.manager.commit();

    // Simulate process death + restart.
    const s2 = await reloadSession(s1);

    const reloaded = s2.manager.getFact(factId);
    expect(reloaded).toBeDefined();
    expect(reloaded!.confidence).toBe(0.88);
    expect(reloaded!.source).toBe("code-analysis-tool");
    expect(reloaded!.value["fact"]).toContain("idempotency keys");
    expect(reloaded!.state).toBe("active");
    expect(reloaded!.observations.observation_count).toBe(1);
  });

  it("revision increments on each commit and survives reload", async () => {
    const s1 = await createSession(tmp.get());
    expect(s1.manager.getRevision()).toBe(0);

    s1.engine.addFact({
      type: "event",
      value: { what: "First fact" },
      confidence: 0.7,
      source: "test",
    });
    const commit1 = await s1.manager.commit();
    expect(commit1.ok).toBe(true);

    const s2 = await reloadSession(s1);
    expect(s2.manager.getRevision()).toBe(1);

    s2.engine.addFact({
      type: "event",
      value: { what: "Second fact" },
      confidence: 0.7,
      source: "test",
    });
    const commit2 = await s2.manager.commit();
    expect(commit2.ok).toBe(true);

    const s3 = await reloadSession(s2);
    expect(s3.manager.getRevision()).toBe(2);
  });

  it("summaries survive reload with stale flag preserved", async () => {
    const s1 = await createSession(tmp.get());

    const factResult = s1.engine.addFact({
      type: "task",
      value: { goal: "Deploy v2 endpoint" },
      confidence: 0.9,
      source: "user",
    });
    expect(factResult.ok).toBe(true);
    if (!factResult.ok) throw new Error();
    const factId = factResult.fact_id;

    s1.engine.addSummary({
      type: "session",
      scope: "current",
      key_points: ["Working on v2 endpoint deployment"],
      conflicts: [],
      recommended_next_actions: [],
      source_fact_ids: [factId],
      confidence: 0.85,
    });

    await s1.manager.commit();

    // Now supersede the source fact — summary should become stale.
    s1.engine.addFact({
      type: "task",
      value: { goal: "Deploy v2 endpoint", status: "COMPLETE" },
      confidence: 0.99,
      source: "ci-system",
    });
    await s1.manager.commit();

    const s2 = await reloadSession(s1);
    const summaries = s2.manager.getActiveSummaries("session");
    expect(summaries.length).toBeGreaterThan(0);
    const summary = summaries[0]!;
    expect(summary.stale).toBe(true);
  });

  it("revision conflict is detected when two processes diverge", async () => {
    const s1 = await createSession(tmp.get());
    s1.engine.addFact({ type: "event", value: { n: 1 }, confidence: 0.5, source: "p1" });
    await s1.manager.commit();

    // Simulate a second process loading and committing independently.
    const s2 = await reloadSession(s1);
    s2.engine.addFact({ type: "event", value: { n: 2 }, confidence: 0.5, source: "p2" });
    await s2.manager.commit(); // p2 wins, revision is now 2.

    // p1 tries to commit with stale expected revision — must fail with conflict.
    s1.engine.addFact({ type: "event", value: { n: 3 }, confidence: 0.5, source: "p1" });
    const result = await s1.manager.commit();

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected conflict");
    expect(result.reason).toBe("revision_conflict");
  });

  it("references survive restart with content integrity intact", async () => {
    const s1 = await createSession(tmp.get());
    const content = "line 1\nline 2\nline 3\nAPI response: 200 OK\nrate_limit: 1000/hr\n";

    const refResult = await s1.engine.addReference({
      source: "tool:http-probe",
      label: "API health check response",
      content,
    });
    expect(refResult.ok).toBe(true);
    if (!refResult.ok) throw new Error();
    const refId = refResult.ref_id;
    await s1.manager.commit();

    const s2 = await reloadSession(s1);
    const ref = s2.manager.getReference(refId);
    expect(ref).toBeDefined();
    expect(ref!.label).toBe("API health check response");
    expect(ref!.line_count).toBe(5);
    expect(ref!.source).toBe("tool:http-probe");
  });
});
