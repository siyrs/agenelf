import { createHash } from "node:crypto";
import { lstat, mkdir, readdir, readFile } from "node:fs/promises";
import { basename, join, resolve } from "node:path";
import { atomicWriteJson, readJson, withDirectoryLock } from "./fs-store.ts";
import { MemoryStore } from "./memory-store.ts";
import { OperationQueue } from "./operation-queue.ts";
import { redactSensitiveText, sanitizeObject } from "./privacy.ts";
import { RepairCatalog, type RepairRequest } from "./repair.ts";
import { parseSimpleYaml } from "./simple-yaml.ts";
import { TaskStore } from "./task-store.ts";
import { randomId, sha256 } from "./canonical.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const MAX_JSON_BYTES = 2 * 1024 * 1024;
const REPAIR_ID = /^repair-[0-9a-f]{16}$/;
const TASK_ID = /^(?:ntask-[0-9a-f]{16}|task-[A-Za-z0-9][A-Za-z0-9._-]{0,120})$/;
const PRIORITIES = new Set(["P0", "P1", "P2", "P3"]);
const INTENTION_STATUSES = new Set(["proposed", "planned", "active", "awaiting_promotion", "blocked", "completed", "dismissed"]);
const OPEN_INTENTION_STATUSES = new Set(["proposed", "planned", "active", "awaiting_promotion", "blocked"]);

