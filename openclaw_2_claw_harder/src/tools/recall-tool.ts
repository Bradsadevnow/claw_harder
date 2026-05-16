/**
 * recall_evidence — surgical artifact retrieval for agents.
 *
 * Modes:
 *   summary  — metadata + first/last N lines (never full content)
 *   head     — first N lines (default 40)
 *   tail     — last N lines (default 40)
 *   range    — lines [start_line, end_line] (1-based, inclusive)
 *   full     — entire artifact, size-capped (default 2000 lines / ~200KB)
 *   search   — line-by-line grep with context lines around each match
 *
 * Every recall is logged as a "recall_event" operation in the manifest.
 * This makes artifact access auditable and replayable.
 *
 * Size caps prevent agents from accidentally pulling multi-MB files into context.
 * All modes return line-numbered output for stable addressing in follow-up calls.
 */

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import type { ManifestManager } from "../manifest/manifest-manager.js";
import type { RecallMode, RecallRequest, RecallResult, RefId } from "../types/manifest.js";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

export interface RecallConfig {
  /** Default lines returned by head/tail modes. Default: 40. */
  default_lines: number;
  /** Max lines returned by full mode. Default: 2000. */
  full_mode_max_lines: number;
  /** Max bytes returned by full mode. Default: 200_000 (200KB). */
  full_mode_max_bytes: number;
  /** Lines of context around each search match. Default: 2. */
  search_context_lines: number;
  /** Max matches returned by search. Default: 30. */
  search_max_matches: number;
}

const RECALL_DEFAULTS: RecallConfig = {
  default_lines: 40,
  full_mode_max_lines: 2000,
  full_mode_max_bytes: 200_000,
  search_context_lines: 2,
  search_max_matches: 30,
};

// ---------------------------------------------------------------------------
// RecallError — typed failure, not exception-based
// ---------------------------------------------------------------------------

export interface RecallError {
  ok: false;
  ref_id: RefId;
  reason: "not_found" | "artifact_missing" | "hash_mismatch" | "invalid_request" | "io_error";
  detail: string;
}

export type RecallResponse = ({ ok: true } & RecallResult) | RecallError;

// ---------------------------------------------------------------------------
// RecallTool
// ---------------------------------------------------------------------------

export class RecallTool {
  readonly config: RecallConfig;

  constructor(
    private readonly manager: ManifestManager,
    config: Partial<RecallConfig> = {},
  ) {
    this.config = { ...RECALL_DEFAULTS, ...config };
  }

  /**
   * Primary entry point. Returns a typed response — never throws.
   */
  async recall(request: RecallRequest): Promise<RecallResponse> {
    const { ref_id, mode } = request;

    // Validate mode-specific params up front.
    const validation = this.validateRequest(request);
    if (validation) {
      return { ok: false, ref_id, reason: "invalid_request", detail: validation };
    }

    // Look up the reference in the manifest.
    const ref = this.manager.getReference(ref_id);
    if (!ref) {
      return { ok: false, ref_id, reason: "not_found", detail: `No reference found for ${ref_id}` };
    }

    // Read artifact from disk.
    let raw: string;
    try {
      raw = await readFile(ref.path, "utf8");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return { ok: false, ref_id, reason: "artifact_missing", detail: `Failed to read ${ref.path}: ${msg}` };
    }

    // Integrity check — verify content hasn't changed since storage.
    const currentHash = createHash("sha256").update(raw).digest("hex");
    if (currentHash !== ref.content_hash) {
      this.manager.recordOperation("recall_event", {
        ref_id,
        mode,
        outcome: "hash_mismatch",
        expected: ref.content_hash,
        actual: currentHash,
      });
      return {
        ok: false,
        ref_id,
        reason: "hash_mismatch",
        detail: `Artifact integrity check failed for ${ref_id}. Content may have been modified.`,
      };
    }

    const allLines = raw.split("\n");
    const totalLines = allLines.length;

    // Execute the requested mode.
    let result: RecallResult;
    switch (mode) {
      case "summary":
        result = this.execSummary(ref_id, ref.label, ref.source, ref.created_at, allLines, raw.length);
        break;
      case "head":
        result = this.execHead(ref_id, ref.label, ref.source, ref.created_at, allLines, request);
        break;
      case "tail":
        result = this.execTail(ref_id, ref.label, ref.source, ref.created_at, allLines, request);
        break;
      case "range":
        result = this.execRange(ref_id, ref.label, ref.source, ref.created_at, allLines, request);
        break;
      case "full":
        result = this.execFull(ref_id, ref.label, ref.source, ref.created_at, allLines, raw.length);
        break;
      case "search":
        result = this.execSearch(ref_id, ref.label, ref.source, ref.created_at, allLines, request);
        break;
      default: {
        const _exhaustive: never = mode;
        return { ok: false, ref_id, reason: "invalid_request", detail: `Unknown mode: ${String(_exhaustive)}` };
      }
    }

    // Audit log — every recall is recorded.
    this.manager.recordOperation("recall_event", {
      ref_id,
      mode,
      outcome: "success",
      lines_returned: result.lines?.length ?? 0,
      match_count: result.match_count,
    });

    return { ok: true, ...result };
  }

