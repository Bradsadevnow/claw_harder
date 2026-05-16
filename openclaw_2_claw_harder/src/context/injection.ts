/**
 * Manifest → prompt injection compiler.
 *
 * Produces a structured, bounded User Message injected immediately after the
 * system prompt. This preserves system prompt stability and prevents policy
 * dilution by keeping operational context separate from behavioral instructions.
 *
 * The manifest is NOT dumped into the prompt. The engine:
 *   select → rank → compress → inject → reference → defer
 *
 * Semantic strata (injection order = cognitive priority):
 *   1. Current Objective       — what we're trying to do
 *   2. Active Tactical Facts   — authoritative beliefs, high-signal only
 *   3. Conflicted Beliefs      — explicit uncertainty, visible not hidden
 *   4. Active Summaries        — navigational map (stale-flagged when stale)
 *   5. Available References    — REF_IDs for surgical recall, no content
 *
 * Drill-down contract: summaries are navigational. Precise execution requires
 * drilling into FACT_IDs and using recall_evidence for artifacts.
 */

import type { Fact, FactType, Manifest, Reference, Summary } from "../types/manifest.js";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

export interface InjectionConfig {
  /** Maximum total characters in the injected block. Default: 8000. */
  max_chars: number;
  /** Maximum facts to inject. Default: 20. */
  max_facts: number;
  /** Maximum summaries to inject. Default: 4. */
  max_summaries: number;
  /** Include the operations log tail (recent events). Default: true. */
  include_recent_ops: boolean;
  /** How many recent operations to surface. Default: 5. */
  max_recent_ops: number;
  /** Include available recall references. Default: true. */
  include_refs: boolean;
}

const INJECTION_DEFAULTS: InjectionConfig = {
  max_chars: 8000,
  max_facts: 20,
  max_summaries: 4,
  include_recent_ops: true,
  max_recent_ops: 5,
  include_refs: true,
};

// ---------------------------------------------------------------------------
// Type priority — shapes which facts get injected first
// Higher number = higher priority when budget is tight.
// ---------------------------------------------------------------------------

const TYPE_PRIORITY: Partial<Record<FactType, number>> = {
  constraint: 100,  // hard limits on behavior — always first
  task: 90,         // active work — agent's primary focus
  credential: 80,   // auth state — reference only, never value
  context: 70,      // situational awareness
  contact: 60,      // people involved
  preference: 50,   // behavioral modifiers
  knowledge: 40,    // background facts
  service: 30,      // infrastructure
  event: 20,        // history — lowest priority, most ephemeral
};

function typePriority(type: FactType): number {
  return TYPE_PRIORITY[type] ?? 10;
}

// ---------------------------------------------------------------------------
// InjectionCompiler
// ---------------------------------------------------------------------------

export class InjectionCompiler {
  readonly config: InjectionConfig;

  constructor(config: Partial<InjectionConfig> = {}) {
    this.config = { ...INJECTION_DEFAULTS, ...config };
  }

  /**
   * Build the operational memory context block from a manifest.
   * Returns a string to be injected as a User Message.
   */
  compile(manifest: Manifest): string {
    const sections: string[] = [];

    // 1. Objective — from goal-type summaries or explicit objective fact.
    const objective = this.extractObjective(manifest);
    if (objective) {
      sections.push(`**Objective:** ${objective}`);
    }

    // 2. Active facts — selected, ranked, compressed.
    const facts = this.selectFacts(manifest);
    if (facts.length > 0) {
      sections.push(this.renderFactsSection(facts));
    }

    // 3. Conflicted beliefs — uncertainty is visible, never hidden.
    const conflicted = this.selectConflictedFacts(manifest);
    if (conflicted.length > 0) {
      sections.push(this.renderConflictedSection(conflicted));
    }

    // 4. Active summaries — navigational map only.
    const summaries = this.selectSummaries(manifest);
    if (summaries.length > 0) {
      sections.push(this.renderSummariesSection(summaries));
    }

    // 5. Recent operations — what just happened.
    if (this.config.include_recent_ops) {
      const ops = this.extractRecentOps(manifest);
      if (ops.length > 0) {
        sections.push(this.renderOpsSection(ops));
      }
    }

    // 6. Recall references — IDs only, no content.
    if (this.config.include_refs) {
      const refs = this.selectRefs(manifest);
      if (refs.length > 0) {
        sections.push(this.renderRefsSection(refs));
      }
    }

    // Drill-down contract — always last.
    sections.push(
      `_Use summaries for navigation. For precise execution, drill into FACT_IDs via \`recall_evidence\`._`,
    );

    const body = sections.join("\n\n");

    // Hard cap — truncate with a note rather than silently drop.
    if (body.length > this.config.max_chars) {
      return (
        body.slice(0, this.config.max_chars - 120) +
        `\n\n[Operational context truncated at ${this.config.max_chars} chars. Use recall_evidence for full state.]`
      );
    }

    return `# Current Operational Memory\n\n${body}`;
  }

