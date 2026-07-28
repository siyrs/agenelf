import { open, readdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { appendLine, atomicWriteJson, readJson } from "./fs-store.ts";
import { randomId, sha256 } from "./canonical.ts";
import { sanitizeObject } from "./privacy.ts";
import { parseSimpleYaml } from "./simple-yaml.ts";
import { ServerCatalog, type ManagedServer } from "./server-catalog.ts";
import {
  OpenSshTransport,
  quoteRemote,
  sanitizeRemoteText,
  truncateRemoteText,
  type RemoteCommandResult
} from "./open-ssh.ts";
import type { OperationRequest } from "./operation-queue.ts";
import type { JsonObject, JsonValue, Risk } from "./types.ts";

const REQUEST_RE = /^op-[0-9a-f]{16}$/;
const PROJECT_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$/;
const MAX_COMPOSE_BYTES = 512 * 1024;

const SERVER_RISKS = new Map<string, Risk>([
  ["apt_update", "change"],
  ["compose_deploy", "change"],
  ["compose_down", "change"],
  ["service_restart", "change"],
  ["docker_install", "privileged"]
]);
const DOCKER_RISKS = new Map<string, Risk>([["restart_docker_container", "change"]]);

interface DecisionDocument extends JsonObject {
  request_id?: JsonValue;
  decision?: JsonValue;
  fingerprint?: JsonValue;
}

interface CommandEvidence extends JsonObject {
  phase: string;
  command: string;
  exit_code: number;
  stdout: string;
  stderr: string;
}

export interface ChangeTransport {
  run(server: ManagedServer, command: string, timeoutMs: number): Promise<RemoteCommandResult>;
  writeText(server: ManagedServer, remotePath: string, content: string, timeoutMs?: number): Promise<RemoteCommandResult>;
}

function now(): string { return new Date().toISOString(); }
function parameters(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("parameters 必须是 object");
  return value as JsonObject;
}
function unknownParameters(value: JsonObject, allowed: string[], label: string): void {
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key));
  if (unknown.length) throw new Error(`${label} 含未知参数：${unknown.sort().join(", ")}`);
}
function safeName(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!NAME_RE.test(text)) throw new Error(`${label} 非法`);
  return text;
}
function safeProject(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!PROJECT_RE.test(text)) throw new Error("非法 Compose 项目名");
  return text;
}
function boundedInteger(value: unknown, fallback: number, min: number, max: number, label: string): number {
  const parsed = Number(value ?? fallback);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) throw new Error(`${label} 必须在 ${min}-${max}`);
  return parsed;
}
function canonicalPayload(request: OperationRequest): JsonObject {
  return {
    capability: request.capability.trim(),
    operation: request.operation.trim(),
    target: request.target.trim(),
    parameters: request.parameters
  };
}
function expectedRisk(request: Pick<OperationRequest, "capability" | "operation">): Risk | undefined {
  if (request.capability === "server.operations") return SERVER_RISKS.get(request.operation);
  if (request.capability === "docker.operations") return DOCKER_RISKS.get(request.operation);
  return undefined;
}
export function isSemanticChangeRequest(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const request = value as Partial<OperationRequest>;
  return expectedRisk({ capability: String(request.capability ?? ""), operation: String(request.operation ?? "") } as OperationRequest) !== undefined;
}
function pathUnder(value: string, roots: string[]): boolean {
  if (!value.startsWith("/")) return true;
  const normalized = value.replace(/\/+$/, "") || "/";
  return roots.some((root) => normalized === root || normalized.startsWith(`${root}/`));
}
function volumeSourceAndTarget(value: JsonValue): { source: string; target: string } {
  if (typeof value === "string") {
    const parts = value.split(":", 3);
    return parts.length >= 2 ? { source: parts[0], target: parts[1] } : { source: "", target: "" };
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const document = value as JsonObject;
    if (String(document.type ?? "volume") === "bind") return { source: String(document.source ?? ""), target: String(document.target ?? "") };
  }
  return { source: "", target: "" };
}

