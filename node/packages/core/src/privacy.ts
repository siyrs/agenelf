import type { JsonObject, JsonValue } from "./types.ts";

const SENSITIVE_KEY = /(?:password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|credential|cookie)/i;
const TEXT_PATTERNS: Array<[RegExp, string]> = [
  [/\bsk-[A-Za-z0-9_-]{8,}\b/g, "sk-[REDACTED]"],
  [/\bgh[pousr]_[A-Za-z0-9]{8,}\b/g, "gh_[REDACTED]"],
  [/\bAKIA[0-9A-Z]{12,}\b/g, "AKIA[REDACTED]"],
  [/\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi, "Bearer [REDACTED]"],
  [/\b(password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key)\b\s*[:=]\s*([^\s,;]+)/gi, "$1=[REDACTED]"]
];

export function redactSensitiveText(value: unknown): string {
  let text = String(value ?? "");
  for (const [pattern, replacement] of TEXT_PATTERNS) text = text.replace(pattern, replacement);
  return text;
}

export interface SanitizeResult {
  value: JsonValue;
  warnings: string[];
}

export function sanitizeJson(value: unknown, path = "root", depth = 8, warnings: string[] = []): SanitizeResult {
  if (depth < 0) {
    warnings.push(`${path}: 嵌套层级过深，已截断`);
    return { value: "[TRUNCATED]", warnings };
  }
  if (value === null || typeof value === "boolean" || typeof value === "number") return { value, warnings };
  if (typeof value === "string") return { value: redactSensitiveText(value), warnings };
  if (Array.isArray(value)) {
    return {
      value: value.map((item, index) => sanitizeJson(item, `${path}[${index}]`, depth - 1, warnings).value),
      warnings
    };
  }
  if (typeof value === "object") {
    const result: JsonObject = {};
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      const childPath = `${path}.${key}`;
      if (SENSITIVE_KEY.test(key)) {
        result[key] = "[REDACTED]";
        warnings.push(`${childPath}: 敏感字段已脱敏`);
      } else {
        result[key] = sanitizeJson(child, childPath, depth - 1, warnings).value;
      }
    }
    return { value: result, warnings };
  }
  return { value: redactSensitiveText(value), warnings };
}

export function sanitizeObject(value: unknown, maxBytes = 64 * 1024): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError("payload 必须是 JSON object");
  const warnings: string[] = [];
  const sanitized = sanitizeJson(value, "payload", 8, warnings).value as JsonObject;
  if (warnings.length) sanitized._privacy_warnings = warnings.slice(0, 20);
  const bytes = Buffer.byteLength(JSON.stringify(sanitized), "utf8");
  if (bytes > maxBytes) throw new RangeError(`payload 超过 ${maxBytes} 字节上限`);
  return sanitized;
}
