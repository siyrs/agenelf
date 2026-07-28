import { readdir } from "node:fs/promises";
import type { IncomingMessage, ServerResponse } from "node:http";
import { join } from "node:path";
import { AgenelfAgent } from "../../../packages/core/src/agent.ts";
import { AutonomyCycleStore } from "../../../packages/core/src/autonomy-cycles.ts";
import { readJson } from "../../../packages/core/src/fs-store.ts";
import { NativeCompatibilityService } from "../../../packages/core/src/native-compatibility.ts";
import type { JsonObject } from "../../../packages/core/src/types.ts";

export type SendJson = (response: ServerResponse, status: number, value: unknown) => void;
export type ReadJsonBody = (request: IncomingMessage) => Promise<JsonObject>;

function numberParam(url: URL, name: string, fallback: number, min: number, max: number): number {
  const value = Number(url.searchParams.get(name) ?? fallback);
  return Number.isFinite(value) ? Math.max(min, Math.min(Math.trunc(value), max)) : fallback;
}
function statusFor(error: unknown): number {
  const message = error instanceof Error ? error.message : String(error);
  return /不存在|not_found|not found/i.test(message) ? 404 : 400;
}

export class NativeCompatibilityRoutes {
  readonly root: string;
  readonly service: NativeCompatibilityService;
  readonly agent: AgenelfAgent;
  readonly autonomy: AutonomyCycleStore;
  constructor(root: string, agent: AgenelfAgent) {
    this.root = root;
    this.service = new NativeCompatibilityService(root);
    this.agent = agent;
    this.autonomy = new AutonomyCycleStore(root, this.service.tasks);
  }
  async initialize(): Promise<void> { await this.service.initialize(); }

  private async runnerHealth(): Promise<JsonObject> {
    const result: JsonObject = {};
    try {
      for (const name of (await readdir(join(this.root, "data", "runner-health"))).filter((item) => item.endsWith(".json")).sort()) {
        const value = await readJson<JsonObject | null>(join(this.root, "data", "runner-health", name), null);
        if (value) result[name.replace(/\.json$/, "")] = { status: value.status ?? "unknown", runtime: value.runtime ?? "", updated_at: value.updated_at ?? "", role: value.role ?? "" };
      }
    } catch { /* missing heartbeat directory is represented by an empty object */ }
    return result;
  }

  private async autonomySnapshot(): Promise<JsonObject> {
    const runtime = await this.agent.status();
    return {
      schema_version: 1,
      observed_at: new Date().toISOString(),
      identity: { name: "Agenelf", kind: "tool-using software agent", runtime: "node-typescript", consciousness_claim: false },
      skills: this.agent.registry.catalog().map((item) => item.id),
      skill_count: this.agent.registry.catalog().length,
      capabilities: this.agent.registry.catalog(),
      capability_count: this.agent.registry.catalog().length,
      registry_errors: {},
      resources: this.agent.resources.catalog(),
      prompts: this.agent.prompts.catalog(),
      validation: runtime.validation ?? {},
      compatibility: runtime.compatibility ?? {},
      runner_health: await this.runnerHealth(),
      event_runs: this.agent.events.list().length,
      evidence_sources: ["Session Ledger", "data/runner-health", "data/*-results", "Resource catalog", "Prompt catalog"]
    };
  }

