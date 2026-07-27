import { createConnection } from "node:net";
import { lstat, mkdir, readFile, readdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import { appendLine, atomicWriteJson, readJson, withDirectoryLock } from "./fs-store.ts";
import { randomId, sha256 } from "./canonical.ts";
import { parseSimpleYaml } from "./simple-yaml.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const CAPABILITY = "software.validation";
const REQUEST_ID = /^val-[0-9a-f]{16}$/;
const MAX_BODY_BYTES = 1_000_000;
const MAX_ASSERTIONS = 30;
const MAX_REDIRECTS = 5;

export type ValidationOperation = "run_check" | "run_suite";
export type ValidationState = "succeeded" | "failed" | "done" | "invalid" | "locked";

interface ValidationConfig {
  checks: Record<string, JsonObject>;
  suites: Record<string, JsonObject | JsonValue[]>;
}

function nowIso(): string { return new Date().toISOString(); }

function boundedInt(value: JsonValue | undefined, fallback: number, minimum: number, maximum: number): number {
  const parsed = typeof value === "number" ? Math.trunc(value) : Number(value ?? fallback);
  return Math.max(minimum, Math.min(Number.isFinite(parsed) ? parsed : fallback, maximum));
}

function asObject(value: JsonValue | undefined): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function asRecord(value: JsonValue | undefined, name: string): Record<string, JsonObject> {
  const object = asObject(value);
  if (!object) throw new Error(`${name} 必须是 mapping`);
  const result: Record<string, JsonObject> = {};
  for (const [key, item] of Object.entries(object)) {
    const entry = asObject(item);
    if (!entry) throw new Error(`${name}.${key} 必须是 mapping`);
    result[key] = entry;
  }
  return result;
}

function assertion(name: string, passed: boolean, detail: string): JsonObject {
  return { name, passed, detail: String(detail).slice(0, 1_000) };
}

function jsonPath(value: JsonValue, dottedPath: string): { found: boolean; value: JsonValue } {
  let current: JsonValue = value;
  for (const part of dottedPath.split(".")) {
    if (Array.isArray(current) && /^\d+$/.test(part)) {
      const index = Number(part);
      if (index >= 0 && index < current.length) { current = current[index]; continue; }
      return { found: false, value: null };
    }
    if (current && typeof current === "object" && !Array.isArray(current) && Object.hasOwn(current, part)) {
      current = current[part];
      continue;
    }
    return { found: false, value: null };
  }
  return { found: true, value: current };
}

async function fileExists(path: string): Promise<boolean> {
  try { await lstat(path); return true; } catch { return false; }
}

async function boundedBody(response: Response): Promise<{ text: string; bytes: number; truncated: boolean }> {
  if (!response.body) return { text: "", bytes: 0, truncated: false };
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  let truncated = false;
  while (true) {
    const item = await reader.read();
    if (item.done) break;
    const available = Math.max(0, MAX_BODY_BYTES - bytes);
    if (item.value.byteLength > available) {
      if (available) chunks.push(item.value.slice(0, available));
      bytes += available;
      truncated = true;
      await reader.cancel();
      break;
    }
    chunks.push(item.value);
    bytes += item.value.byteLength;
  }
  const buffer = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), bytes);
  return { text: buffer.toString("utf8"), bytes, truncated };
}

function validatedHttpUrl(raw: string, label: string): URL {
  let url: URL;
  try { url = new URL(raw); }
  catch { throw new Error(`${label} 的 URL 非法`); }
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error(`${label} 仅支持 HTTP/HTTPS`);
  if (!url.hostname || url.username || url.password) throw new Error(`${label} 的 URL 不得包含凭据且必须包含主机`);
  return url;
}

