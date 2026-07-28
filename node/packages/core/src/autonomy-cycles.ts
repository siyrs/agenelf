import { readdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import { appendLine, atomicWriteJson, readJson } from "./fs-store.ts";
import { randomId } from "./canonical.ts";
import { redactSensitiveText, sanitizeObject } from "./privacy.ts";
import { TaskStore } from "./task-store.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const CYCLE_ID = /^auto-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$/;
const SAFETY_INVARIANTS = [
  "自我模型只描述可观测运行状态，不声称主观意识、情感或独立人格",
  "Autonomy API 不直接修改源码、Git、Runner、策略或宿主机",
  "apply_changes 只能创建 Node Task 和 owner-authorized Self-upgrade 下一步",
  "代码候选必须经过双阶段主人授权、完整测试、红线、备份和回滚",
  "能力健康来自可信 Runner/result/ledger 证据，不使用模型自评代替"
];

function now(): string { return new Date().toISOString(); }
function safeText(value: unknown, limit = 4000): string { return redactSensitiveText(value).trim().slice(0, limit); }
function cycleId(): string {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
  return `auto-${stamp}-${randomId("", 6)}`;
}
function priorityRank(value: unknown): number { return ({ P0: 0, P1: 1, P2: 2, P3: 3 } as Record<string, number>)[String(value)] ?? 9; }

export class AutonomyCycleStore {
  readonly root: string;
  readonly cycles: string;
  readonly events: string;
  readonly tasks: TaskStore;

  constructor(root: string, tasks = new TaskStore(root)) {
    this.root = resolve(root);
    this.cycles = join(this.root, "data", "autonomy-cycles");
    this.events = join(this.root, "data", "autonomy-events");
    this.tasks = tasks;
  }

  private path(id: string): string {
    if (!CYCLE_ID.test(id)) throw new Error(`非法 autonomy cycle ID：${id}`);
    return join(this.cycles, `${id}.json`);
  }
  private async event(id: string, type: string, payload: JsonObject = {}): Promise<void> {
    await appendLine(join(this.events, `${id}.jsonl`), JSON.stringify({ schema_version: 1, id: randomId("aevt-", 20), cycle_id: id, type, origin: "node-autonomy", ts: now(), payload: sanitizeObject(payload) }));
  }
  private async save(cycle: JsonObject): Promise<void> {
    cycle.updated_at = now();
    await atomicWriteJson(this.path(String(cycle.id)), cycle);
  }

  assess(snapshot: JsonObject): JsonObject {
    const findings: JsonObject[] = [];
    const registryErrors = snapshot.registry_errors && typeof snapshot.registry_errors === "object" && !Array.isArray(snapshot.registry_errors) ? snapshot.registry_errors as JsonObject : {};
    if (Object.keys(registryErrors).length) findings.push({ priority: "P0", code: "registry_errors", finding: "存在 Node 技能或 Resource 加载错误", recommendation: "修复加载错误并补充合同测试" });
    const validation = snapshot.validation && typeof snapshot.validation === "object" && !Array.isArray(snapshot.validation) ? snapshot.validation as JsonObject : {};
    if (validation.ready !== true) findings.push({ priority: "P0", code: "validation_unavailable", finding: "Node Validation 未就绪", recommendation: "恢复主人配置并取得独立 Runner 证据" });
    const compatibility = snapshot.compatibility && typeof snapshot.compatibility === "object" && !Array.isArray(snapshot.compatibility) ? snapshot.compatibility as JsonObject : {};
    if (compatibility.legacy_api === true) findings.push({ priority: "P1", code: "legacy_runtime_remaining", finding: "默认控制面仍依赖 internal legacy API", recommendation: "完成 Node 原生 API cutover 并移除 legacy-agent" });
    const runnerHealth = snapshot.runner_health && typeof snapshot.runner_health === "object" && !Array.isArray(snapshot.runner_health) ? snapshot.runner_health as JsonObject : {};
    const unhealthy = Object.entries(runnerHealth).filter(([, value]) => value && typeof value === "object" && !Array.isArray(value) && !["ok", "healthy"].includes(String((value as JsonObject).status ?? "")));
    if (unhealthy.length) findings.push({ priority: "P1", code: "runner_health", finding: `存在 ${unhealthy.length} 个非健康 Runner heartbeat`, recommendation: "分析 Runner 日志和可信结果后修复" });
    if (!findings.length) findings.push({ priority: "P2", code: "continuous_improvement", finding: "当前没有阻断性证据", recommendation: "选择一个小而可验证的能力缺口，建立 Node Task 并补充证据" });
    findings.sort((a, b) => priorityRank(a.priority) - priorityRank(b.priority) || String(a.code).localeCompare(String(b.code)));
    return { observed_at: snapshot.observed_at ?? now(), health: findings[0].priority === "P0" ? "degraded" : "ready", findings, recommended_goal: findings[0].recommendation };
  }

  async create(input: {
    goal?: string;
    applyChanges?: boolean;
    snapshot: JsonObject;
    intention?: JsonObject | null;
  }): Promise<JsonObject> {
    const id = cycleId();
    const snapshot = sanitizeObject({ ...input.snapshot, safety_invariants: SAFETY_INVARIANTS, consciousness_claim: false });
    const assessment = this.assess(snapshot);
    const goal = safeText(input.goal) || String(assessment.recommended_goal ?? "持续改进 Node Runtime");
    const cycle: JsonObject = {
      schema_version: 1,
      id,
      started_at: now(),
      updated_at: now(),
      status: "planned",
      goal,
      apply_changes: Boolean(input.applyChanges),
      snapshot,
      assessment,
      plan: {
        architecture: "Pi observe → assess → plan → task → owner-authorized Self-upgrade",
        steps: [
          "从 Node Runtime、Session Ledger、Resources 和 Runner evidence 建立快照",
          "确定最高优先级、可验证且不扩大权限的缺口",
          "创建带 acceptance criteria 的 Node Task",
          "需要代码变化时生成 owner-authorized Self-upgrade 意图",
          "候选经完整 Node/Python 测试、红线、批准、备份和回滚后才可应用"
        ],
        acceptance_criteria: [
          "不读取凭据、不访问 Docker Socket、不执行任意 Shell",
          "不直接修改源码、Git main、策略或授权决定",
          "完整测试和独立 Runner 证据通过",
          "最终状态可由 result、ledger、event 和 backup 复核"
        ]
      },
      linked_intention: input.intention ?? null,
      linked_task_id: null,
      next_action: "审阅计划"
    };
    await this.save(cycle);
    await this.event(id, "autonomy.snapshot.created", { goal, apply_changes: Boolean(input.applyChanges) });
    await this.event(id, "autonomy.assessment.completed", { health: assessment.health, recommended_goal: assessment.recommended_goal });
    await this.event(id, "autonomy.plan.created", { steps: 5 });

    if (!input.applyChanges) {
      cycle.status = "plan_ready";
      cycle.next_action = "审阅计划；需要代码变化时重新提交 apply_changes=true";
      await this.save(cycle);
      await this.event(id, "autonomy.plan.ready", { modified_source: false });
      return cycle;
    }

    const criteria = ((cycle.plan as JsonObject).acceptance_criteria as JsonValue[]).map(String);
    const task = await this.tasks.create({ title: `Autonomy: ${goal}`.slice(0, 300), goal, acceptanceCriteria: criteria });
    cycle.linked_task_id = task.id;
    cycle.status = "awaiting_owner_authorized_upgrade";
    cycle.next_action = "在主人终端发起并批准精确 Self-upgrade 候选；Autonomy 本身不会修改源码";
    await this.save(cycle);
    await this.event(id, "autonomy.task.created", { task_id: task.id });
    await this.event(id, "autonomy.owner_authorization.required", { execution_mode: "owner_authorized_self_upgrade" });
    return cycle;
  }

  async list(limit = 20): Promise<JsonObject[]> {
    try {
      const names = (await readdir(this.cycles)).filter((name) => /^auto-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}\.json$/.test(name));
      const cycles = await Promise.all(names.map((name) => readJson<JsonObject | null>(join(this.cycles, name), null)));
      return cycles.filter((item): item is JsonObject => Boolean(item)).map(sanitizeObject).sort((a, b) => String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? ""))).slice(0, Math.max(0, Math.min(limit, 100)));
    } catch { return []; }
  }

  async get(id: string): Promise<JsonObject> {
    const cycle = await readJson<JsonObject | null>(this.path(id), null);
    if (!cycle) throw new Error(`autonomy cycle 不存在：${id}`);
    return sanitizeObject(cycle);
  }
}
