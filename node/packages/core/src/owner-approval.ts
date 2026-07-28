import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { lstat, mkdir, readFile, readdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import { atomicWriteJson, readJson, withDirectoryLock } from "./fs-store.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const REQUEST_ID = /^(?:op|auth)-[0-9a-f]{16}$/;
const COMMAND_ID = /^apc-[0-9a-f]{16}$/;
const FINGERPRINT = /^[0-9a-f]{64}$/;
const DECISIONS = new Set(["approve", "deny"]);
const RISKS = new Set(["change", "privileged"]);
const REQUIRED_COMMAND_KEYS = [
  "schema_version", "id", "action", "request_id", "request_fingerprint", "reason",
  "decided_by", "created_at", "expires_at", "duplicates", "signature"
] as const;
const COMMAND_KEYS = new Set(REQUIRED_COMMAND_KEYS);
const MAX_DUPLICATES = 100;

export class ApprovalError extends Error {}
export class AmbiguousApprovalError extends ApprovalError {
  readonly pending: JsonObject[];
  constructor(message: string, pending: JsonObject[]) { super(message); this.pending = pending; }
}

function nowIso(): string { return new Date().toISOString(); }
function compareCodePoints(left: string, right: string): number {
  const a = [...left].map((item) => item.codePointAt(0) ?? 0);
  const b = [...right].map((item) => item.codePointAt(0) ?? 0);
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}
function pythonString(value: string): string {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
}
export function pythonCanonical(value: JsonValue): string {
  if (value === null) return "null";
  if (typeof value === "string") return pythonString(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new ApprovalError("审批 payload 包含非有限数字");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(pythonCanonical).join(",")}]`;
  return `{${Object.entries(value).sort(([a], [b]) => compareCodePoints(a, b)).map(([key, child]) => `${pythonString(key)}:${pythonCanonical(child)}`).join(",")}}`;
}
export function bindingFromRequest(request: JsonObject): JsonObject {
  return {
    capability: request.capability ?? "",
    operation: request.operation ?? "",
    target: request.target ?? "",
    parameters: request.parameters ?? {}
  };
}
export function bindingFingerprint(binding: JsonObject): string {
  return createHash("sha256").update(pythonCanonical(binding), "utf8").digest("hex");
}
function parseDate(value: JsonValue, field: string): Date {
  if (typeof value !== "string" || !value.trim()) throw new ApprovalError(`审批命令 ${field} 为空`);
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) throw new ApprovalError(`审批命令 ${field} 非法`);
  return parsed;
}
function objectValue(value: JsonValue | undefined, field: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApprovalError(`${field} 必须是 object`);
  return value;
}
function safeId(value: JsonValue, pattern: RegExp, field: string): string {
  const text = String(value ?? "").trim();
  if (!pattern.test(text)) throw new ApprovalError(`${field} 非法：${text}`);
  return text;
}
function commandString(value: JsonValue | undefined, field: string, maximum = 1_000): string {
  if (typeof value !== "string") throw new ApprovalError(`审批命令 ${field} 必须是字符串`);
  if (value.length > maximum) throw new ApprovalError(`审批命令 ${field} 超过长度限制`);
  return value;
}
async function regularFile(path: string): Promise<boolean> {
  try { const info = await lstat(path); return info.isFile() && !info.isSymbolicLink(); }
  catch { return false; }
}
function decisionEquivalent(existing: JsonObject, intended: JsonObject): boolean {
  return existing.decision === intended.decision &&
    existing.fingerprint === intended.fingerprint &&
    String(existing.reason ?? "") === String(intended.reason ?? "") &&
    String(existing.decided_by ?? "") === String(intended.decided_by ?? "");
}

export class OwnerApprovalStore {
  readonly root: string;
  readonly opsRequests: string;
  readonly authRequests: string;
  readonly authDecisions: string;
  readonly opsResults: string;
  readonly commands: string;
  readonly commandResults: string;
  readonly locks: string;

  constructor(root: string) {
    this.root = resolve(root);
    this.opsRequests = join(this.root, "data", "ops-requests");
    this.authRequests = join(this.root, "data", "auth-requests");
    this.authDecisions = join(this.root, "data", "auth-decisions");
    this.opsResults = join(this.root, "data", "ops-results");
    this.commands = join(this.root, "data", "approval-commands");
    this.commandResults = join(this.root, "data", "approval-results");
    this.locks = join(this.root, "data", "approval-locks");
  }

  async initialize(): Promise<void> {
    await Promise.all([
      this.opsRequests, this.authRequests, this.authDecisions, this.opsResults,
      this.commands, this.commandResults, this.locks
    ].map((path) => mkdir(path, { recursive: true })));
  }

  private async requestPath(requestId: string): Promise<string> {
    const id = safeId(requestId, REQUEST_ID, "请求 ID");
    const base = id.startsWith("op-") ? this.opsRequests : this.authRequests;
    const path = join(base, `${id}.json`);
    if (!(await regularFile(path))) throw new ApprovalError(`未找到待审批请求：${id}`);
    return path;
  }

  async loadRequest(requestId: string): Promise<JsonObject> {
    const id = safeId(requestId, REQUEST_ID, "请求 ID");
    const request = await readJson<JsonObject | null>(await this.requestPath(id), null);
    if (!request) throw new ApprovalError(`待审批请求不是合法 JSON：${id}`);
    if (request.id !== id) throw new ApprovalError(`待审批请求文件与文档 ID 不一致：${id}`);
    return request;
  }

  private validateRequestDocument(request: JsonObject, expectedId = ""): JsonObject {
    if (request.schema_version !== 1) throw new ApprovalError("不支持的待审批请求版本");
    const id = safeId(request.id, REQUEST_ID, "请求 ID");
    if (expectedId && id !== expectedId) throw new ApprovalError(`待审批请求 ID 不匹配：${expectedId}`);
    if (!RISKS.has(String(request.risk ?? ""))) throw new ApprovalError("只有 change/privileged 请求可以进入审批通道");
    for (const field of ["capability", "operation", "target", "fingerprint", "created_at"]) {
      if (typeof request[field] !== "string" || !String(request[field]).trim()) throw new ApprovalError(`待审批请求缺少字段：${field}`);
    }
    objectValue(request.parameters, "parameters");
    if (!FINGERPRINT.test(String(request.fingerprint))) throw new ApprovalError("待审批请求 fingerprint 格式非法");
    if (request.fingerprint !== bindingFingerprint(bindingFromRequest(request))) throw new ApprovalError("待审批请求指纹不匹配，文件可能被篡改");
    return request;
  }

  async validatePendingRequest(request: JsonObject): Promise<JsonObject> {
    const value = this.validateRequestDocument(request);
    const id = String(value.id);
    if (await regularFile(join(this.authDecisions, `${id}.json`))) throw new ApprovalError(`请求已被裁决：${id}`);
    if (await regularFile(join(this.opsResults, `${id}.json`))) throw new ApprovalError(`请求已有执行结果：${id}`);
    return value;
  }

  private async preflightDecision(path: string, decision: JsonObject): Promise<void> {
    const existing = await readJson<JsonObject | null>(path, null);
    if (existing && !decisionEquivalent(existing, decision)) throw new ApprovalError(`请求已有不同裁决：${String(decision.request_id)}`);
  }

  private async writeDecisionExact(path: string, decision: JsonObject): Promise<{ value: JsonObject; idempotent: boolean }> {
    try {
      await atomicWriteJson(path, decision, true);
      return { value: decision, idempotent: false };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      const existing = await readJson<JsonObject | null>(path, null);
      if (existing && decisionEquivalent(existing, decision)) return { value: existing, idempotent: true };
      throw new ApprovalError(`请求已有不同裁决：${String(decision.request_id)}`);
    }
  }

  async applyDecision(
    requestId: string,
    options: { action?: "approve" | "deny"; reason?: string; decidedBy?: string; expectedFingerprint?: string; duplicates?: string[] } = {}
  ): Promise<JsonObject> {
    const id = safeId(requestId, REQUEST_ID, "请求 ID");
    const action = options.action ?? "approve";
    if (!DECISIONS.has(action)) throw new ApprovalError(`不支持的审批动作：${action}`);
    const initial = this.validateRequestDocument(await this.loadRequest(id), id);
    const expectedFingerprint = options.expectedFingerprint ? String(options.expectedFingerprint) : String(initial.fingerprint);
    if (initial.fingerprint !== expectedFingerprint) throw new ApprovalError("审批命令绑定的请求指纹与当前请求不一致");

    return withDirectoryLock(join(this.locks, `binding-${expectedFingerprint}.lock`), async () => {
      const request = this.validateRequestDocument(await this.loadRequest(id), id);
      if (request.fingerprint !== expectedFingerprint) throw new ApprovalError("审批期间请求指纹发生变化");
      if (await regularFile(join(this.opsResults, `${id}.json`))) throw new ApprovalError(`请求已有执行结果：${id}`);

      const reason = String(options.reason ?? "").trim().slice(0, 1_000);
      const decidedBy = String(options.decidedBy ?? "owner").trim().slice(0, 200) || "owner";
      const duplicateIds = [...new Set((options.duplicates ?? []).map((item) => safeId(item, REQUEST_ID, "重复请求 ID")))]
        .filter((duplicateId) => duplicateId !== id);
      if (duplicateIds.length > MAX_DUPLICATES) throw new ApprovalError(`重复请求超过 ${MAX_DUPLICATES} 个上限`);

      const decidedAt = nowIso();
      const primaryDecision: JsonObject = {
        schema_version: 1, request_id: id, decision: action, fingerprint: request.fingerprint,
        decided_at: decidedAt, decided_by: decidedBy, reason
      };
      const duplicateDecisions: Array<{ id: string; decision: JsonObject }> = [];
      for (const duplicateId of duplicateIds) {
        const duplicate = this.validateRequestDocument(await this.loadRequest(duplicateId), duplicateId);
        if (duplicate.fingerprint !== request.fingerprint) throw new ApprovalError(`重复请求 ${duplicateId} 与已审批请求指纹不同`);
        if (await regularFile(join(this.opsResults, `${duplicateId}.json`))) throw new ApprovalError(`重复请求已有执行结果：${duplicateId}`);
        duplicateDecisions.push({
          id: duplicateId,
          decision: {
            schema_version: 1, request_id: duplicateId, decision: "deny", fingerprint: duplicate.fingerprint,
            decided_at: decidedAt, decided_by: decidedBy, reason: `superseded_by:${id}`
          }
        });
      }

      const primaryPath = join(this.authDecisions, `${id}.json`);
      await this.preflightDecision(primaryPath, primaryDecision);
      for (const duplicate of duplicateDecisions) {
        await this.preflightDecision(join(this.authDecisions, `${duplicate.id}.json`), duplicate.decision);
      }

      const primary = await this.writeDecisionExact(primaryPath, primaryDecision);
      const superseded: string[] = [];
      for (const duplicate of duplicateDecisions) {
        await this.writeDecisionExact(join(this.authDecisions, `${duplicate.id}.json`), duplicate.decision);
        superseded.push(duplicate.id);
      }
      return { ...primary.value, idempotent: primary.idempotent, ...(superseded.length ? { superseded_duplicates: superseded } : {}) };
    }, { timeoutMs: 5_000, staleMs: 60_000 });
  }

  async listPending(): Promise<JsonObject[]> {
    const result: JsonObject[] = [];
    for (const base of [this.opsRequests, this.authRequests]) {
      let names: string[] = [];
      try { names = await readdir(base); } catch { continue; }
      for (const name of names.filter((item) => /^(?:op|auth)-[0-9a-f]{16}\.json$/.test(item))) {
        const id = name.slice(0, -5);
        try { result.push(await this.validatePendingRequest(await this.loadRequest(id))); } catch { /* not pending or malformed */ }
      }
    }
    return result.sort((a, b) => String(a.created_at ?? "").localeCompare(String(b.created_at ?? "")) || String(a.id).localeCompare(String(b.id)));
  }

  async resolvePending(requestId = ""): Promise<{ selected: JsonObject; duplicates: string[] }> {
    const pending = await this.listPending();
    if (requestId.trim()) {
      const selected = await this.validatePendingRequest(await this.loadRequest(requestId));
      return {
        selected,
        duplicates: pending.filter((item) => item.id !== selected.id && item.fingerprint === selected.fingerprint).map((item) => String(item.id))
      };
    }
    if (!pending.length) throw new ApprovalError("当前没有待审批请求");
    const groups = new Map<string, JsonObject[]>();
    for (const item of pending) groups.set(String(item.fingerprint), [...(groups.get(String(item.fingerprint)) ?? []), item]);
    if (groups.size !== 1) throw new AmbiguousApprovalError("存在多个不同的待审批请求，请提供精确请求 ID", pending);
    const group = [...groups.values()][0];
    const selected = group[group.length - 1];
    return { selected, duplicates: group.slice(0, -1).map((item) => String(item.id)) };
  }

  static commandPayload(document: JsonObject): JsonObject {
    return {
      schema_version: document.schema_version, id: document.id, action: document.action,
      request_id: document.request_id, request_fingerprint: document.request_fingerprint,
      reason: document.reason ?? "", decided_by: document.decided_by ?? "owner",
      created_at: document.created_at, expires_at: document.expires_at, duplicates: document.duplicates ?? []
    };
  }

  static signCommand(document: JsonObject, key: Buffer): string {
    return createHmac("sha256", key).update(pythonCanonical(OwnerApprovalStore.commandPayload(document)), "utf8").digest("hex");
  }

  private async normalizeDuplicates(request: JsonObject, values: JsonValue[], pendingOnly: boolean): Promise<string[]> {
    const ids: string[] = [];
    for (const value of values) {
      if (typeof value !== "string") throw new ApprovalError("审批命令 duplicates 只能包含字符串 ID");
      const id = safeId(value, REQUEST_ID, "重复请求 ID");
      if (id !== request.id && !ids.includes(id)) ids.push(id);
    }
    if (ids.length > MAX_DUPLICATES) throw new ApprovalError(`重复请求超过 ${MAX_DUPLICATES} 个上限`);
    for (const id of ids) {
      const loaded = await this.loadRequest(id);
      const duplicate = pendingOnly ? await this.validatePendingRequest(loaded) : this.validateRequestDocument(loaded, id);
      if (duplicate.fingerprint !== request.fingerprint) throw new ApprovalError(`重复请求 ${id} 与当前请求指纹不同`);
    }
    return ids;
  }

  async submitCommand(
    requestId: string,
    key: Buffer,
    options: { action?: "approve" | "deny"; reason?: string; decidedBy?: string; expiresInSeconds?: number; duplicates?: string[] } = {}
  ): Promise<JsonObject> {
    if (key.length < 32) throw new ApprovalError("审批 HMAC key 太短");
    const request = await this.validatePendingRequest(await this.loadRequest(requestId));
    const action = options.action ?? "approve";
    if (!DECISIONS.has(action)) throw new ApprovalError(`不支持的审批动作：${action}`);
    const createdAt = new Date();
    const command: JsonObject = {
      schema_version: 1, id: `apc-${randomBytes(8).toString("hex")}`, action,
      request_id: request.id, request_fingerprint: request.fingerprint,
      reason: String(options.reason ?? "").trim().slice(0, 1_000),
      decided_by: String(options.decidedBy ?? "owner").trim().slice(0, 200) || "owner",
      created_at: createdAt.toISOString(),
      expires_at: new Date(createdAt.getTime() + Math.max(1, Math.min(Math.trunc(options.expiresInSeconds ?? 120), 600)) * 1_000).toISOString(),
      duplicates: await this.normalizeDuplicates(request, options.duplicates ?? [], true)
    };
    command.signature = OwnerApprovalStore.signCommand(command, key);
    await atomicWriteJson(join(this.commands, `${String(command.id)}.json`), command, true);
    return command;
  }

  async verifyCommand(document: JsonObject, key: Buffer, at = new Date()): Promise<JsonObject> {
    if (key.length < 32) throw new ApprovalError("审批 HMAC key 太短");
    const missing = REQUIRED_COMMAND_KEYS.filter((keyName) => !Object.hasOwn(document, keyName));
    if (missing.length) throw new ApprovalError(`审批命令缺少字段：${missing.join(", ")}`);
    const unknown = Object.keys(document).filter((keyName) => !COMMAND_KEYS.has(keyName));
    if (unknown.length) throw new ApprovalError(`审批命令包含未知字段：${unknown.sort().join(", ")}`);
    if (document.schema_version !== 1) throw new ApprovalError("不支持的审批命令版本");
    safeId(document.id, COMMAND_ID, "审批命令 ID");
    const action = commandString(document.action, "action", 20);
    if (!DECISIONS.has(action)) throw new ApprovalError("审批命令 action 非法");
    const requestId = safeId(document.request_id, REQUEST_ID, "请求 ID");
    const requestFingerprint = commandString(document.request_fingerprint, "request_fingerprint", 64);
    if (!FINGERPRINT.test(requestFingerprint)) throw new ApprovalError("审批命令 request_fingerprint 非法");
    commandString(document.reason, "reason");
    commandString(document.decided_by, "decided_by", 200);
    const created = parseDate(document.created_at, "created_at");
    const expires = parseDate(document.expires_at, "expires_at");
    if (expires.getTime() <= at.getTime()) throw new ApprovalError("审批命令已过期");
    if (created.getTime() > at.getTime() + 5 * 60 * 1_000) throw new ApprovalError("审批命令 created_at 超出允许时钟偏差");
    if (expires.getTime() <= created.getTime()) throw new ApprovalError("审批命令 expires_at 必须晚于 created_at");
    if (expires.getTime() - created.getTime() > 10 * 60 * 1_000) throw new ApprovalError("审批命令有效期超过 10 分钟上限");
    const signature = commandString(document.signature, "signature", 64);
    if (!FINGERPRINT.test(signature)) throw new ApprovalError("审批命令 signature 非法");
    const actualBytes = Buffer.from(signature, "ascii");
    const expectedBytes = Buffer.from(OwnerApprovalStore.signCommand(document, key), "ascii");
    if (actualBytes.length !== expectedBytes.length || !timingSafeEqual(actualBytes, expectedBytes)) throw new ApprovalError("审批命令签名无效");

    const request = this.validateRequestDocument(await this.loadRequest(requestId), requestId);
    if (requestFingerprint !== request.fingerprint) throw new ApprovalError("审批命令未绑定当前请求指纹");
    if (!Array.isArray(document.duplicates)) throw new ApprovalError("审批命令 duplicates 必须是数组");
    await this.normalizeDuplicates(request, document.duplicates, false);
    return request;
  }

  async processCommand(document: JsonObject, key: Buffer): Promise<JsonObject> {
    await this.verifyCommand(document, key);
    const decision = await this.applyDecision(String(document.request_id), {
      action: String(document.action) as "approve" | "deny",
      reason: String(document.reason), decidedBy: String(document.decided_by),
      expectedFingerprint: String(document.request_fingerprint),
      duplicates: Array.isArray(document.duplicates) ? document.duplicates.map(String) : []
    });
    return { schema_version: 1, command_id: document.id, status: "succeeded", processed_at: nowIso(), decision };
  }

  async waitForCommandResult(commandId: string, timeoutSeconds = 10): Promise<JsonObject> {
    const id = safeId(commandId, COMMAND_ID, "审批命令 ID");
    const deadline = Date.now() + Math.max(0, timeoutSeconds) * 1_000;
    const path = join(this.commandResults, `${id}.json`);
    while (true) {
      const result = await readJson<JsonObject | null>(path, null);
      if (result) return result;
      if (Date.now() >= deadline) throw new ApprovalError(`等待审批 Broker 超时：${id}`);
      await new Promise((done) => setTimeout(done, 100));
    }
  }
}

export async function loadApprovalKey(path = process.env.AGENELF_APPROVAL_KEY_FILE || "/agenelf/approval/key"): Promise<Buffer> {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink()) throw new ApprovalError(`审批 key 不是普通文件：${path}`);
  const key = Buffer.from((await readFile(path)).toString("utf8").trim(), "utf8");
  if (key.length < 32) throw new ApprovalError("审批 key 无效或过短");
  return key;
}

export class ApprovalRunner {
  readonly store: OwnerApprovalStore;
  readonly key: Buffer;
  constructor(root: string, key: Buffer) { this.store = new OwnerApprovalStore(root); this.key = key; }
  async initialize(): Promise<void> { await this.store.initialize(); }

  async processPath(path: string): Promise<"succeeded" | "failed" | "done" | "locked" | "invalid"> {
    const match = /(?:^|\/)(apc-[0-9a-f]{16})\.json$/.exec(path);
    if (!match) return "invalid";
    const info = await lstat(path).catch(() => null);
    if (!info || !info.isFile() || info.isSymbolicLink()) return "invalid";
    const commandId = match[1];
    const resultPath = join(this.store.commandResults, `${commandId}.json`);
    if (await regularFile(resultPath)) return "done";
    try {
      return await withDirectoryLock(join(this.store.locks, `${commandId}.lock`), async () => {
        if (await regularFile(resultPath)) return "done" as const;
        const document = await readJson<JsonObject | null>(path, null);
        let result: JsonObject;
        let status: "succeeded" | "failed";
        try {
          if (!document) throw new ApprovalError("审批命令不是合法 JSON object");
          if (document.id !== commandId) throw new ApprovalError(`审批命令文件与文档 ID 不一致：${commandId}`);
          result = await this.store.processCommand(document, this.key);
          status = "succeeded";
        } catch (error) {
          result = {
            schema_version: 1, command_id: commandId, status: "failed", processed_at: nowIso(),
            error: `${error instanceof Error ? error.name : "Error"}: ${error instanceof Error ? error.message : String(error)}`
          };
          status = "failed";
        }
        await atomicWriteJson(resultPath, result, true);
        return status;
      }, { timeoutMs: 250, staleMs: 60_000 });
    } catch (error) {
      if (error instanceof Error && error.message.includes("获取文件锁超时")) return "locked";
      throw error;
    }
  }

  async processOnce(): Promise<Record<string, number>> {
    let names: string[] = [];
    try { names = await readdir(this.store.commands); } catch { return {}; }
    const candidates: Array<{ name: string; mtime: number }> = [];
    for (const name of names.filter((item) => /^apc-[0-9a-f]{16}\.json$/.test(item))) {
      const info = await lstat(join(this.store.commands, name)).catch(() => null);
      if (info?.isFile() && !info.isSymbolicLink()) candidates.push({ name, mtime: info.mtimeMs });
    }
    candidates.sort((a, b) => a.mtime - b.mtime || a.name.localeCompare(b.name));
    const counts: Record<string, number> = {};
    for (const item of candidates) {
      const state = await this.processPath(join(this.store.commands, item.name));
      counts[state] = (counts[state] ?? 0) + 1;
    }
    return counts;
  }
}