export function validateComposeYaml(composeYaml: string, server: ManagedServer): JsonObject {
  if (!composeYaml.trim()) throw new Error("compose_yaml 不能为空");
  if (Buffer.byteLength(composeYaml, "utf8") > MAX_COMPOSE_BYTES) throw new Error(`compose_yaml 超过 ${MAX_COMPOSE_BYTES} 字节上限`);
  const document = parseSimpleYaml(composeYaml);
  const services = document.services;
  if (!services || typeof services !== "object" || Array.isArray(services) || !Object.keys(services).length) throw new Error("Compose 必须包含非空 services mapping");
  for (const [serviceName, raw] of Object.entries(services)) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error(`service ${serviceName} 配置必须是 mapping`);
    const service = raw as JsonObject;
    if (service.privileged === true) throw new Error(`service ${serviceName} 禁止 privileged=true`);
    for (const field of ["network_mode", "pid", "ipc", "userns_mode"]) {
      if (String(service[field] ?? "").toLowerCase() === "host") throw new Error(`service ${serviceName} 禁止 ${field}: host`);
    }
    if (Array.isArray(service.cap_add) && service.cap_add.some((item) => String(item).toUpperCase() === "ALL")) throw new Error(`service ${serviceName} 禁止 cap_add: ALL`);
    if (Array.isArray(service.devices) && service.devices.length) throw new Error(`service ${serviceName} 禁止 devices 映射`);
    if (service.volumes !== undefined && !Array.isArray(service.volumes)) throw new Error(`service ${serviceName} volumes 必须是 list`);
    for (const volume of Array.isArray(service.volumes) ? service.volumes : []) {
      const { source, target } = volumeSourceAndTarget(volume);
      if ([source, target].some((item) => ["/var/run/docker.sock", "/run/docker.sock"].includes(item))) throw new Error("安全红线：禁止挂载 Docker Socket");
      if (source === "/") throw new Error("安全红线：禁止挂载宿主机根目录");
      if (source.startsWith("/") && !pathUnder(source, server.allowedBindRoots)) throw new Error(`绝对路径挂载未获允许：${source}`);
    }
  }
  return document;
}

function evidence(phase: string, result: RemoteCommandResult): CommandEvidence {
  return {
    phase,
    command: sanitizeRemoteText(result.command),
    exit_code: result.exit_code,
    stdout: truncateRemoteText(sanitizeRemoteText(result.stdout)),
    stderr: truncateRemoteText(sanitizeRemoteText(result.stderr))
  };
}

export class ChangeOpsRunner {
  readonly root: string;
  readonly catalog: ServerCatalog;
  readonly transport: ChangeTransport;
  readonly requests: string;
  readonly results: string;
  readonly locks: string;
  readonly decisions: string;
  readonly events: string;
  readonly auditPath: string;
  private initialized = false;

  constructor(root: string, options: { catalog?: ServerCatalog; transport?: ChangeTransport } = {}) {
    this.root = root;
    this.catalog = options.catalog ?? new ServerCatalog(root);
    this.transport = options.transport ?? new OpenSshTransport(this.catalog);
    this.requests = join(root, "data", "ops-requests");
    this.results = join(root, "data", "ops-results");
    this.locks = join(root, "data", "ops-locks");
    this.decisions = join(root, "data", "auth-decisions");
    this.events = join(root, "data", "ops-events");
    this.auditPath = join(root, "logs", "change-ops-runner.log");
  }

  async initialize(): Promise<void> {
    await this.catalog.initialize();
    this.initialized = true;
  }

  protected async beforeLock(_request: OperationRequest): Promise<void> { /* testable race boundary */ }

  private async event(requestId: string, type: string, payload: JsonObject = {}): Promise<void> {
    await appendLine(join(this.events, `${requestId}.jsonl`), JSON.stringify({
      schema_version: 1,
      id: randomId("oevt-", 20),
      operation_id: requestId,
      type,
      origin: "runner",
      ts: now(),
      payload: sanitizeObject(payload)
    }));
  }