async function fetchBoundedRedirects(initial: URL, method: "GET" | "HEAD", timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let current = initial;
  try {
    for (let redirect = 0; redirect <= MAX_REDIRECTS; redirect += 1) {
      const response = await fetch(current, {
        method,
        headers: { "user-agent": "Agenelf-Validation/1.0" },
        signal: controller.signal,
        redirect: "manual"
      });
      if (response.status < 300 || response.status >= 400) return response;
      const location = response.headers.get("location");
      if (!location) return response;
      if (redirect >= MAX_REDIRECTS) throw new Error(`HTTP 重定向超过 ${MAX_REDIRECTS} 次`);
      current = validatedHttpUrl(new URL(location, current).toString(), "重定向目标");
    }
    throw new Error("HTTP 重定向状态异常");
  } finally {
    clearTimeout(timer);
  }
}

export class ValidationQueue {
  readonly root: string;
  readonly validationFile: string;
  readonly requests: string;
  readonly results: string;
  readonly locks: string;
  readonly auditPath: string;
  private config: ValidationConfig | null = null;

  constructor(root: string, validationFile = process.env.AGENELF_VALIDATION_FILE || join(root, "local", "validation.yaml")) {
    this.root = resolve(root);
    this.validationFile = resolve(validationFile);
    this.requests = join(this.root, "data", "validation-requests");
    this.results = join(this.root, "data", "validation-results");
    this.locks = join(this.root, "data", "validation-locks");
    this.auditPath = join(this.root, "logs", "validation.log");
  }

  async initialize(): Promise<void> {
    const info = await lstat(this.validationFile);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error(`验证配置不存在、不是普通文件或是符号链接：${this.validationFile}`);
    const parsed = parseSimpleYaml(await readFile(this.validationFile, "utf8"));
    const checks = asRecord(parsed.checks, "checks");
    const suitesObject = asObject(parsed.suites);
    if (!suitesObject) throw new Error("suites 必须是 mapping");
    const suites: Record<string, JsonObject | JsonValue[]> = {};
    for (const [key, value] of Object.entries(suitesObject)) {
      if (Array.isArray(value)) suites[key] = value;
      else {
        const object = asObject(value);
        if (!object) throw new Error(`suites.${key} 必须是 mapping 或数组`);
        suites[key] = object;
      }
    }
    this.config = { checks, suites };
    await mkdir(this.requests, { recursive: true });
    await mkdir(this.results, { recursive: true });
    await mkdir(this.locks, { recursive: true });
  }

  async reload(): Promise<void> { this.config = null; await this.initialize(); }

  private ready(): ValidationConfig {
    if (!this.config) throw new Error("ValidationQueue 尚未初始化");
    return this.config;
  }

  hasCheck(name: string): boolean { return Object.hasOwn(this.ready().checks, name); }
  hasSuite(name: string): boolean { return Object.hasOwn(this.ready().suites, name); }

  checkConfig(name: string): JsonObject {
    const value = this.ready().checks[name];
    if (!value) throw new Error(`未知验证检查：${name}`);
    return value;
  }

  catalog(): JsonObject {
    const config = this.ready();
    const checks = Object.entries(config.checks).map(([name, cfg]) => ({
      name,
      type: String(cfg.type ?? "unknown"),
      description: String(cfg.description ?? ""),
      tags: Array.isArray(cfg.tags) ? cfg.tags.map(String).slice(0, 20) : []
    }));
    const suites = Object.keys(config.suites).map((name) => ({ name, checks: this.suiteMembers(name) }));
    return { checks, suites } as unknown as JsonObject;
  }

  static canonicalPayload(operation: ValidationOperation, target: string): JsonObject {
    if (operation !== "run_check" && operation !== "run_suite") throw new Error(`不支持的验证操作：${operation}`);
    const normalized = String(target || "").trim();
    if (!normalized) throw new Error("验证目标不能为空");
    return { capability: CAPABILITY, operation, target: normalized, parameters: {} };
  }

  static fingerprint(payload: JsonObject): string { return sha256(payload); }

