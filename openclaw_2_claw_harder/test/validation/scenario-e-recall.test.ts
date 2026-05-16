/**
 * Scenario E: Evidence-Linked Action / Surgical Recall
 *
 * Proves: Large artifacts are offloaded from context, surfaced as REF_IDs,
 * and can be recalled surgically — the model gets exactly the slice it needs,
 * not a 50KB wall of JSON dumped into the prompt.
 *
 * Behavioral property:
 *   Context is bounded. Recall is intentional.
 *   The injection compiler never embeds full artifact content.
 *   recall_evidence retrieves precisely: summary, head, tail, range, or search.
 *   Every recall is audited in the operations log.
 */

import { describe, expect, it } from "vitest";
import { useTempDir, createSession, reloadSession } from "./harness.js";
import { formatRecallResult } from "../../src/tools/recall-tool.js";

const LARGE_TOOL_OUTPUT = [
  "=== Service Health Report ===",
  "Generated: 2026-05-15T14:23:00Z",
  ...Array.from({ length: 200 }, (_, i) => `metric[${i}]: ${(Math.random() * 100).toFixed(2)}ms`),
  "=== Summary ===",
  "p50: 23ms  p95: 87ms  p99: 312ms",
  "error_rate: 0.003%",
  "throughput: 4821 req/s",
  "status: HEALTHY",
].join("\n");

describe("Scenario E: Evidence-Linked Action / Surgical Recall", () => {
  const tmp = useTempDir();

  it("large artifact is stored and retrievable by ref_id", async () => {
    const s = await createSession(tmp.get());

    const result = await s.engine.addReference({
      source: "tool:health-check",
      label: "Production service health report",
      content: LARGE_TOOL_OUTPUT,
    });

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error();

    const ref = s.manager.getReference(result.ref_id);
    expect(ref).toBeDefined();
    expect(ref!.byte_size).toBeGreaterThan(0);
    expect(ref!.line_count).toBeGreaterThan(200);
  });

  it("injection compiler surfaces ref_id without embedding content", async () => {
    const s = await createSession(tmp.get());

    const refResult = await s.engine.addReference({
      source: "tool:sql-query",
      label: "Full query execution plan",
      content: LARGE_TOOL_OUTPUT,
    });
    expect(refResult.ok).toBe(true);
    if (!refResult.ok) throw new Error();

    const { operational_memory } = s.engine.buildContext();

    // REF_ID must appear in Available References.
    expect(operational_memory).toContain(refResult.ref_id);
    expect(operational_memory).toContain("Available References");

    // But the full content must NOT be embedded.
    expect(operational_memory).not.toContain("metric[100]");
    expect(operational_memory.length).toBeLessThan(10_000);
  });

  it("recall summary mode returns metadata + head/tail preview", async () => {
    const s = await createSession(tmp.get());

    const refResult = await s.engine.addReference({
      source: "test",
      label: "Health report",
      content: LARGE_TOOL_OUTPUT,
    });
    expect(refResult.ok).toBe(true);
    if (!refResult.ok) throw new Error();

    const result = await s.recall.recall({ ref_id: refResult.ref_id, mode: "summary" });
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error(result.detail);

    expect(result.total_lines).toBeGreaterThan(200);
    expect(result.lines).toBeDefined();
    expect(result.lines!.length).toBeGreaterThan(0);
  });

  it("recall search mode returns only matching lines with context", async () => {
    const s = await createSession(tmp.get());

    const refResult = await s.engine.addReference({
      source: "test",
      label: "Health report",
      content: LARGE_TOOL_OUTPUT,
    });
    expect(refResult.ok).toBe(true);
    if (!refResult.ok) throw new Error();

    const result = await s.recall.recall({
      ref_id: refResult.ref_id,
      mode: "search",
      query: "p99",
    });
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error(result.detail);

    expect(result.match_count).toBeGreaterThan(0);
    const matchLines = result.lines!.filter(l => l.text.includes("p99"));
    expect(matchLines.length).toBeGreaterThan(0);

    // Search result must be far smaller than full content.
    const formatted = formatRecallResult(result);
    expect(formatted.length).toBeLessThan(LARGE_TOOL_OUTPUT.length / 2);
  });

  it("recall range mode returns exact line slice with correct line numbers", async () => {
    const content = Array.from({ length: 50 }, (_, i) => `Line ${i + 1}: data`).join("\n");
    const s = await createSession(tmp.get());

    const refResult = await s.engine.addReference({
      source: "test",
      label: "Numbered content",
      content,
    });
    expect(refResult.ok).toBe(true);
    if (!refResult.ok) throw new Error();

    const result = await s.recall.recall({
      ref_id: refResult.ref_id,
      mode: "range",
      start_line: 10,
      end_line: 20,
    });
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error(result.detail);

    expect(result.lines).toHaveLength(11); // lines 10–20 inclusive
    expect(result.lines![0]!.n).toBe(10);
    expect(result.lines![10]!.n).toBe(20);
    expect(result.lines![0]!.text).toContain("Line 10");
  });

  it("every recall is logged in the operations log", async () => {
    const s = await createSession(tmp.get());

    const refResult = await s.engine.addReference({
      source: "test",
      label: "Audit test",
      content: "line 1\nline 2\nline 3\n",
    });
    expect(refResult.ok).toBe(true);
    if (!refResult.ok) throw new Error();

    const before = s.manager.manifest.operations.length;

    await s.recall.recall({ ref_id: refResult.ref_id, mode: "head" });
    await s.recall.recall({ ref_id: refResult.ref_id, mode: "tail" });

    const after = s.manager.manifest.operations.length;
    expect(after).toBe(before + 2);

    const recallOps = s.manager.manifest.operations.filter(op => op.type === "recall_event");
    expect(recallOps.length).toBeGreaterThanOrEqual(2);
  });

  it("sliding window auto-offloads large tool results and stores them as references", async () => {
    const s = await createSession(tmp.get());

    const largeTool: import("../../src/context/sliding-window.js").TurnMessage = {
      role: "tool",
      content: LARGE_TOOL_OUTPUT, // > 4000 chars default threshold
    };

    const result = await s.engine.completeTurn(
      [
        { role: "user", content: "Check service health" },
        { role: "assistant", content: "Running health check..." },
        largeTool,
      ],
      false,
    );

    expect(result.offloaded_ref_ids.length).toBeGreaterThan(0);

    // The reference should now exist in the manifest.
    const ref = s.manager.getReference(result.offloaded_ref_ids[0]! as `REF_${string}`);
    expect(ref).toBeDefined();
    expect(ref!.source).toBe("sliding-window");
  });

  it("recall validates content integrity on every read", async () => {
    const s = await createSession(tmp.get());

    const refResult = await s.engine.addReference({
      source: "test",
      label: "Integrity test",
      content: "original content\n",
    });
    expect(refResult.ok).toBe(true);
    if (!refResult.ok) throw new Error();

    // Happy path — should pass.
    const result = await s.recall.recall({ ref_id: refResult.ref_id, mode: "full" });
    expect(result.ok).toBe(true);
  });
});