  private async audit(eventName: string, detail: string): Promise<void> {
    await appendLine(this.auditPath, `[${now()}] [${eventName}] ${sanitizeRemoteText(detail)}`);
  }

  private validate(request: OperationRequest): { server: ManagedServer; payload: JsonObject; risk: Risk; plan: JsonObject } {
    if (request.schema_version !== 1 || !REQUEST_RE.test(String(request.id ?? ""))) throw new Error("不支持的操作请求版本或 ID");
    if (!request.parameters || typeof request.parameters !== "object" || Array.isArray(request.parameters)) throw new Error("parameters 必须是 object");
    const risk = expectedRisk(request);
    if (!risk) throw new Error("请求能力或操作不受支持");
    if (request.risk !== risk) throw new Error(`风险级别不匹配：${request.operation} 必须是 ${risk}`);
    const payload = canonicalPayload(request);
    if (sha256(payload as unknown as JsonValue) !== request.fingerprint) throw new Error("请求指纹校验失败，文件可能被篡改");
    const expiresAt = Date.parse(String(request.expires_at ?? ""));
    if (!Number.isFinite(expiresAt)) throw new Error("请求 expires_at 非法");
    const server = this.catalog.get(request.target);
    const params = parameters(request.parameters);
    let plan: JsonObject = {};
    if (request.capability === "server.operations") {
      if (request.operation === "compose_down") {
        const allowed = server.allowedOperations;
        if (allowed && !allowed.has("compose_down") && !allowed.has("compose_deploy")) throw new Error("目标策略未允许 compose_down");
      } else if (server.allowedOperations && !server.allowedOperations.has(request.operation)) throw new Error(`服务器策略未允许操作：${request.operation}`);
      if (request.operation === "apt_update" || request.operation === "docker_install") {
        unknownParameters(params, [], request.operation);
      } else if (request.operation === "service_restart") {
        unknownParameters(params, ["service"], "service_restart");
        const service = safeName(params.service, "service");
        if (!server.allowedServices.has(service)) throw new Error(`服务不在允许清单：${service}`);
        plan = { service };
      } else if (request.operation === "compose_down") {
        unknownParameters(params, ["project", "timeout_seconds", "remove_orphans"], "compose_down");
        plan = {
          project: safeProject(params.project),
          timeout_seconds: boundedInteger(params.timeout_seconds, 30, 0, 120, "timeout_seconds"),
          remove_orphans: params.remove_orphans === undefined ? true : params.remove_orphans
        };
        if (typeof plan.remove_orphans !== "boolean") throw new Error("remove_orphans 必须是 boolean");
      } else if (request.operation === "compose_deploy") {
        unknownParameters(params, ["project", "compose_yaml", "pull"], "compose_deploy");
        const composeYaml = String(params.compose_yaml ?? "");
        validateComposeYaml(composeYaml, server);
        plan = {
          project: safeProject(params.project),
          compose_yaml: composeYaml,
          pull: params.pull === undefined ? true : params.pull
        };
        if (typeof plan.pull !== "boolean") throw new Error("pull 必须是 boolean");
      }
    } else {
      if (server.allowedDockerOperations && !server.allowedDockerOperations.has(request.operation)) throw new Error(`目标 Docker 策略未允许操作：${request.operation}`);
      unknownParameters(params, ["container", "timeout_seconds"], "restart_docker_container");
      const container = safeName(params.container, "container");
      if (server.allowedContainers && !server.allowedContainers.has(container)) throw new Error(`容器不在允许清单：${container}`);
      plan = { container, timeout_seconds: boundedInteger(params.timeout_seconds, 10, 0, 60, "timeout_seconds") };
    }
    return { server, payload, risk, plan };
  }

