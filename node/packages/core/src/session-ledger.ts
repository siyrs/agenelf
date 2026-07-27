import { readFile, stat } from "node:fs/promises";
import { join } from "node:path";
import { canonicalize, randomId, sha256 } from "./canonical.ts";
import { appendLine, withDirectoryLock } from "./fs-store.ts";
import { sanitizeObject } from "./privacy.ts";
import type { JsonObject, JsonValue } from "./types.ts";

export const SESSION_LEDGER_SCHEMA_VERSION = 1;
export const SESSION_EVENT_TYPES = new Set([
  "message", "tool_call", "tool_result", "checkpoint", "reflection", "intention",
  "approval_ref", "evidence_ref", "branch_summary", "compaction", "label", "custom"
]);
export const SESSION_ORIGINS = new Set(["runtime", "agent_skill", "owner", "runner", "migration"]);

const SESSION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const ENTRY_ID_RE = /^evt-[0-9a-f]{16}$/;
const BRANCH_ID_RE = /^(main|br-[0-9a-f]{12})$/;
const MAX_LEDGER_BYTES = 32 * 1024 * 1024;

export interface SessionLedgerEntry {
  schema_version: 1;
  id: string;
  session_id: string;
  seq: number;
  parent_id: string | null;
  branch_id: string;
  type: string;
  origin: string;
  ts: string;
  payload: JsonObject;
  prev_hash: string;
  entry_hash: string;
}

function safeSessionId(value: string): string {
  const sessionId = value.trim();
  if (!SESSION_ID_RE.test(sessionId)) throw new Error("非法 session_id");
  return sessionId;
}

function entryWithoutHash(entry: SessionLedgerEntry): JsonObject {
  const { entry_hash: _ignored, ...rest } = entry;
  return rest as unknown as JsonObject;
}

export class SessionLedgerStore {
  readonly root: string;
  readonly directory: string;

  constructor(root: string) {
    this.root = root;
    this.directory = join(root, "local", "memory", "session-ledger");
  }

  private path(sessionId: string): string {
    return join(this.directory, `${safeSessionId(sessionId)}.jsonl`);
  }

  private async readEntries(sessionId: string): Promise<SessionLedgerEntry[]> {
    const path = this.path(sessionId);
    try {
      const info = await stat(path);
      if (info.size > MAX_LEDGER_BYTES) throw new Error("session ledger 超过读取上限");
      const text = await readFile(path, "utf8");
      const result: SessionLedgerEntry[] = [];
      for (const [index, line] of text.split(/\r?\n/).entries()) {
        if (!line.trim()) continue;
        const parsed = JSON.parse(line) as SessionLedgerEntry;
        if (!parsed || typeof parsed !== "object") throw new Error(`ledger 第 ${index + 1} 行非法`);
        result.push(parsed);
      }
      return result;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
      throw error;
    }
  }