  async submit(operation: ValidationOperation, target: string, summary: string, createdBy = "agenelf-node-agent"): Promise<JsonObject> {
    const payload = ValidationQueue.canonicalPayload(operation, target);
    if (operation === "run_check" && !this.hasCheck(target)) throw new Error(`未知验证检查：${target}`);
    if (operation === "run_suite" && !this.hasSuite(target)) throw new Error(`未知验证套件：${target}`);
    const id = randomId("val-", 16);
    const request: JsonObject = {
      schema_version: 1,
      id,
      ...payload,
      risk: "read",
      summary: String(summary || "").trim(),
      fingerprint: ValidationQueue.fingerprint(payload),
      created_at: nowIso(),
      created_by: createdBy
    };
    await atomicWriteJson(join(this.requests, `${id}.json`), request, true);
    await appendLine(this.auditPath, `[${nowIso()}] [validation_submitted] ${id} ${operation} target=${target}`);
    return request;
  }

  async get(id: string): Promise<JsonObject> {
    if (!REQUEST_ID.test(id)) throw new Error(`非法验证 ID：${id}`);
    const request = await readJson<JsonObject | null>(join(this.requests, `${id}.json`), null);
    if (!request) return { id, status: "not_found" };
    const result = await readJson<JsonObject | null>(join(this.results, `${id}.json`), null);
    return result ? { id, status: String(result.status ?? "finished"), request, result } : { id, status: "queued", request };
  }

  suiteMembers(name: string): string[] {
    const raw = this.ready().suites[name];
    if (!raw) throw new Error(`未知验证套件：${name}`);
    const members = Array.isArray(raw) ? raw : raw.checks;
    if (!Array.isArray(members) || !members.length) throw new Error(`套件 ${name} 没有检查项`);
    const result = members.slice(0, 100).map(String);
    const unknown = result.filter((item) => !this.hasCheck(item));
    if (unknown.length) throw new Error(`套件 ${name} 引用了未知检查：${unknown.join(", ")}`);
    return result;
  }
}

export class ValidationRunner {
  readonly queue: ValidationQueue;

  constructor(root: string, validationFile?: string) { this.queue = new ValidationQueue(root, validationFile); }
  async initialize(): Promise<void> { await this.queue.initialize(); }

  private validateRequest(request: JsonObject): { operation: ValidationOperation; target: string } {
    if (request.schema_version !== 1) throw new Error("不支持的验证请求版本");
    const operation = String(request.operation ?? "") as ValidationOperation;
    const target = String(request.target ?? "");
    const payload = ValidationQueue.canonicalPayload(operation, target);
    if (request.capability !== CAPABILITY) throw new Error("请求能力不是 software.validation");
    if (request.risk !== "read") throw new Error("软件验证必须是只读风险级别");
    const parameters = request.parameters;
    if (parameters !== null && parameters !== undefined && (!asObject(parameters) || Object.keys(parameters).length)) {
      throw new Error("验证请求不得携带自由参数");
    }
    if (request.fingerprint !== ValidationQueue.fingerprint(payload)) throw new Error("验证请求指纹不匹配，文件可能被篡改");
    if (operation === "run_check" && !this.queue.hasCheck(target)) throw new Error(`未知验证检查：${target}`);
    if (operation === "run_suite" && !this.queue.hasSuite(target)) throw new Error(`未知验证套件：${target}`);
    return { operation, target };
  }