  private async decisionState(request: OperationRequest, payload: JsonObject): Promise<"approved" | "pending" | "denied" | "invalid"> {
    const decision = await readJson<DecisionDocument | null>(join(this.decisions, `${request.id}.json`), null);
    if (!decision) return "pending";
    if (String(decision.request_id ?? "") !== request.id) return "invalid";
    if (String(decision.decision ?? "") === "deny") return "denied";
    if (String(decision.decision ?? "") !== "approve") return "invalid";
    if (String(decision.fingerprint ?? "") !== sha256(payload as unknown as JsonValue)) return "invalid";
    return "approved";
  }

  private baseResult(request: OperationRequest, status: string, commands: CommandEvidence[], extra: JsonObject = {}): JsonObject {
    return {
      schema_version: 1,
      id: request.id,
      status,
      target: request.target,
      capability: request.capability,
      operation: request.operation,
      started_at: now(),
      finished_at: now(),
      commands: commands as unknown as JsonValue,
      ...extra
    };
  }

  private async execute(request: OperationRequest, server: ManagedServer, plan: JsonObject): Promise<JsonObject> {
    const commands: CommandEvidence[] = [];
    const run = async (phase: string, command: string, timeoutMs: number) => {
      await this.event(request.id, "ssh.started", { phase, target: request.target, operation: request.operation });
      const outcome = await this.transport.run(server, command, timeoutMs);
      const item = evidence(phase, outcome);
      commands.push(item);
      await this.event(request.id, "ssh.completed", { phase, exit_code: outcome.exit_code });
      return outcome;
    };
    const write = async (phase: string, path: string, content: string) => {
      await this.event(request.id, "ssh.started", { phase, target: request.target, operation: request.operation });
      const outcome = await this.transport.writeText(server, path, content, 120_000);
      commands.push(evidence(phase, outcome));
      await this.event(request.id, "ssh.completed", { phase, exit_code: outcome.exit_code });
      return outcome;
    };

    if (request.operation === "apt_update") {
      await run("apt_update", "sudo -n apt-get update", 600_000);
    } else if (request.operation === "docker_install") {
      await run("docker_install", "sudo -n apt-get update && sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 && sudo -n systemctl enable --now docker && sudo -n docker version", 1_200_000);
    } else if (request.operation === "service_restart") {
      const service = quoteRemote(String(plan.service));
      await run("service_restart", `sudo -n systemctl restart ${service} && systemctl status --no-pager --full ${service}`, 180_000);
    } else if (request.operation === "restart_docker_container") {
      const containerRaw = String(plan.container);
      const container = quoteRemote(containerRaw);
      const timeout = Number(plan.timeout_seconds);
      const restart = await run("container_restart", `${server.dockerCommand} restart --time ${timeout} ${container}`, 180_000);
      if (restart.exit_code === 0) await run("container_status", `${server.dockerCommand} ps -a --filter name=^/${containerRaw}$ --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}'`, 60_000);
    } else if (request.operation === "compose_down") {
      const project = String(plan.project);
      const projectDir = `${server.managedRoot}/${project}`;
      const composePath = `${projectDir}/compose.yaml`;
      const check = await run("compose_exists", `test -d ${quoteRemote(projectDir)} && test -f ${quoteRemote(composePath)}`, 60_000);
      if (check.exit_code === 0) {
        const validate = await run("compose_validate", `cd ${quoteRemote(projectDir)} && ${server.dockerCommand} compose -f ${quoteRemote(composePath)} config --services`, 120_000);
        if (validate.exit_code === 0) {
          const suffix = plan.remove_orphans === true ? " --remove-orphans" : "";
          const timeout = Number(plan.timeout_seconds);
          const down = await run("compose_down", `cd ${quoteRemote(projectDir)} && ${server.dockerCommand} compose -f ${quoteRemote(composePath)} down --timeout ${timeout}${suffix}`, Math.max(180_000, (timeout + 120) * 1_000));
          if (down.exit_code === 0) await run("compose_status", `cd ${quoteRemote(projectDir)} && ${server.dockerCommand} compose -f ${quoteRemote(composePath)} ps -a`, 120_000);
        }
      }
      const ok = commands.length > 0 && commands.every((item) => item.exit_code === 0);
      return this.baseResult(request, ok ? "succeeded" : "failed", commands, { project, preserved: ["named_volumes", "images", "compose.yaml", ".agenelf-backups"] });
    } else if (request.operation === "compose_deploy") {
      const project = String(plan.project);
      const projectDir = `${server.managedRoot}/${project}`;
      const composePath = `${projectDir}/compose.yaml`;
      const backupDir = `${projectDir}/.agenelf-backups`;
      const stamp = now().replace(/[-:TZ.]/g, "").slice(0, 14);
      const backupPath = `${backupDir}/${stamp}-${request.id}.yaml`;
      const tempPath = `${projectDir}/.compose.${request.id}.tmp.yaml`;
      const prepare = await run("compose_prepare", `mkdir -p ${quoteRemote(projectDir)} ${quoteRemote(backupDir)}`, 60_000);
      if (prepare.exit_code === 0) {
        const written = await write("compose_write", tempPath, String(plan.compose_yaml));
        if (written.exit_code === 0) {
          const validate = await run("compose_validate", `${server.dockerCommand} compose -f ${quoteRemote(tempPath)} config`, 120_000);
          if (validate.exit_code !== 0) await run("compose_temp_cleanup", `rm -f ${quoteRemote(tempPath)}`, 30_000);
          else {
            const promote = await run("compose_backup_promote", `if [ -f ${quoteRemote(composePath)} ]; then cp ${quoteRemote(composePath)} ${quoteRemote(backupPath)}; fi && mv ${quoteRemote(tempPath)} ${quoteRemote(composePath)}`, 60_000);
            if (promote.exit_code === 0) {
              await this.event(request.id, "compose.backup.created", { project, backup_path: backupPath });
              const rollback = async () => {
                await this.event(request.id, "compose.rollback.started", { project });
                const outcome = await run("rollback", `if [ -f ${quoteRemote(backupPath)} ]; then cp ${quoteRemote(backupPath)} ${quoteRemote(composePath)} && cd ${quoteRemote(projectDir)} && ${server.dockerCommand} compose up -d --remove-orphans; else exit 3; fi`, 1_200_000);
                await this.event(request.id, "compose.rollback.completed", { project, exit_code: outcome.exit_code });
              };
              if (plan.pull === true) {
                const pull = await run("compose_pull", `cd ${quoteRemote(projectDir)} && ${server.dockerCommand} compose pull`, 1_200_000);
                if (pull.exit_code !== 0) await rollback();
                else {
                  const deploy = await run("compose_deploy", `cd ${quoteRemote(projectDir)} && ${server.dockerCommand} compose up -d --remove-orphans`, 1_200_000);
                  if (deploy.exit_code !== 0) await rollback();
                  else await run("compose_status", `cd ${quoteRemote(projectDir)} && ${server.dockerCommand} compose ps`, 120_000);
                }
              } else {
                const deploy = await run("compose_deploy", `cd ${quoteRemote(projectDir)} && ${server.dockerCommand} compose up -d --remove-orphans`, 1_200_000);
                if (deploy.exit_code !== 0) await rollback();
                else await run("compose_status", `cd ${quoteRemote(projectDir)} && ${server.dockerCommand} compose ps`, 120_000);
              }
            }
          }
        }
      }
      const primaryFailure = commands.some((item) => item.phase !== "rollback" && item.exit_code !== 0);
      return this.baseResult(request, !primaryFailure && commands.length > 0 ? "succeeded" : "failed", commands, { project, backup_path: backupPath, artifact: "remote-compose" });
    }
    const ok = commands.length > 0 && commands.every((item) => item.exit_code === 0);
    return this.baseResult(request, ok ? "succeeded" : "failed", commands);
  }