  async handle(request: IncomingMessage, response: ServerResponse, url: URL, sendJson: SendJson, readJsonBody: ReadJsonBody): Promise<boolean> {
    try {
      if (request.method === "GET" && url.pathname === "/local/status") {
        sendJson(response, 200, await this.service.local.status()); return true;
      }
      if (request.method === "POST" && url.pathname === "/local/reload") {
        sendJson(response, 200, await this.service.local.reload()); return true;
      }
      if (request.method === "POST" && url.pathname === "/memory") {
        const body = await readJsonBody(request);
        sendJson(response, 200, await this.service.remember(String(body.kind ?? ""), String(body.content ?? ""))); return true;
      }
      if (request.method === "GET" && url.pathname === "/memory/search") {
        sendJson(response, 200, await this.service.searchMemory(String(url.searchParams.get("q") ?? ""), numberParam(url, "limit", 5, 1, 20))); return true;
      }
      if (request.method === "GET" && url.pathname === "/approvals") {
        sendJson(response, 200, await this.service.approvals()); return true;
      }
      if (request.method === "GET" && url.pathname === "/tasks") {
        sendJson(response, 200, await this.service.listTasks(String(url.searchParams.get("status") ?? ""))); return true;
      }
      const taskMatch = url.pathname.match(/^\/tasks\/(ntask-[0-9a-f]{16}|task-[A-Za-z0-9][A-Za-z0-9._-]{0,120})$/);
      if (request.method === "GET" && taskMatch) {
        sendJson(response, 200, await this.service.task(taskMatch[1])); return true;
      }
      const operationMatch = url.pathname.match(/^\/operations\/(op-[0-9a-f]{16})$/);
      if (request.method === "GET" && operationMatch) {
        sendJson(response, 200, await this.service.operations.get(operationMatch[1])); return true;
      }
      if (request.method === "GET" && url.pathname === "/code-repair/catalog") {
        sendJson(response, 200, await this.service.repairCatalogView()); return true;
      }
      if (request.method === "POST" && url.pathname === "/code-repair/requests") {
        const body = await readJsonBody(request);
        const state = await this.service.submitRepair({
          repository: String(body.repository ?? ""),
          unifiedDiff: String(body.unified_diff ?? ""),
          testProfile: String(body.test_profile ?? ""),
          expectedBase: String(body.expected_base ?? ""),
          summary: String(body.summary ?? "")
        });
        sendJson(response, 202, state); return true;
      }
      const repairMatch = url.pathname.match(/^\/code-repair\/requests\/(repair-[0-9a-f]{16})$/);
      if (request.method === "GET" && repairMatch) {
        sendJson(response, 200, await this.service.repair(repairMatch[1], numberParam(url, "wait_seconds", 0, 0, 15))); return true;
      }
      if (request.method === "GET" && url.pathname === "/evolution/status") {
        sendJson(response, 200, await this.service.evolutionStatus()); return true;
      }
      if (request.method === "GET" && url.pathname === "/self-upgrade/status") {
        sendJson(response, 200, await this.service.selfUpgradeStatus()); return true;
      }

      if (request.method === "GET" && url.pathname === "/self/development") {
        sendJson(response, 200, await this.service.self.status()); return true;
      }
      if (request.method === "GET" && url.pathname === "/self/reflections") {
        sendJson(response, 200, { reflections: await this.service.self.reflections(numberParam(url, "limit", 10, 1, 50)) }); return true;
      }
      if (request.method === "POST" && url.pathname === "/self/reflections") {
        const body = await readJsonBody(request);
        const runtime = await this.agent.status();
        sendJson(response, 200, await this.service.self.reflect(String(body.note ?? ""), Boolean(body.deep), { runtime: runtime.runtime ?? "", validation: runtime.validation ?? {} })); return true;
      }
      if (request.method === "GET" && url.pathname === "/self/intentions") {
        sendJson(response, 200, { intentions: await this.service.self.intentions(String(url.searchParams.get("status") ?? ""), numberParam(url, "limit", 20, 1, 100)) }); return true;
      }
      if (request.method === "POST" && url.pathname === "/self/intentions") {
        const body = await readJsonBody(request);
        sendJson(response, 200, await this.service.self.createIntention({
          title: String(body.title ?? ""),
          rationale: String(body.rationale ?? ""),
          priority: String(body.priority ?? "P2"),
          acceptanceCriteria: Array.isArray(body.acceptance_criteria) ? body.acceptance_criteria.map(String) : [],
          source: "node-api"
        })); return true;
      }
      const intentionMatch = url.pathname.match(/^\/self\/intentions\/(intent-[A-Za-z0-9._-]+)$/);
      if (request.method === "GET" && intentionMatch) {
        sendJson(response, 200, await this.service.self.intention(intentionMatch[1])); return true;
      }
      const pursueMatch = url.pathname.match(/^\/self\/intentions\/(intent-[A-Za-z0-9._-]+)\/pursue$/);
      if (request.method === "POST" && pursueMatch) {
        const body = await readJsonBody(request);
        sendJson(response, 200, await this.service.self.pursue(pursueMatch[1], this.service.tasks, Boolean(body.apply_changes))); return true;
      }

      if (request.method === "GET" && url.pathname === "/self/optimization") {
        sendJson(response, 200, await this.agent.optimization.status()); return true;
      }
      if (request.method === "POST" && url.pathname === "/self/optimization/apply") {
        const body = await readJsonBody(request);
        sendJson(response, 200, await this.agent.optimization.apply(String(body.key ?? ""), body.value, String(body.reason ?? ""), body.evidence)); return true;
      }
      if (request.method === "POST" && url.pathname === "/self/optimization/rollback") {
        const body = await readJsonBody(request);
        sendJson(response, 200, await this.agent.optimization.rollback(String(body.key ?? ""))); return true;
      }
      if (request.method === "POST" && url.pathname === "/self/optimization/auto") {
        sendJson(response, 200, await this.agent.optimization.autoTune()); return true;
      }

      if (request.method === "POST" && url.pathname === "/autonomy/cycles") {
        const body = await readJsonBody(request);
        const goal = String(body.goal ?? "").trim();
        const applyChanges = Boolean(body.apply_changes);
        const intentionResult = applyChanges ? await this.service.self.createIntention({
          title: goal ? `Autonomy：${goal}` : "Autonomy evidence-driven improvement",
          rationale: "Pi 风格 autonomy cycle 识别的改进目标；代码变化必须走主人授权 Self-upgrade",
          priority: "P1",
          acceptanceCriteria: ["Node Task 完成", "独立验证通过", "主人批准精确 Self-upgrade 候选", "保留 result/event/backup 证据"],
          source: "node-autonomy"
        }) : null;
        const intention = intentionResult && intentionResult.intention && typeof intentionResult.intention === "object" && !Array.isArray(intentionResult.intention) ? intentionResult.intention as JsonObject : null;
        sendJson(response, 200, await this.autonomy.create({ goal, applyChanges, snapshot: await this.autonomySnapshot(), intention })); return true;
      }
      if (request.method === "GET" && url.pathname === "/autonomy/cycles") {
        sendJson(response, 200, { cycles: await this.autonomy.list(numberParam(url, "limit", 20, 1, 100)) }); return true;
      }
      const cycleMatch = url.pathname.match(/^\/autonomy\/cycles\/(auto-[0-9]{8}-[0-9]{6}-[0-9a-f]{6})$/);
      if (request.method === "GET" && cycleMatch) {
        sendJson(response, 200, await this.autonomy.get(cycleMatch[1])); return true;
      }

      if (request.method === "GET" && url.pathname === "/self") {
        sendJson(response, 200, { runtime: await this.agent.status(), development: await this.service.self.status(), self_upgrade: await this.service.selfUpgradeStatus() }); return true;
      }
      if (request.method === "GET" && url.pathname === "/self/assessment") {
        const runtime = await this.agent.status();
        const development = await this.service.self.status();
        const findings: JsonObject[] = [];
        const validation = runtime.validation && typeof runtime.validation === "object" && !Array.isArray(runtime.validation) ? runtime.validation as JsonObject : {};
        if (validation.ready !== true) findings.push({ priority: "P0", code: "validation_unavailable", finding: "Node Validation 不可用", recommendation: "恢复主人配置并取得独立 Runner 证据" });
        if (runtime.compatibility && typeof runtime.compatibility === "object" && !Array.isArray(runtime.compatibility) && (runtime.compatibility as JsonObject).legacy_api === true) findings.push({ priority: "P1", code: "legacy_api_remaining", finding: "默认 Compose 尚未移除 internal legacy API 服务", recommendation: "删除 legacy-agent 与 Python API 生产依赖" });
        if (!findings.length) findings.push({ priority: "P2", code: "continuous_improvement", finding: "当前未发现阻断性缺陷", recommendation: "选择一个小而可验证的缺口并补充证据" });
        sendJson(response, 200, { observed_at: new Date().toISOString(), health: findings[0].priority === "P0" ? "degraded" : "ready", findings, recommended_goal: findings[0].recommendation, development }); return true;
      }
      if (request.method === "GET" && url.pathname === "/self/capability-health") {
        const runtime = await this.agent.status();
        sendJson(response, 200, { schema_version: 1, observed_at: new Date().toISOString(), runtime, runner_health: await this.runnerHealth(), evidence_sources: ["data/runner-health", "data/*-results", "Session Ledger"], model_self_rating_used: false }); return true;
      }
      if (request.method === "GET" && url.pathname === "/self/roadmap") {
        const limit = numberParam(url, "limit", 10, 1, 50);
        const intentions = await this.service.self.intentions("", limit);
        sendJson(response, 200, { roadmap: intentions.map((item) => ({ id: item.id, title: item.title, priority: item.priority, status: item.status, acceptance_criteria: item.acceptance_criteria ?? [] })), count: intentions.length }); return true;
      }
      return false;
    } catch (error) {
      sendJson(response, statusFor(error), { error: error instanceof Error ? error.message : String(error), runtime: "node-typescript" });
      return true;
    }
  }
}