  // -------------------------------------------------------------------------
  // Mode executors
  // -------------------------------------------------------------------------

  private execSummary(
    ref_id: RefId,
    label: string,
    source: string,
    created_at: string,
    allLines: string[],
    byteSize: number,
  ): RecallResult {
    const totalLines = allLines.length;
    const n = Math.min(this.config.default_lines, Math.floor(totalLines / 2));

    const previewLines: Array<{ n: number; text: string }> = [];
    // Head preview
    for (let i = 0; i < n && i < totalLines; i++) {
      previewLines.push({ n: i + 1, text: allLines[i] ?? "" });
    }
    // Separator if content exists in between
    if (n * 2 < totalLines) {
      previewLines.push({ n: -1, text: `... [${totalLines - n * 2} lines omitted] ...` });
    }
    // Tail preview
    const tailStart = Math.max(n, totalLines - n);
    for (let i = tailStart; i < totalLines; i++) {
      previewLines.push({ n: i + 1, text: allLines[i] ?? "" });
    }

    return {
      ref_id,
      mode: "summary",
      label,
      total_lines: totalLines,
      byte_size: byteSize,
      source,
      created_at,
      lines: previewLines,
    };
  }

  private execHead(
    ref_id: RefId,
    label: string,
    source: string,
    created_at: string,
    allLines: string[],
    request: RecallRequest,
  ): RecallResult {
    const count = request.end_line ?? this.config.default_lines;
    const slice = allLines.slice(0, count);
    return {
      ref_id,
      mode: "head",
      label,
      total_lines: allLines.length,
      byte_size: slice.join("\n").length,
      source,
      created_at,
      lines: slice.map((text, i) => ({ n: i + 1, text })),
    };
  }

  private execTail(
    ref_id: RefId,
    label: string,
    source: string,
    created_at: string,
    allLines: string[],
    request: RecallRequest,
  ): RecallResult {
    const count = request.start_line ?? this.config.default_lines;
    const from = Math.max(0, allLines.length - count);
    const slice = allLines.slice(from);
    return {
      ref_id,
      mode: "tail",
      label,
      total_lines: allLines.length,
      byte_size: slice.join("\n").length,
      source,
      created_at,
      lines: slice.map((text, i) => ({ n: from + i + 1, text })),
    };
  }

  private execRange(
    ref_id: RefId,
    label: string,
    source: string,
    created_at: string,
    allLines: string[],
    request: RecallRequest,
  ): RecallResult {
    const start = Math.max(1, request.start_line ?? 1);
    const end = Math.min(allLines.length, request.end_line ?? allLines.length);
    const slice = allLines.slice(start - 1, end);
    return {
      ref_id,
      mode: "range",
      label,
      total_lines: allLines.length,
      byte_size: slice.join("\n").length,
      source,
      created_at,
      lines: slice.map((text, i) => ({ n: start + i, text })),
    };
  }

  private execFull(
    ref_id: RefId,
    label: string,
    source: string,
    created_at: string,
    allLines: string[],
    byteSize: number,
  ): RecallResult {
    const capped = allLines.length > this.config.full_mode_max_lines ||
      byteSize > this.config.full_mode_max_bytes;

    let lines: string[];
    let cappedNote: Array<{ n: number; text: string }> = [];

    if (capped) {
      // Byte budget: slice until we exceed the byte cap.
      lines = [];
      let bytes = 0;
      for (const line of allLines) {
        if (
          lines.length >= this.config.full_mode_max_lines ||
          bytes + line.length > this.config.full_mode_max_bytes
        ) break;
        lines.push(line);
        bytes += line.length + 1;
      }
      cappedNote = [{
        n: -1,
        text: `[Truncated: ${allLines.length - lines.length} lines omitted. Use range mode for specific sections.]`,
      }];
    } else {
      lines = allLines;
    }

    return {
      ref_id,
      mode: "full",
      label,
      total_lines: allLines.length,
      byte_size: byteSize,
      source,
      created_at,
      lines: [...lines.map((text, i) => ({ n: i + 1, text })), ...cappedNote],
    };
  }