  async append(input: {
    sessionId: string;
    type: string;
    origin?: string;
    payload: JsonObject;
    parentId?: string | null;
    branchId?: string | null;
  }): Promise<SessionLedgerEntry> {
    const sessionId = safeSessionId(input.sessionId);
    if (!SESSION_EVENT_TYPES.has(input.type)) throw new Error(`未知 ledger 事件类型：${input.type}`);
    const origin = input.origin ?? "runtime";
    if (!SESSION_ORIGINS.has(origin)) throw new Error(`未知 ledger origin：${origin}`);
    if (input.parentId && !ENTRY_ID_RE.test(input.parentId)) throw new Error("非法 parent_id");
    if (input.branchId && !BRANCH_ID_RE.test(input.branchId)) throw new Error("非法 branch_id");
    const payload = sanitizeObject(input.payload);
    const path = this.path(sessionId);

    return withDirectoryLock(`${path}.lock`, async () => {
      const entries = await this.readEntries(sessionId);
      const byId = new Map(entries.map((entry) => [entry.id, entry]));
      if (input.parentId && !byId.has(input.parentId)) throw new Error("parent_id 不存在");
      const previous = entries.at(-1);
      const parentId = input.parentId ?? previous?.id ?? null;
      const branchId = input.branchId ?? (parentId ? byId.get(parentId)?.branch_id : undefined) ?? "main";
      const base = {
        schema_version: SESSION_LEDGER_SCHEMA_VERSION,
        id: randomId("evt-", 16),
        session_id: sessionId,
        seq: entries.length + 1,
        parent_id: parentId,
        branch_id: branchId,
        type: input.type,
        origin,
        ts: new Date().toISOString(),
        payload,
        prev_hash: previous?.entry_hash ?? ""
      } as const;
      const entry = { ...base, entry_hash: sha256(base as unknown as JsonValue) } as SessionLedgerEntry;
      const nextSize = Buffer.byteLength(`${canonicalize(entry as unknown as JsonValue)}\n`, "utf8");
      const currentSize = previous ? (await stat(path)).size : 0;
      if (currentSize + nextSize > MAX_LEDGER_BYTES) throw new Error("session ledger 写入后超过大小上限");
      await appendLine(path, canonicalize(entry as unknown as JsonValue));
      return entry;
    });
  }

  async createBranch(sessionId: string, parentId: string, label: string, summary = "", origin = "runtime") {
    if (!label.trim()) throw new Error("branch label 不能为空");
    return this.append({
      sessionId,
      type: "branch_summary",
      origin,
      parentId,
      branchId: randomId("br-", 12),
      payload: { label: label.trim().slice(0, 200), summary: summary.trim().slice(0, 4000), branched_from: parentId }
    });
  }

  async entries(sessionId: string, options: { limit?: number; type?: string; branchId?: string } = {}) {
    let entries = await this.readEntries(safeSessionId(sessionId));
    if (options.type) entries = entries.filter((entry) => entry.type === options.type);
    if (options.branchId) entries = entries.filter((entry) => entry.branch_id === options.branchId);
    const limit = Math.max(0, Math.min(Number(options.limit ?? 50), 500));
    return limit ? entries.slice(-limit) : [];
  }

  async verify(sessionId: string) {
    const entries = await this.readEntries(safeSessionId(sessionId));
    const seen = new Set<string>();
    const errors: string[] = [];
    let previousHash = "";
    entries.forEach((entry, index) => {
      const seq = index + 1;
      if (entry.schema_version !== 1) errors.push(`seq=${seq}: schema_version`);
      if (entry.seq !== seq) errors.push(`seq=${seq}: sequence`);
      if (!ENTRY_ID_RE.test(entry.id) || seen.has(entry.id)) errors.push(`seq=${seq}: id`);
      if (entry.parent_id && !seen.has(entry.parent_id)) errors.push(`seq=${seq}: parent`);
      if (!BRANCH_ID_RE.test(entry.branch_id)) errors.push(`seq=${seq}: branch`);
      if (!SESSION_EVENT_TYPES.has(entry.type)) errors.push(`seq=${seq}: type`);
      if (!SESSION_ORIGINS.has(entry.origin)) errors.push(`seq=${seq}: origin`);
      if (entry.prev_hash !== previousHash) errors.push(`seq=${seq}: prev_hash`);
      const computed = sha256(entryWithoutHash(entry) as unknown as JsonValue);
      if (computed !== entry.entry_hash) errors.push(`seq=${seq}: entry_hash`);
      previousHash = entry.entry_hash;
      seen.add(entry.id);
    });
    return {
      schema_version: 1,
      session_id: sessionId,
      entries: entries.length,
      branches: [...new Set(entries.map((entry) => entry.branch_id))].sort(),
      last_entry_id: entries.at(-1)?.id ?? null,
      last_hash: previousHash,
      integrity: errors.length ? "failed" : "ok",
      errors
    };
  }
}