  private async persistTerminal(request: OperationRequest, result: JsonObject, eventType = "ops.result.persisted"): Promise<string> {
    const path = join(this.results, `${request.id}.json`);
    try { await atomicWriteJson(path, result, true); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return "done";
      throw error;
    }
    await this.event(request.id, eventType, { status: result.status as JsonValue });
    return String(result.status ?? "failed");
  }

  async processRequest(path: string): Promise<string> {
    const initial = await readJson<OperationRequest | null>(path, null);
    if (!initial || !isSemanticChangeRequest(initial)) return "skipped";
    const requestId = String(initial.id ?? "");
    if (!REQUEST_RE.test(requestId)) return "invalid";
    const resultPath = join(this.results, `${requestId}.json`);
    if (await readJson<JsonObject | null>(resultPath, null)) return "done";
    let preliminary;
    try { preliminary = this.validate(initial); }
    catch (error) {
      const failed = this.baseResult(initial, "failed", [], { reason: sanitizeRemoteText(error instanceof Error ? `${error.name}: ${error.message}` : String(error)) });
      return this.persistTerminal(initial, failed, "ops.failed");
    }
    if (Date.parse(initial.expires_at) <= Date.now()) {
      const expired = this.baseResult(initial, "expired", [], { reason: "操作请求已过期，未连接服务器" });
      return this.persistTerminal(initial, expired);
    }
    const preState = await this.decisionState(initial, preliminary.payload);
    if (preState === "pending") return "pending";
    await this.beforeLock(initial);

    const lockPath = join(this.locks, `${requestId}.lock`);
    let lock;
    try { lock = await open(lockPath, "wx", 0o600); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return "locked";
      throw error;
    }
    try {
      if (await readJson<JsonObject | null>(resultPath, null)) return "done";
      const request = await readJson<OperationRequest | null>(path, null);
      if (!request || request.id !== requestId || !isSemanticChangeRequest(request)) throw new Error("锁后请求不存在或语义已变化");
      const validated = this.validate(request);
      await this.event(requestId, "ops.runner.claimed", { capability: request.capability, operation: request.operation, target: request.target, risk: validated.risk });
      if (Date.parse(request.expires_at) <= Date.now()) {
        const expired = this.baseResult(request, "expired", [], { reason: "操作请求已过期，未连接服务器" });
        return this.persistTerminal(request, expired);
      }
      const state = await this.decisionState(request, validated.payload);
      await this.event(requestId, "ops.approval.checked", { state, fingerprint: request.fingerprint });
      if (state !== "approved") {
        const blocked = this.baseResult(request, "blocked", [], { reason: state === "denied" ? "主人拒绝" : "授权缺失、无效或已被撤销" });
        return this.persistTerminal(request, blocked);
      }
      const result = await this.execute(request, validated.server, validated.plan);
      const status = await this.persistTerminal(request, result);
      await this.audit(status, `${request.id} ${request.target} ${request.operation}`);
      return status;
    } catch (error) {
      const request = await readJson<OperationRequest | null>(path, initial);
      const reason = sanitizeRemoteText(error instanceof Error ? `${error.name}: ${error.message}` : String(error));
      const failed = this.baseResult(request, "failed", [], { reason });
      const status = await this.persistTerminal(request, failed, "ops.failed");
      await this.audit("failed", `${requestId} ${reason}`);
      return status;
    } finally {
      await lock.close();
      await rm(lockPath, { force: true });
    }
  }

  async processOnce(): Promise<Record<string, number>> {
    if (!this.initialized) await this.initialize();
    try { await this.catalog.initialize(); } catch (error) { await this.audit("profiles_reload_failed", error instanceof Error ? error.message : String(error)); }
    let names: string[] = [];
    try { names = await readdir(this.requests); } catch { return {}; }
    const counts: Record<string, number> = {};
    for (const name of names.filter((item) => /^op-[0-9a-f]{16}\.json$/.test(item)).sort()) {
      const state = await this.processRequest(join(this.requests, name));
      counts[state] = (counts[state] ?? 0) + 1;
    }
    return counts;
  }
}
