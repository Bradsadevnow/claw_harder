/**
 * Identity key derivation — the ontology anchor for reality.
 *
 * An identity_key identifies an ENTITY, not an observation's value.
 * Two observations can share an identity_key while making contradictory
 * claims — that's exactly when arbitration fires.
 *
 * Design rules:
 * - Derived from stable identifying fields only (not mutable value fields).
 * - Normalized: lowercase, trimmed, whitespace-collapsed.
 * - Deterministic: same input always produces same key.
 * - Type-scoped: keys are prefixed with fact type to prevent cross-type collisions.
 * - Extensible: custom types register their own strategies via registerStrategy().
 *
 * Unknown types fall back to value-hashing — meaning each unique value gets
 * a unique key and facts never falsely conflict. Conservative but safe.
 */

import { createHash } from "node:crypto";
import type { FactType } from "../types/manifest.js";
import { canonicalJson } from "./serialization.js";

// ---------------------------------------------------------------------------
// Strategy registry
// ---------------------------------------------------------------------------

export type IdentityStrategy = (value: Record<string, unknown>) => string;

const registry = new Map<string, IdentityStrategy>();

function register(type: string, strategy: IdentityStrategy): void {
  registry.set(type, strategy);
}

export function registerStrategy(type: string, strategy: IdentityStrategy): void {
  registry.set(type, strategy);
}

// ---------------------------------------------------------------------------
// Normalization helpers
// ---------------------------------------------------------------------------

/** Lowercase, trim, collapse whitespace, strip control characters. */
function norm(v: unknown): string {
  if (v == null) return "";
  return String(v).toLowerCase().trim().replace(/\s+/g, " ").replace(/[\x00-\x1f]/g, "");
}

/** First non-empty value from candidates. */
function first(...candidates: unknown[]): string {
  for (const c of candidates) {
    const n = norm(c);
    if (n.length > 0) return n;
  }
  return "unknown";
}

// ---------------------------------------------------------------------------
// Built-in strategies — one per core FactType
// ---------------------------------------------------------------------------

register("preference", (v) =>
  // A preference is identified by its topic/key — not its current value.
  `preference:${first(v["key"], v["topic"], v["name"], v["category"])}`,
);

register("contact", (v) =>
  // Contacts are identified by a canonical identifier — prefer machine-stable ids.
  `contact:${first(v["email"], v["id"], v["username"], v["handle"], v["name"])}`,
);

register("task", (v) =>
  // Tasks identified by explicit id first, then title.
  `task:${first(v["id"], v["task_id"], v["title"])}`,
);

register("context", (v) =>
  // Context facts are scoped — e.g. "context:location:current" or "context:session:goal".
  `context:${first(v["scope"], v["domain"])}:${first(v["key"], v["name"])}`,
);

register("credential", (v) => {
  // IMPORTANT: identity_key never contains actual credential values.
  // It identifies the credential SLOT (username + service), not the secret.
  const username = first(v["username"], v["account"], v["principal"]);
  const service = first(v["service"], v["host"], v["realm"], v["provider"]);
  return `credential:${username}@${service}`;
});

register("service", (v) => {
  const name = first(v["name"], v["service_name"], v["id"]);
  const endpoint = first(v["endpoint"], v["host"], v["url"], v["provider_id"], "");
  return endpoint ? `service:${name}:${endpoint}` : `service:${name}`;
});

register("knowledge", (v) => {
  const domain = first(v["domain"], v["category"], "general");
  const key = first(v["key"], v["topic"], v["subject"]);
  return `knowledge:${domain}:${key}`;
});

register("constraint", (v) => {
  const scope = first(v["scope"], v["agent"], "global");
  const key = first(v["key"], v["constraint_id"], v["name"]);
  return `constraint:${scope}:${key}`;
});

register("event", (v) => {
  // Events are usually unique — use explicit id if present, otherwise unique per value.
  const id = norm(v["id"] ?? v["event_id"] ?? "");
  if (id) return `event:${first(v["type"], "unknown")}:${id}`;
  // No stable id — treat each event as unique by hashing its full value.
  return `event:${first(v["type"], "unknown")}:${shortHash(canonicalJson(v))}`;
});

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Derive the identity_key for a fact.
 *
 * Falls back to `{type}:{hash_of_value[:8]}` for unregistered types —
 * meaning identical values deduplicate but different values never falsely conflict.
 */
export function deriveIdentityKey(
  type: FactType,
  value: Record<string, unknown>,
): string {
  const strategy = registry.get(type);
  if (strategy) {
    try {
      return strategy(value);
    } catch {
      // Strategy threw — fall back to safe hash-based key.
      return `${type}:${shortHash(canonicalJson(value))}`;
    }
  }
  // Unknown type — conservative hash-based fallback.
  return `${type}:${shortHash(canonicalJson(value))}`;
}

/**
 * Derive the fact_hash for deduplication.
 * SHA256 of type + canonical_json(value).
 */
export function deriveFactHash(
  type: FactType,
  value: Record<string, unknown>,
): string {
  return createHash("sha256")
    .update(`${type}:${canonicalJson(value)}`, "utf8")
    .digest("hex");
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function shortHash(input: string): string {
  return createHash("sha256").update(input, "utf8").digest("hex").slice(0, 8);
}