function now(): string { return new Date().toISOString(); }
function object(value: JsonValue | undefined, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} 必须是 object`);
  return value;
}
function safeText(value: unknown, limit = 2_000): string {
  return redactSensitiveText(value).trim().slice(0, limit);
}
function bounded(value: unknown, fallback: number, min: number, max: number): number {
  const number = Number(value ?? fallback);
  return Number.isFinite(number) ? Math.max(min, Math.min(Math.trunc(number), max)) : fallback;
}
async function regularFile(path: string, maxBytes = MAX_JSON_BYTES): Promise<boolean> {
  try {
    const info = await lstat(path);
    return info.isFile() && !info.isSymbolicLink() && info.size <= maxBytes;
  } catch { return false; }
}
async function safeDocument(path: string): Promise<JsonObject | null> {
  if (!(await regularFile(path))) return null;
  const value = await readJson<JsonValue | null>(path, null);
  return value && typeof value === "object" && !Array.isArray(value) ? sanitizeObject(value) : null;
}
async function safeArray(path: string): Promise<JsonObject[]> {
  if (!(await regularFile(path))) return [];
  const value = await readJson<JsonValue | null>(path, null);
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => item && typeof item === "object" && !Array.isArray(item) ? [sanitizeObject(item)] : []);
}
async function listJson(directory: string, pattern: RegExp): Promise<Array<{ name: string; value: JsonObject }>> {
  try {
    const names = (await readdir(directory)).filter((name) => pattern.test(name)).sort();
    const rows = await Promise.all(names.map(async (name) => ({ name, value: await safeDocument(join(directory, name)) })));
    return rows.flatMap((row) => row.value ? [{ name: row.name, value: row.value }] : []);
  } catch { return []; }
}
function stripPatch(request: JsonObject): JsonObject {
  const result = { ...request };
  delete result.patch;
  return result;
}

export class LocalContextView {
  readonly root: string;
  readonly localDir: string;
  constructor(root: string) { this.root = resolve(root); this.localDir = join(this.root, "local"); }

  private async inspect(name: string): Promise<{ loaded: boolean; warning: string; keys: string[] }> {
    const path = join(this.localDir, `${name}.yaml`);
    try {
      const info = await lstat(path);
      if (!info.isFile() || info.isSymbolicLink()) return { loaded: false, warning: `${name}.yaml 不是普通文件`, keys: [] };
      if (info.size > 1024 * 1024) return { loaded: false, warning: `${name}.yaml 超过 1 MiB`, keys: [] };
      const value = parseSimpleYaml(await readFile(path, "utf8"));
      return { loaded: true, warning: "", keys: Object.keys(value).sort().slice(0, 100) };
    } catch (error) {
      return { loaded: false, warning: `${name}.yaml：${error instanceof Error ? error.message : String(error)}`, keys: [] };
    }
  }

  async status(): Promise<JsonObject> {
    const [profile, preferences, models] = await Promise.all([this.inspect("profile"), this.inspect("preferences"), this.inspect("models")]);
    const warnings = [profile.warning, preferences.warning, models.warning].filter(Boolean);
    return {
      local_dir: this.localDir,
      profile_loaded: profile.loaded,
      preferences_loaded: preferences.loaded,
      models_loaded: models.loaded,
      local_context_ready: profile.loaded || preferences.loaded,
      warnings,
      files: {
        profile: { loaded: profile.loaded, keys: profile.keys },
        preferences: { loaded: preferences.loaded, keys: preferences.keys },
        models: { loaded: models.loaded, keys: models.keys }
      },
      credentials_exposed: false
    };
  }

  async reload(): Promise<JsonObject> { return { ...(await this.status()), reloaded: true, reloaded_at: now() }; }
}

export class SelfDevelopmentStore {
  readonly root: string;
  readonly directory: string;
  readonly statePath: string;
  readonly reflectionsPath: string;
  readonly intentionsPath: string;
  readonly lockPath: string;

  constructor(root: string) {
    this.root = resolve(root);
    this.directory = join(this.root, "local", "self");
    this.statePath = join(this.directory, "state.json");
    this.reflectionsPath = join(this.directory, "reflections.json");
    this.intentionsPath = join(this.directory, "intentions.json");
    this.lockPath = join(this.directory, ".node-self.lock");
  }

  async initialize(): Promise<void> { await mkdir(this.directory, { recursive: true }); }

  private async state(): Promise<JsonObject> {
    const existing = await safeDocument(this.statePath);
    if (existing) return existing;
    const created = {
      schema_version: 1,
      continuity_id: randomId("self-", 32),
      created_at: now(),
      updated_at: now(),
      last_reflection_at: null,
      episode_cursor: 0,
      operational_identity: {
        kind: "persistent tool-using software agent",
        purpose: "持续理解主人目标，以证据完成任务，并在安全边界内改进通用能力",
        principles: ["主人目标与明确授权优先", "证据优先于自我宣称", "安全边界优先于速度", "把失败与结果沉淀为可验证的下一步"],
        consciousness_claim: false
      }
    } as JsonObject;
    await atomicWriteJson(this.statePath, created);
    return created;
  }

  async status(): Promise<JsonObject> {
    await this.initialize();
    const [state, reflections, intentions] = await Promise.all([this.state(), safeArray(this.reflectionsPath), safeArray(this.intentionsPath)]);
    const counts: JsonObject = {};
    for (const status of [...INTENTION_STATUSES].sort()) counts[status] = 0;
    for (const item of intentions) {
      const status = String(item.status ?? "proposed");
      if (INTENTION_STATUSES.has(status)) counts[status] = Number(counts[status] ?? 0) + 1;
    }
    const open = intentions
      .filter((item) => OPEN_INTENTION_STATUSES.has(String(item.status ?? "")))
      .sort((a, b) => String(a.priority ?? "P9").localeCompare(String(b.priority ?? "P9")) || String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? "")))
      .slice(0, 5);
    return {
      schema_version: 1,
      self_dir: this.directory,
      continuity_id: state.continuity_id ?? "",
      operational_identity: state.operational_identity ?? {},
      last_reflection_at: state.last_reflection_at ?? null,
      episode_cursor: state.episode_cursor ?? 0,
      reflection_count: reflections.length,
      latest_reflection: reflections.at(-1) ?? null,
      intention_count: intentions.length,
      intention_status_counts: counts,
      open_intentions: open,
      policy: { max_reflections: 200, max_intentions: 100, auto_pursue: false, consciousness_claim: false }
    };
  }

  async reflections(limit = 10): Promise<JsonObject[]> {
    const values = await safeArray(this.reflectionsPath);
    return values.slice(-bounded(limit, 10, 0, 50)).reverse();
  }

  async reflect(note: string, deep: boolean, evidence: JsonObject = {}): Promise<JsonObject> {
    await this.initialize();
    return withDirectoryLock(this.lockPath, async () => {
      const reflections = await safeArray(this.reflectionsPath);
      const summary = safeText(note, 2_000) || "Node Runtime 完成一次证据驱动复盘";
      const reflection: JsonObject = {
        schema_version: 1,
        id: `reflection-${new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14)}-${randomId("", 6)}`,
        at: now(),
        trigger: "manual",
        summary,
        observations: ["已从 Node Runtime、Runner heartbeat 和可信结果文件形成复盘"],
        lessons: ["继续以合同测试和真实 Runner 证据推动迁移", "不以模型自评替代验证证据"],
        evidence: Object.entries(evidence).slice(0, 20).map(([key, value]) => `${key}=${safeText(value, 500)}`),
        generated_intention_ids: [],
        deep_reflection: Boolean(deep),
        deep_warning: deep ? "Node 原生深度反思尚未调用模型；本次使用确定性证据复盘" : "",
        consciousness_claim: false
      };
      reflections.push(reflection);
      await atomicWriteJson(this.reflectionsPath, reflections.slice(-200) as unknown as JsonValue);
      const state = await this.state();
      state.last_reflection_at = reflection.at;
      state.updated_at = now();
      await atomicWriteJson(this.statePath, state);
      return reflection;
    });
  }

  async intentions(status = "", limit = 20): Promise<JsonObject[]> {
    const normalized = String(status ?? "").trim();
    if (normalized && !INTENTION_STATUSES.has(normalized)) throw new Error(`未知意向状态：${normalized}`);
    return (await safeArray(this.intentionsPath))
      .filter((item) => !normalized || String(item.status ?? "") === normalized)
      .sort((a, b) => String(a.priority ?? "P9").localeCompare(String(b.priority ?? "P9")) || String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? "")))
      .slice(0, bounded(limit, 20, 0, 100));
  }

  async intention(id: string): Promise<JsonObject> {
    if (!/^intent-[A-Za-z0-9._-]+$/.test(id)) throw new Error(`非法改进意向 ID：${id}`);
    const found = (await safeArray(this.intentionsPath)).find((item) => item.id === id);
    if (!found) throw new Error(`改进意向不存在：${id}`);
    return found;
  }

  async createIntention(input: { title: string; rationale?: string; priority?: string; acceptanceCriteria?: string[]; evidence?: string[]; source?: string }): Promise<JsonObject> {
    await this.initialize();
    const title = safeText(input.title, 300);
    if (!title) throw new Error("改进意向标题不能为空");
    const priority = String(input.priority ?? "P2").toUpperCase();
    if (!PRIORITIES.has(priority)) throw new Error("priority 必须是 P0-P3");
    const fingerprint = createHash("sha256").update(title.toLowerCase().replace(/\s+/g, " "), "utf8").digest("hex");
    return withDirectoryLock(this.lockPath, async () => {
      const intentions = await safeArray(this.intentionsPath);
      const existing = intentions.find((item) => item.fingerprint === fingerprint && OPEN_INTENTION_STATUSES.has(String(item.status ?? "")));
      if (existing) return { created: false, intention: existing };
      const at = now();
      const intention: JsonObject = {
        schema_version: 1,
        id: `intent-${at.replace(/[-:TZ.]/g, "").slice(0, 14)}-${randomId("", 6)}`,
        title,
        rationale: safeText(input.rationale, 2_000),
        priority,
        status: "proposed",
        operational_commitment: { P0: 100, P1: 80, P2: 60, P3: 40 }[priority as "P0" | "P1" | "P2" | "P3"],
        acceptance_criteria: (input.acceptanceCriteria ?? []).map((item) => safeText(item, 800)).filter(Boolean).slice(0, 12),
        evidence: (input.evidence ?? []).map((item) => safeText(item, 1_000)).filter(Boolean).slice(0, 20),
        source: safeText(input.source ?? "api", 100),
        owner_aligned: true,
        fingerprint,
        created_at: at,
        updated_at: at,
        attempts: 0,
        last_note: "",
        linked_cycle_id: null,
        evolution_session_id: null,
        consciousness_claim: false
      };
      if (!(intention.acceptance_criteria as JsonValue[]).length) intention.acceptance_criteria = ["有明确、可复现的验收证据", "不绕过权限、审批、测试和晋升安全门"];
      intentions.push(intention);
      await atomicWriteJson(this.intentionsPath, intentions.slice(-100) as unknown as JsonValue);
      return { created: true, intention };
    });
  }

  async pursue(id: string, taskStore: TaskStore, applyChanges: boolean): Promise<JsonObject> {
    await this.initialize();
    return withDirectoryLock(this.lockPath, async () => {
      const intentions = await safeArray(this.intentionsPath);
      const index = intentions.findIndex((item) => item.id === id);
      if (index < 0) throw new Error(`改进意向不存在：${id}`);
      const current = intentions[index];
      if (["completed", "dismissed"].includes(String(current.status ?? ""))) throw new Error("终态意向不能继续推进");
      const criteria = Array.isArray(current.acceptance_criteria) ? current.acceptance_criteria.map(String) : [];
      const task = await taskStore.create({ title: String(current.title ?? "改进意向"), goal: String(current.rationale ?? current.title ?? ""), acceptanceCriteria: criteria });
      current.status = applyChanges ? "awaiting_promotion" : "planned";
      current.updated_at = now();
      current.attempts = Number(current.attempts ?? 0) + 1;
      current.linked_task_id = task.id;
      current.last_note = applyChanges ? "已创建 Node Task；代码变更必须继续走 owner-authorized Self-upgrade" : "已创建 Node 计划任务";
      intentions[index] = current;
      await atomicWriteJson(this.intentionsPath, intentions as unknown as JsonValue);
      return {
        intention: current,
        task,
        apply_changes_requested: Boolean(applyChanges),
        next_action: applyChanges ? "通过主人授权 Self-upgrade 生成并批准精确候选" : "推进 Node Task 并收集验收证据"
      };
    });
  }
}

export class NativeCompatibilityService {
  readonly root: string;
  readonly memory: MemoryStore;
  readonly tasks: TaskStore;
  readonly operations: OperationQueue;
  readonly repairCatalog: RepairCatalog;
  readonly local: LocalContextView;
  readonly self: SelfDevelopmentStore;

  constructor(root: string) {
    this.root = resolve(root);
    this.memory = new MemoryStore(this.root);
    this.tasks = new TaskStore(this.root);
    this.operations = new OperationQueue(this.root);
    this.repairCatalog = new RepairCatalog(this.root);
    this.local = new LocalContextView(this.root);
    this.self = new SelfDevelopmentStore(this.root);
  }

  async initialize(): Promise<void> { await this.self.initialize(); }

  async remember(kind: string, content: string): Promise<JsonObject> {
    if (!new Set(["fact", "preference"]).has(kind)) throw new Error("kind 只能是 fact 或 preference");
    return await this.memory.add(kind, content) as unknown as JsonObject;
  }
  async searchMemory(query: string, limit = 5): Promise<JsonObject> {
    if (!query.trim()) throw new Error("q 不能为空");
    return { query, results: await this.memory.recall(query, bounded(limit, 5, 1, 20)) as unknown as JsonValue };
  }

  async approvals(): Promise<JsonObject> {
    const pending: JsonObject[] = [];
    for (const [kind, directory] of [["operation", "ops-requests"], ["authorization", "auth-requests"]] as const) {
      const rows = await listJson(join(this.root, "data", directory), /^(?:op|auth)-[0-9a-f]{12,16}\.json$/);
      for (const { value } of rows) {
        const id = String(value.id ?? "");
        if (!id) continue;
        if (await regularFile(join(this.root, "data", "auth-decisions", `${id}.json`))) continue;
        if (id.startsWith("op-") && await regularFile(join(this.root, "data", "ops-results", `${id}.json`))) continue;
        const expiry = Date.parse(String(value.expires_at ?? ""));
        if (Number.isFinite(expiry) && expiry <= Date.now()) continue;
        pending.push({
          operation_id: id,
          kind,
          summary: value.summary ?? value.detail ?? value.reason ?? "",
          operation: value.operation ?? value.action ?? "",
          target: value.target ?? value.skill ?? "",
          risk: value.risk ?? "",
          created_at: value.created_at ?? "",
          expires_at: value.expires_at ?? "",
          fingerprint: value.fingerprint ?? ""
        });
      }
    }
    pending.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
    return { pending, hint: "本端点只读。审批只能由主人通过 CLI /approve 或 /deny 执行。" };
  }

  private async boardTasks(): Promise<JsonObject[]> {
    const board = await safeDocument(join(this.root, "workspace", "tasks", "board.json"));
    return Array.isArray(board?.tasks) ? board.tasks.flatMap((item) => item && typeof item === "object" && !Array.isArray(item) ? [sanitizeObject(item)] : []) : [];
  }
  private async engineTasks(): Promise<JsonObject[]> {
    return (await listJson(join(this.root, "data", "tasks"), /^task-[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.json$/)).map((row) => row.value);
  }
  async listTasks(status = ""): Promise<JsonObject> {
    const node = (await this.tasks.list(500)).map((task) => ({ source: "node", ...task } as unknown as JsonObject));
    const board = (await this.boardTasks()).map((task) => {
      const steps = Array.isArray(task.steps) ? task.steps : [];
      const done = steps.filter((step) => step && typeof step === "object" && !Array.isArray(step) && (step as JsonObject).status === "done").length;
      return { id: task.id ?? "", source: "board", title: task.title ?? "", status: task.status ?? "", priority: task.priority ?? "", progress: `${done}/${steps.length}`, created_at: task.created_at ?? "", updated_at: task.updated_at ?? "", done_at: task.done_at ?? null, linked_intention: task.linked_intention ?? null, block_reason: task.block_reason ?? "" } as JsonObject;
    });
    const engine = (await this.engineTasks()).map((task) => ({ source: "engine", ...task } as JsonObject));
    const tasks = [...node, ...board, ...engine]
      .filter((task) => !status || String(task.status ?? "") === status)
      .sort((a, b) => String(b.updated_at ?? b.created_at ?? "").localeCompare(String(a.updated_at ?? a.created_at ?? "")));
    return { tasks, count: tasks.length, sources: { node: "data/node-tasks", board: "workspace/tasks/board.json", engine: "data/tasks" } };
  }
  async task(id: string): Promise<JsonObject> {
    if (!TASK_ID.test(id) || id.includes("..")) throw new Error(`非法任务 ID：${id}`);
    if (id.startsWith("ntask-")) return { source: "node", task: await this.tasks.get(id) as unknown as JsonObject };
    for (const task of await this.boardTasks()) if (task.id === id) return { source: "board", task };
    for (const task of await this.engineTasks()) if (task.id === id) return { source: "engine", task };
    throw new Error(`任务不存在：${id}`);
  }

  async repairCatalogView(): Promise<JsonObject> {
    await this.repairCatalog.initialize();
    const config = parseSimpleYaml(await readFile(this.repairCatalog.configFile, "utf8"));
    const repositories = object(config.repositories, "repositories");
    const rows = Object.entries(repositories).flatMap(([alias, raw]) => {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
      const profile = raw as JsonObject;
      return [{ alias, description: safeText(profile.description, 500), default_test_profile: String(profile.default_test_profile ?? ""), allowed_test_profiles: Array.isArray(profile.allowed_test_profiles) ? profile.allowed_test_profiles.map(String).slice(0, 20) : [], language: safeText(profile.language, 100) }];
    });
    return { schema_version: 1, repositories: rows, credentials_exposed: false };
  }

  async submitRepair(input: { repository: string; unifiedDiff: string; testProfile?: string; expectedBase?: string; summary?: string }): Promise<JsonObject> {
    await this.repairCatalog.initialize();
    const repository = input.repository.trim();
    const profile = this.repairCatalog.repositories.get(repository);
    if (!profile) throw new Error(`未知代码仓库别名：${repository}`);
    const testProfile = (input.testProfile || profile.defaultTestProfile).trim();
    if (!profile.allowedTestProfiles.includes(testProfile)) throw new Error(`仓库 ${repository} 未允许测试配置 ${testProfile}`);
    const patch = String(input.unifiedDiff ?? "");
    const patchBytes = Buffer.byteLength(patch, "utf8");
    if (!patch || patch.includes("\0") || !patch.includes("diff --git ")) throw new Error("必须提交无 NUL 的 git unified diff");
    if (patchBytes > 262_144 || patchBytes > profile.maxPatchBytes) throw new Error("补丁超过允许大小");
    if (redactSensitiveText(patch) !== patch) throw new Error("补丁包含疑似凭据");
    const expectedBase = String(input.expectedBase ?? "").trim().toLowerCase();
    if (expectedBase && !/^[0-9a-f]{7,64}$/.test(expectedBase)) throw new Error("expected_base 必须是 7-64 位 Git SHA");
    const patchDigest = createHash("sha256").update(patch, "utf8").digest("hex");
    const parameters = { test_profile: testProfile, patch_sha256: patchDigest, patch_bytes: patchBytes, expected_base: expectedBase };
    const payload = { capability: "code.repair", operation: "apply_patch_and_test", target: repository, parameters } as unknown as JsonObject;
    const request: RepairRequest = {
      schema_version: 1,
      id: randomId("repair-", 16),
      capability: "code.repair",
      operation: "apply_patch_and_test",
      target: repository,
      parameters,
      risk: "read",
      summary: safeText(input.summary, 1_000),
      patch,
      fingerprint: sha256(payload as unknown as JsonValue),
      created_at: now(),
      created_by: "agenelf-node-api"
    };
    await atomicWriteJson(join(this.root, "data", "repair-requests", `${request.id}.json`), request as unknown as JsonObject, true);
    return { id: request.id, status: "queued", request: stripPatch(request as unknown as JsonObject) };
  }

  async repair(id: string, waitSeconds = 0): Promise<JsonObject> {
    if (!REPAIR_ID.test(id)) throw new Error(`非法代码修复 ID：${id}`);
    const deadline = Date.now() + bounded(waitSeconds, 0, 0, 15) * 1_000;
    while (true) {
      const request = await safeDocument(join(this.root, "data", "repair-requests", `${id}.json`));
      if (!request) return { id, status: "not_found" };
      const result = await safeDocument(join(this.root, "data", "repair-results", `${id}.json`));
      if (result) return { id, status: String(result.status ?? "finished"), request: stripPatch(request), result };
      if (Date.now() >= deadline) return { id, status: "queued", request: stripPatch(request) };
      await new Promise((done) => setTimeout(done, 100));
    }
  }

  async evolutionStatus(): Promise<JsonObject> {
    const session = await safeDocument(join(this.root, "data", "evolution-session.json"));
    const requests: JsonObject[] = [];
    for (const [source, directory] of [["candidate", join(this.root, "app-tmp", "promote-requests")], ["promoted", join(this.root, "data", "promote-requests")]] as const) {
      for (const row of await listJson(directory, /\.json$/)) requests.push({ source, file: basename(row.name), ...row.value });
    }
    requests.sort((a, b) => String(b.created_at ?? b.updated_at ?? "").localeCompare(String(a.created_at ?? a.updated_at ?? "")));
    return { root: this.root, session, promotion_requests: requests.slice(0, 10) };
  }

  async selfUpgradeStatus(): Promise<JsonObject> {
    const sessions = await listJson(join(this.root, "data", "authorized-upgrades"), /^upgrade-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.json$/);
    const results = await listJson(join(this.root, "data", "self-upgrade-results"), /^self-upgrade-[0-9a-f]{16}\.json$/);
    const latestSession = sessions.sort((a, b) => String(b.value.updated_at ?? "").localeCompare(String(a.value.updated_at ?? ""))).at(0)?.value ?? null;
    const latestResult = results.sort((a, b) => String(b.value.finished_at ?? "").localeCompare(String(a.value.finished_at ?? ""))).at(0)?.value ?? null;
    return { latest_session: latestSession, latest_result: latestResult, session_count: sessions.length, result_count: results.length };
  }
}