  private async httpCheck(name: string, cfg: JsonObject): Promise<JsonObject> {
    const url = validatedHttpUrl(String(cfg.url ?? "").trim(), `HTTP 检查 ${name}`);
    const methodRaw = String(cfg.method ?? "GET").toUpperCase();
    if (methodRaw !== "GET" && methodRaw !== "HEAD") throw new Error(`HTTP 检查 ${name} 仅支持 GET/HEAD`);
    const method = methodRaw as "GET" | "HEAD";
    const timeout = boundedInt(cfg.timeout_seconds, 5, 1, 30) * 1_000;
    const expectedRaw = cfg.expected_status;
    const expected = (Array.isArray(expectedRaw) ? expectedRaw : [expectedRaw ?? 200])
      .filter((item): item is number => typeof item === "number")
      .slice(0, 20);
    if (!expected.length) expected.push(200);
    const started = performance.now();
    let statusCode: number | null = null;
    let body = { text: "", bytes: 0, truncated: false };
    let networkError = "";
    try {
      const response = await fetchBoundedRedirects(url, method, timeout);
      statusCode = response.status;
      body = method === "HEAD" ? body : await boundedBody(response);
    } catch (error) {
      networkError = `${error instanceof Error ? error.name : "Error"}: ${error instanceof Error ? error.message : String(error)}`;
    }
    const latencyMs = Math.round((performance.now() - started) * 100) / 100;
    const assertions: JsonObject[] = [
      assertion("network", !networkError, networkError || "连接成功"),
      assertion("status", statusCode !== null && expected.includes(statusCode), `实际=${statusCode}，期望=${JSON.stringify(expected)}`)
    ];
    if (cfg.max_latency_ms !== undefined) {
      const limit = boundedInt(cfg.max_latency_ms, 5_000, 1, 300_000);
      assertions.push(assertion("latency", latencyMs <= limit, `实际=${latencyMs}ms，最大=${limit}ms`));
    }
    const contains = Array.isArray(cfg.contains) ? cfg.contains : cfg.contains === undefined ? [] : [cfg.contains];
    for (const [index, needle] of contains.slice(0, 10).entries()) {
      const text = String(needle);
      assertions.push(assertion(`contains[${index}]`, body.text.includes(text), `响应正文${body.text.includes(text) ? "包含" : "不包含"}指定文本`));
    }
    const equals = asObject(cfg.json_equals);
    if (equals && Object.keys(equals).length) {
      let parsed: JsonValue = null;
      let parseError = "";
      try { parsed = JSON.parse(body.text) as JsonValue; }
      catch (error) { parseError = error instanceof Error ? error.message : String(error); }
      assertions.push(assertion("json_parse", !parseError, parseError || "JSON 解析成功"));
      if (!parseError) {
        for (const [path, expectedValue] of Object.entries(equals).slice(0, 10)) {
          const actual = jsonPath(parsed, path);
          assertions.push(assertion(`json_equals:${path}`, actual.found && JSON.stringify(actual.value) === JSON.stringify(expectedValue), `actual=${JSON.stringify(actual.value)}, expected=${JSON.stringify(expectedValue)}`));
        }
      }
    }
    const limited = assertions.slice(0, MAX_ASSERTIONS);
    return {
      name,
      type: "http",
      passed: limited.length > 0 && limited.every((item) => item.passed === true),
      latency_ms: latencyMs,
      observed: { status_code: statusCode, body_bytes: body.bytes, body_truncated: body.truncated },
      assertions: limited
    };
  }

  private async tcpCheck(name: string, cfg: JsonObject): Promise<JsonObject> {
    const host = String(cfg.host ?? "").trim();
    const port = boundedInt(cfg.port, 0, 1, 65_535);
    const timeout = boundedInt(cfg.timeout_seconds, 5, 1, 30) * 1_000;
    if (!host || !port) throw new Error(`TCP 检查 ${name} 缺少 host/port`);
    const started = performance.now();
    let errorText = "";
    await new Promise<void>((resolvePromise) => {
      const socket = createConnection({ host, port });
      let settled = false;
      const finish = (error = "") => {
        if (settled) return;
        settled = true;
        errorText = error;
        clearTimeout(timer);
        socket.destroy();
        resolvePromise();
      };
      const timer = setTimeout(() => finish("TimeoutError: TCP 连接超时"), timeout);
      socket.once("connect", () => finish());
      socket.once("error", (error) => finish(`${error.name}: ${error.message}`));
    });
    const latencyMs = Math.round((performance.now() - started) * 100) / 100;
    const assertions: JsonObject[] = [assertion("connect", !errorText, errorText || "TCP 连接成功")];
    if (cfg.max_latency_ms !== undefined) {
      const limit = boundedInt(cfg.max_latency_ms, 5_000, 1, 300_000);
      assertions.push(assertion("latency", latencyMs <= limit, `实际=${latencyMs}ms，最大=${limit}ms`));
    }
    return { name, type: "tcp", passed: assertions.every((item) => item.passed === true), latency_ms: latencyMs, observed: { connected: !errorText }, assertions };
  }