  // -------------------------------------------------------------------------
  // Section builders
  // -------------------------------------------------------------------------

  private extractObjective(manifest: Manifest): string | null {
    // Goal-type summary takes priority.
    for (const s of Object.values(manifest.summaries) as Summary[]) {
      if (s.type === "goal" && s.state === "active") {
        const staleTag = s.stale ? " [STALE — refresh before acting]" : "";
        return s.key_points[0]
          ? `${s.key_points[0]}${staleTag}`
          : null;
      }
    }
    // Fall back to highest-confidence task fact.
    const tasks = (Object.values(manifest.facts) as Fact[])
      .filter((f) => f.type === "task" && f.state === "active")
      .sort((a, b) => b.confidence - a.confidence);
    if (tasks.length > 0 && tasks[0]) {
      const t = tasks[0];
      const title = String(t.value["title"] ?? t.value["goal"] ?? t.value["id"] ?? "");
      return title || null;
    }
    return null;
  }

  private selectFacts(manifest: Manifest): Fact[] {
    const active = (Object.values(manifest.facts) as Fact[]).filter(
      (f) => f.state === "active",
    );

    // Sort: type priority desc, then confidence desc, then discovery_index asc.
    active.sort((a, b) => {
      const pd = typePriority(b.type) - typePriority(a.type);
      if (pd !== 0) return pd;
      const cd = b.confidence - a.confidence;
      if (cd !== 0) return cd;
      return a.discovery_index - b.discovery_index;
    });

    return active.slice(0, this.config.max_facts);
  }

  private selectConflictedFacts(manifest: Manifest): Fact[] {
    return (Object.values(manifest.facts) as Fact[]).filter(
      (f) => f.state === "conflicted",
    );
  }

  private selectSummaries(manifest: Manifest): Summary[] {
    // Priority: goal > session > conflict > others. Active first, stale second.
    const summaryTypePriority: Record<string, number> = {
      goal: 100,
      session: 80,
      conflict: 70,
      task: 60,
      contact: 40,
    };

    const active = (Object.values(manifest.summaries) as Summary[])
      .filter((s) => s.state === "active")
      .sort((a, b) => {
        const staleScore = (Number(a.stale) - Number(b.stale)); // non-stale first
        if (staleScore !== 0) return staleScore;
        const tp = (summaryTypePriority[b.type] ?? 10) - (summaryTypePriority[a.type] ?? 10);
        return tp;
      });

    return active.slice(0, this.config.max_summaries);
  }

  private extractRecentOps(manifest: Manifest): Array<{ type: string; ts: string; summary: string }> {
    const relevant = manifest.operations
      .filter((op) =>
        op.type === "fact_added" ||
        op.type === "fact_superseded" ||
        op.type === "fact_conflicted" ||
        op.type === "summary_added" ||
        op.type === "manifest_committed" ||
        op.type === "recall_event",
      )
      .slice(-this.config.max_recent_ops);

    return relevant.map((op) => ({
      type: op.type,
      ts: op.timestamp,
      summary: this.summarizeOp(op),
    }));
  }

