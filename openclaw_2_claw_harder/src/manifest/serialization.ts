/**
 * Canonical JSON serialization.
 *
 * Determinism requirements:
 * - Object keys sorted lexicographically at every depth.
 * - Arrays preserve insertion order (they're ordered data).
 * - No undefined values — they don't exist in JSON.
 * - No circular references — manifests are plain data.
 *
 * This is used exclusively for integrity hashing and fact deduplication.
 * Do NOT use for display or wire serialization — use JSON.stringify there.
 */

export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortedReplacer(value));
}

function sortedReplacer(value: unknown): unknown {
  if (value === null || typeof value !== "object") {
    return value;
  }

  if (Array.isArray(value)) {
    return value.map(sortedReplacer);
  }

  const obj = value as Record<string, unknown>;
  const sorted: Record<string, unknown> = {};
  for (const key of Object.keys(obj).sort()) {
    const v = obj[key];
    if (v !== undefined) {
      sorted[key] = sortedReplacer(v);
    }
  }
  return sorted;
}