  async runCheck(name: string): Promise<JsonObject> {
    const cfg = this.queue.checkConfig(name);
    const type = String(cfg.type ?? "").toLowerCase();
    const started = nowIso();
    try {
      const result = type === "http"
        ? await this.httpCheck(name, cfg)
        : type === "tcp"
          ? await this.tcpCheck(name, cfg)
          : (() => { throw new Error(`检查 ${name} 使用不支持的类型：${type}`); })();
      return { ...result, started_at: started, finished_at: nowIso() };
    } catch (error) {
      return {
        name,
        type: type || "unknown",
        passed: false,
        latency_ms: null,
        observed: {},
        assertions: [assertion("configuration_or_execution", false, `${error instanceof Error ? error.name : "Error"}: ${error instanceof Error ? error.message : String(error)}`)],
        started_at: started,
        finished_at: nowIso()
      };
    }
  }

  async execute(request: JsonObject): Promise<JsonObject> {
    const { operation, target } = this.validateRequest(request);
    const started = nowIso();
    const names = operation === "run_check" ? [target] : this.queue.suiteMembers(target);
    const checks: JsonObject[] = [];
    for (const name of names) checks.push(await this.runCheck(name));
    const passed = checks.filter((item) => item.passed === true).length;
    const failed = checks.length - passed;
    return {
      schema_version: 1,
      id: String(request.id ?? ""),
      capability: CAPABILITY,
      operation,
      target,
      status: failed === 0 && checks.length ? "succeeded" : "failed",
      started_at: started,
      finished_at: nowIso(),
      summary: `${passed}/${checks.length} 个检查通过，${failed} 个失败`,
      passed,
      failed,
      checks
    };
  }

  async processRequest(path: string): Promise<ValidationState> {
    const request = await readJson<JsonObject | null>(path, null);
    if (!request) return "invalid";
    const id = String(request.id ?? "");
    if (!REQUEST_ID.test(id)) return "invalid";
    const resultPath = join(this.queue.results, `${id}.json`);
    if (await fileExists(resultPath)) return "done";
    try {
      return await withDirectoryLock(join(this.queue.locks, `${id}.lock`), async () => {
        if (await fileExists(resultPath)) return "done" as ValidationState;
        try {
          const result = await this.execute(request);
          await atomicWriteJson(resultPath, result, true);
          await appendLine(this.queue.auditPath, `[${nowIso()}] [${String(result.status)}] ${id} ${String(request.operation)} ${String(request.target)}`);
          return String(result.status) as ValidationState;
        } catch (error) {
          const result: JsonObject = {
            schema_version: 1,
            id,
            capability: CAPABILITY,
            status: "failed",
            reason: `${error instanceof Error ? error.name : "Error"}: ${error instanceof Error ? error.message : String(error)}`,
            finished_at: nowIso()
          };
          await atomicWriteJson(resultPath, result, true);
          await appendLine(this.queue.auditPath, `[${nowIso()}] [failed] ${id} ${String(result.reason)}`);
          return "failed";
        }
      }, { timeoutMs: 250, staleMs: 60_000 });
    } catch (error) {
      if (error instanceof Error && error.message.includes("获取文件锁超时")) return "locked";
      throw error;
    }
  }

  async processOnce(): Promise<Record<string, number>> {
    await mkdir(this.queue.requests, { recursive: true });
    const counts: Record<string, number> = {};
    for (const name of (await readdir(this.queue.requests)).filter((item) => /^val-[0-9a-f]{16}\.json$/.test(item)).sort()) {
      const state = await this.processRequest(join(this.queue.requests, name));
      counts[state] = (counts[state] ?? 0) + 1;
    }
    return counts;
  }
}