  private summarizeOp(op: { type: string; metadata: Record<string, unknown> }): string {
    switch (op.type) {
      case "fact_added":
        return `Fact ${op.metadata["fact_id"]} added (${op.metadata["state"] ?? "active"})`;
      case "fact_superseded":
        return `Fact ${op.metadata["superseded_id"] ?? op.metadata["fact_id"]} superseded by ${op.metadata["new_fact_id"] ?? "manual"}`;
      case "fact_conflicted":
        return `Conflict: facts ${op.metadata["new_fact_id"]} and ${op.metadata["conflicted_id"]}`;
      case "summary_added":
        return `Summary ${op.metadata["summary_id"]} generated (${op.metadata["type"]}:${op.metadata["scope"]})`;
      case "manifest_committed":
        return `State committed (rev ${op.metadata["ts"] ?? ""})`;
      case "recall_event":
        return `Recall: ${op.metadata["ref_id"]} mode=${op.metadata["mode"]}`;
      default:
        return op.type;
    }
  }

  private selectRefs(manifest: Manifest): Reference[] {
    // Most recently created refs first — they're most likely relevant.
    return (Object.values(manifest.references) as Reference[])
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .slice(0, 8);
  }

  // -------------------------------------------------------------------------
  // Renderers — sparse and scannable, not verbose
  // -------------------------------------------------------------------------

  private renderFactsSection(facts: Fact[]): string {
    const lines = facts.map((f) => {
      const conf = `${(f.confidence * 100).toFixed(0)}%`;
      const obs = f.observations.observation_count > 1
        ? ` ×${f.observations.observation_count}`
        : "";
      const value = this.renderFactValue(f);
      return `- [${f.fact_id}] **${f.type}** (${conf}${obs}): ${value}`;
    });
    return `## Active Facts\n\n${lines.join("\n")}`;
  }

  private renderFactValue(fact: Fact): string {
    // Credentials: never render values, only reference existence.
    if (fact.type === "credential") {
      const user = fact.value["username"] ?? fact.value["account"] ?? "?";
      const svc = fact.value["service"] ?? fact.value["host"] ?? "?";
      return `[credential slot] ${user}@${svc} — use identity_key to reference`;
    }
    // General: render a compact JSON-ish summary of value fields.
    const entries = Object.entries(fact.value)
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .slice(0, 4) // cap at 4 fields to avoid bloat
      .map(([k, v]) => `${k}: ${JSON.stringify(v)}`);
    return entries.join(", ");
  }

  private renderConflictedSection(facts: Fact[]): string {
    const lines = facts.map((f) => {
      const conf = `${(f.confidence * 100).toFixed(0)}%`;
      const value = this.renderFactValue(f);
      return `- [STATE: CONFLICTED] [${f.fact_id}] **${f.type}** (${conf}): ${value}`;
    });
    return `## Conflicted Beliefs ⚠️\n\n${lines.join("\n")}\n\n_These beliefs require resolution before acting on them._`;
  }

  private renderSummariesSection(summaries: Summary[]): string {
    const lines = summaries.map((s) => {
      const staleTag = s.stale ? " **[STALE]**" : "";
      const conf = `${(s.confidence * 100).toFixed(0)}%`;
      const points = s.key_points.slice(0, 3).map((p) => `  - ${p}`).join("\n");
      const conflicts = s.conflicts.length > 0
        ? `\n  - ⚠️ ${s.conflicts.slice(0, 2).join("; ")}`
        : "";
      const actions = s.recommended_next_actions.length > 0
        ? `\n  → ${s.recommended_next_actions[0]}`
        : "";
      return `**[${s.summary_id}]** ${s.type}:${s.scope}${staleTag} (${conf})\n${points}${conflicts}${actions}`;
    });
    return `## Summaries\n\n${lines.join("\n\n")}`;
  }

  private renderOpsSection(
    ops: Array<{ type: string; ts: string; summary: string }>,
  ): string {
    const lines = ops.map((op) => {
      const ts = op.ts.slice(11, 19); // HH:MM:SS
      return `- \`${ts}\` ${op.summary}`;
    });
    return `## Recent Events\n\n${lines.join("\n")}`;
  }

  private renderRefsSection(refs: Reference[]): string {
    const lines = refs.map((r) => {
      const size = r.byte_size > 1024
        ? `${(r.byte_size / 1024).toFixed(1)}KB`
        : `${r.byte_size}B`;
      return `- \`${r.ref_id}\` — ${r.label} (${r.line_count} lines, ${size}, src: ${r.source})`;
    });
    return `## Available References\n\n${lines.join("\n")}\n\n_Recall with: \`recall_evidence({ ref_id, mode: "summary" | "search" | "range" })\`_`;
  }
}