  private execSearch(
    ref_id: RefId,
    label: string,
    source: string,
    created_at: string,
    allLines: string[],
    request: RecallRequest,
  ): RecallResult {
    const query = request.query ?? "";
    const ctx = this.config.search_context_lines;
    const maxMatches = this.config.search_max_matches;

    // Case-insensitive substring match.
    const lowerQuery = query.toLowerCase();
    const matchedLineNums = new Set<number>();

    for (let i = 0; i < allLines.length; i++) {
      if ((allLines[i] ?? "").toLowerCase().includes(lowerQuery)) {
        // Expand context window around the match.
        for (let c = Math.max(0, i - ctx); c <= Math.min(allLines.length - 1, i + ctx); c++) {
          matchedLineNums.add(c);
        }
        if (matchedLineNums.size >= maxMatches * (ctx * 2 + 1)) break;
      }
    }

    const sorted = Array.from(matchedLineNums).sort((a, b) => a - b);

    // Build result lines with separator markers between non-contiguous groups.
    const resultLines: Array<{ n: number; text: string }> = [];
    let matchCount = 0;
    let prevLine = -2;

    for (const i of sorted) {
      if (i > prevLine + 1 && prevLine >= 0) {
        resultLines.push({ n: -1, text: "---" });
      }
      const isMatch = (allLines[i] ?? "").toLowerCase().includes(lowerQuery);
      if (isMatch) matchCount++;
      resultLines.push({ n: i + 1, text: allLines[i] ?? "" });
      prevLine = i;

      if (matchCount >= maxMatches) {
        resultLines.push({ n: -1, text: `[Search capped at ${maxMatches} matches]` });
        break;
      }
    }

    return {
      ref_id,
      mode: "search",
      label,
      total_lines: allLines.length,
      byte_size: 0,
      source,
      created_at,
      lines: resultLines,
      match_count: matchCount,
    };
  }

  // -------------------------------------------------------------------------
  // Validation
  // -------------------------------------------------------------------------

  private validateRequest(request: RecallRequest): string | null {
    const { mode, start_line, end_line, query } = request;

    if (mode === "range") {
      if (start_line === undefined || end_line === undefined) {
        return "range mode requires both start_line and end_line";
      }
      if (start_line < 1) return "start_line must be >= 1";
      if (end_line < start_line) return "end_line must be >= start_line";
    }

    if (mode === "search" && !query?.trim()) {
      return "search mode requires a non-empty query";
    }

    return null;
  }
}

// ---------------------------------------------------------------------------
// Standalone factory — for callers that want a detached recall function
// ---------------------------------------------------------------------------

export function createRecallTool(
  manager: ManifestManager,
  config?: Partial<RecallConfig>,
): (request: RecallRequest) => Promise<RecallResponse> {
  const tool = new RecallTool(manager, config);
  return (req) => tool.recall(req);
}

// ---------------------------------------------------------------------------
// Formatter — render RecallResult as a string for injection into model context
// ---------------------------------------------------------------------------

export function formatRecallResult(result: RecallResponse): string {
  if (!result.ok) {
    return `[recall_evidence error: ${result.reason}] ${result.detail}`;
  }

  const header = [
    `REF: ${result.ref_id}`,
    `Label: ${result.label}`,
    `Source: ${result.source}`,
    `Total: ${result.total_lines} lines`,
    `Mode: ${result.mode}`,
    result.match_count !== undefined ? `Matches: ${result.match_count}` : null,
  ].filter(Boolean).join(" | ");

  const body = (result.lines ?? [])
    .map((l) => (l.n === -1 ? l.text : `${String(l.n).padStart(4)}: ${l.text}`))
    .join("\n");

  return `--- ${header} ---\n${body}\n--- end ${result.ref_id} ---`;
}
