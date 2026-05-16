/**
 * Scenario C: Fingerprint Conflict / Ambiguity Handling
 *
 * Proves: When two observations of the same entity have similar confidence,
 * the system refuses to guess. Both beliefs enter "conflicted" state.
 * The injection compiler surfaces conflict explicitly — the model sees it.
 *
 * Behavioral property:
 *   Uncertainty is a first-class state, not a hidden failure.
 *   An agent operating on conflicted beliefs knows it's operating on contested ground.
 *
 * Contrast: naive systems either pick one (silent data loss) or error (operational halt).
 * The manifest engine does neither.
 */

import { describe, expect, it } from "vitest";
import { useTempDir, createSession } from "./harness.js";
import { CONFIDENCE_SUPERIORITY_THRESHOLD } from "../../src/manifest/arbitration.js";

describe("Scenario C: Fingerprint Conflict / Ambiguity", () => {
  const tmp = useTempDir();

  it("two observations within threshold produce conflict state, not silent override", async () => {
    const s = await createSession(tmp.get());

    // First observation: prod DB is postgres 14.
    const r1 = s.engine.addFact({
      type: "knowledge",
      value: { entity: "prod-db", database: "postgres", version: "14.2" },
      confidence: 0.80,
      source: "ops-team-slack",
    });
    expect(r1.ok && r1.outcome).toBe("new");

    // Second observation: same entity, different version, confidence within threshold.
    const conflictingConfidence = 0.80 - (CONFIDENCE_SUPERIORITY_THRESHOLD / 2); // 0.725 — delta = 0.075, below threshold
    const r2 = s.engine.addFact({
      type: "knowledge",
      value: { entity: "prod-db", database: "postgres", version: "15.1" },
      confidence: conflictingConfidence,
      source: "runbook-doc",
    });

    expect(r2.ok).toBe(true);
    if (!r2.ok) throw new Error();
    expect(r2.outcome).toBe("conflict");
  });

  it("conflicted beliefs appear in the 'Conflicted Beliefs' stratum, not Active Facts", async () => {
    const s = await createSession(tmp.get());

    s.engine.addFact({
      type: "context",
      value: { service: "auth-svc", owner: "platform-team" },
      confidence: 0.75,
      source: "wiki",
    });

    s.engine.addFact({
      type: "context",
      value: { service: "auth-svc", owner: "security-team" },
      confidence: 0.72,
      source: "org-chart",
    });

    const { operational_memory } = s.engine.buildContext();

    // Must appear in conflicted section.
    expect(operational_memory).toContain("Conflicted Beliefs");
    expect(operational_memory).toContain("STATE: CONFLICTED");

    // Must NOT appear as an unqualified active fact.
    const activeSection = operational_memory.split("## Conflicted Beliefs")[0]!;
    expect(activeSection).not.toContain("platform-team");
    expect(activeSection).not.toContain("security-team");
  });

  it("getConflictedFacts includes both sides of a conflict", async () => {
    const s = await createSession(tmp.get());

    s.engine.addFact({
      type: "knowledge",
      value: { entity: "prod-db", database: "postgres", version: "14.2" },
      confidence: 0.8,
      source: "ops-team-slack",
    });

    const second = s.engine.addFact({
      type: "knowledge",
      value: { entity: "prod-db", database: "postgres", version: "15.1" },
      confidence: 0.75,
      source: "runbook-doc",
    });

    expect(second.ok).toBe(true);
    if (!second.ok) throw new Error();
    expect(second.outcome).toBe("conflict");

    const conflicted = s.manager.getConflictedFacts();
    expect(conflicted).toHaveLength(2);

    const versions = conflicted
      .map((fact) => String(fact.value["version"] ?? ""))
      .sort();
    expect(versions).toEqual(["14.2", "15.1"]);
  });

  it("high-confidence win supersedes, does not conflict", async () => {
    const s = await createSession(tmp.get());

    s.engine.addFact({
      type: "knowledge",
      value: { entity: "cache", ttl_seconds: 300 },
      confidence: 0.60,
      source: "guess",
    });

    // Clear superiority — delta well above threshold.
    const r2 = s.engine.addFact({
      type: "knowledge",
      value: { entity: "cache", ttl_seconds: 900 },
      confidence: 0.90,
      source: "config-file",
    });

    expect(r2.ok).toBe(true);
    if (!r2.ok) throw new Error();
    expect(r2.outcome).toBe("supersede");

    // No conflicted facts — clean state.
    const conflicted = s.manager.getConflictedFacts();
    expect(conflicted).toHaveLength(0);
  });

  it("convergence strengthens confidence without creating new facts", async () => {
    const s = await createSession(tmp.get());

    const r1 = s.engine.addFact({
      type: "preference",
      value: { style: "typescript-strict", linting: "eslint" },
      confidence: 0.70,
      source: "user",
    });
    expect(r1.ok && r1.outcome).toBe("new");
    if (!r1.ok) throw new Error();
    const factId = r1.fact_id;

    // Same value observed again — should converge, not create a duplicate.
    const r2 = s.engine.addFact({
      type: "preference",
      value: { style: "typescript-strict", linting: "eslint" },
      confidence: 0.70,
      source: "config-file",
    });
    expect(r2.ok && r2.outcome).toBe("converge");

    const fact = s.manager.getFact(factId)!;
    expect(fact.observations.observation_count).toBe(2);
    expect(fact.confidence).toBeGreaterThan(0.70); // convergence bonus applied
    expect(fact.supporting_sources).toContain("config-file");
  });

  it("proposeRefinement forces resolution of a conflict", async () => {
    const s = await createSession(tmp.get());

    // Create conflict.
    s.engine.addFact({
      type: "knowledge",
      value: { entity: "rate-limit", requests_per_minute: 100 },
      confidence: 0.70,
      source: "docs-v1",
    });

    const r2 = s.engine.addFact({
      type: "knowledge",
      value: { entity: "rate-limit", requests_per_minute: 150 },
      confidence: 0.68,
      source: "docs-v2",
    });
    expect(r2.ok && r2.outcome).toBe("conflict");
    if (!r2.ok) throw new Error();

    // Operator manually resolves by proposing a refinement.
    const conflict = s.manager.getConflictedFacts();
    expect(conflict.length).toBeGreaterThan(0);

    const resolution = s.engine.proposeRefinement(conflict[0]!.fact_id, {
      type: "knowledge",
      value: { entity: "rate-limit", requests_per_minute: 150, verified_by: "load-test" },
      confidence: 0.97,
      source: "production-measurement",
    });

    expect(resolution.ok).toBe(true);

    // Conflict should be resolved — no more conflicted facts.
    const remaining = s.manager.getConflictedFacts();
    expect(remaining).toHaveLength(0);
  });
});
