import { createHash, randomBytes } from "node:crypto";
import type { JsonValue } from "./types.ts";

export function canonicalize(value: JsonValue): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  const entries = Object.entries(value).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([key, child]) => `${JSON.stringify(key)}:${canonicalize(child)}`).join(",")}}`;
}

export function sha256(value: JsonValue): string {
  return createHash("sha256").update(canonicalize(value), "utf8").digest("hex");
}

export function randomId(prefix: string, hexChars = 16): string {
  return `${prefix}${randomBytes(Math.ceil(hexChars / 2)).toString("hex").slice(0, hexChars)}`;
}
