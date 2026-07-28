import { join } from "node:path";
import { atomicWriteJson, readJson, withDirectoryLock } from "./fs-store.ts";
import { redactSensitiveText } from "./privacy.ts";
import { randomId } from "./canonical.ts";
import type { JsonObject } from "./types.ts";

export interface MemoryEntry {
  id: string;
  kind: string;
  content: string;
  created_at: string;
}

export class MemoryStore {
  readonly path: string;
  readonly lockPath: string;
  readonly maxEntries: number;

  constructor(root: string, maxEntries = 1000) {
    this.path = join(root, "local", "memory", "node-memory.json");
    this.lockPath = `${this.path}.lock`;
    this.maxEntries = Math.max(10, Math.min(maxEntries, 10_000));
  }

  async list(limit = 100): Promise<MemoryEntry[]> {
    const data = await readJson<{ entries?: MemoryEntry[] }>(this.path, {});
    const entries = Array.isArray(data.entries) ? data.entries : [];
    return entries.slice(-Math.max(0, Math.min(limit, 500)));
  }

  async add(kind: string, content: string): Promise<MemoryEntry> {
    const safeKind = String(kind || "episode").trim().slice(0, 40) || "episode";
    const safeContent = redactSensitiveText(content).trim().slice(0, 8000);
    if (!safeContent) throw new Error("memory content 不能为空");
    return withDirectoryLock(this.lockPath, async () => {
      const data = await readJson<{ schema_version?: number; entries?: MemoryEntry[] }>(this.path, {});
      const entries = Array.isArray(data.entries) ? data.entries : [];
      const entry: MemoryEntry = { id: randomId("mem-", 16), kind: safeKind, content: safeContent, created_at: new Date().toISOString() };
      entries.push(entry);
      await atomicWriteJson(this.path, { schema_version: 1, entries: entries.slice(-this.maxEntries) } as unknown as JsonObject);
      return entry;
    });
  }

  async recall(query: string, limit = 5): Promise<MemoryEntry[]> {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    const entries = await this.list(this.maxEntries);
    return entries
      .map((entry) => ({ entry, score: terms.reduce((score, term) => score + (entry.content.toLowerCase().includes(term) ? 1 : 0), 0) }))
      .filter((item) => item.score > 0 || terms.length === 0)
      .sort((a, b) => b.score - a.score || b.entry.created_at.localeCompare(a.entry.created_at))
      .slice(0, Math.max(1, Math.min(limit, 50)))
      .map((item) => item.entry);
  }

  async promptBlock(limit = 30, maxChars = 8000): Promise<string> {
    const entries = await this.list(limit);
    if (!entries.length) return "";
    const boundedChars = Math.max(500, Math.min(Math.trunc(maxChars), 50_000));
    const text = ["主人长期记忆（已脱敏）：", ...entries.map((entry) => `- [${entry.kind}] ${entry.content}`)].join("\n");
    return text.length <= boundedChars ? text : `${text.slice(0, Math.max(0, boundedChars - 1))}…`;
  }
}
