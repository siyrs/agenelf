import { lstat, open, readFile, readdir, rm, unlink } from "node:fs/promises";
import { join, resolve } from "node:path";
import { appendLine, atomicWriteJson, readJson } from "./fs-store.ts";
import { randomId, sha256 } from "./canonical.ts";
import { sanitizeObject } from "./privacy.ts";
import {
  INVENTORY_SCRIPT,
  PATCH_SCRIPT,
  SECRET_STAGE_RE,
  parseSecretInventory,
  rawSha256,
  validateSecretStage,
  type SecretStage
} from "./secret-env.ts";
import { SecretTargetCatalog, type ManagedSecretTarget } from "./secret-targets.ts";
import { ServerCatalog, type ManagedServer } from "./server-catalog.ts";
import {
  OpenSshTransport,
  quoteRemote,
  sanitizeRemoteText,
  type RemoteCommandResult
} from "./open-ssh.ts";
import type { OperationRequest } from "./operation-queue.ts";
import type { JsonObject, JsonValue, Risk } from "./types.ts";

const REQUEST_RE = /^op-[0-9a-f]{16}$/;
const ALIAS_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
const MAX_STAGE_BYTES = 256 * 1024;

interface DecisionDocument extends JsonObject {
  request_id?: JsonValue;
  decision?: JsonValue;
  fingerprint?: JsonValue;
}

interface SecretEvidence extends JsonObject {
  phase: string;
  command: string;
  exit_code: number;
  stdout: string;
  stderr: string;
}

interface ValidatedSecretRequest {
  server: ManagedServer;
  target: ManagedSecretTarget;
  payload: JsonObject;
  risk: Risk;
  plan: JsonObject;
}

export interface SecretTransport {
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

function canonicalPayload(request: OperationRequest): JsonObject {
  return {
    capability: request.capability.trim(),
    operation: request.operation.trim(),
    target: request.target.trim(),
    parameters: request.parameters
  };
}

function expectedRisk(request: Pick<OperationRequest, "capability" | "operation">): Risk | undefined {
  if (request.capability !== "server.secrets") return undefined;
  if (request.operation === "inventory_env") return "read";
  if (request.operation === "patch_env") return "change";
  return undefined;
}

export function isSecretOperationRequest(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const request = value as Partial<OperationRequest>;
  return expectedRisk({
    capability: String(request.capability ?? ""),
    operation: String(request.operation ?? "")
  } as OperationRequest) !== undefined;
}

function safeAlias(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!ALIAS_RE.test(text)) throw new Error(`${label} 非法`);
  return text;
}

function secureEvidence(phase: string, result: RemoteCommandResult): SecretEvidence {
  return {
    phase,
    command: sanitizeRemoteText(result.command),
    exit_code: result.exit_code,
    stdout: result.exit_code === 0 ? "[secure structured output omitted]" : "",
    stderr: result.exit_code === 0 ? "" : "[secure error output omitted]"
  };
}

function seatPayload(target: ManagedSecretTarget): string {
  return JSON.stringify([...target.seats.values()].map((seat) => ({
    seat_id: seat.id,
    env_name: seat.envName
  })));
}

function parsePatchSummary(text: string): JsonObject {
  let parsed: JsonValue;
  try { parsed = JSON.parse(text) as JsonValue; }
  catch { throw new Error("远程密钥修改结果不是有效 JSON"); }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("远程密钥修改结果必须是 object");
  const document = parsed as JsonObject;
  if (Number(document.schema_version) !== 1) throw new Error("远程密钥修改结果版本非法");
  const inventoryHash = String(document.inventory_hash_after ?? "");
  if (!SHA256_RE.test(inventoryHash)) throw new Error("远程密钥修改结果 inventory_hash_after 非法");
  if (!Array.isArray(document.changes)) throw new Error("远程密钥修改结果 changes 非法");
  return {
    schema_version: 1,
    inventory_hash_after: inventoryHash,
    changes: document.changes.map((raw) => {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("远程 changes 项非法");
      const item = raw as JsonObject;
      const action = String(item.action ?? "");
      if (!["keep", "delete", "set"].includes(action)) throw new Error("远程 changes action 非法");
      return {
        seat_id: safeAlias(item.seat_id, "远程 seat_id"),
        action,
        old_fingerprint: String(item.old_fingerprint ?? "").slice(0, 16),
        new_fingerprint: String(item.new_fingerprint ?? "").slice(0, 16),
        present: item.present === true
      };
    })
  };
}

export class SecretOpsRunner {
  readonly root: string;
  readonly servers: ServerCatalog;
  readonly targets: SecretTargetCatalog;
  readonly transport: SecretTransport;
  readonly requests: string;
  readonly results: string;
  readonly locks: string;
  readonly decisions: string;
  readonly events: string;
  readonly staging: string;
  readonly auditPath: string;
  private initialized = false;

  constructor(root: string, options: {
    servers?: ServerCatalog;
    targets?: SecretTargetCatalog;
    transport?: SecretTransport;
    stagingDir?: string;
  } = {}) {
    this.root = resolve(root);
    this.servers = options.servers ?? new ServerCatalog(this.root);
    this.targets = options.targets ?? new SecretTargetCatalog(this.root, this.servers);
    this.transport = options.transport ?? new OpenSshTransport(this.servers);
    this.requests = join(this.root, "data", "ops-requests");
    this.results = join(this.root, "data", "ops-results");
    this.locks = join(this.root, "data", "ops-locks");
    this.decisions = join(this.root, "data", "auth-decisions");
    this.events = join(this.root, "data", "ops-events");
    this.staging = resolve(options.stagingDir ?? process.env.AGENELF_SECRET_STAGING_DIR ?? join(this.root, "local", "secret-staging"));
    this.auditPath = join(this.root, "logs", "secret-ops-runner.log");
  }

  async initialize(): Promise<void> {
    await this.targets.initialize();
    this.initialized = true;
  }

  private async event(requestId: string, type: string, payload: JsonObject = {}): Promise<void> {
    await appendLine(join(this.events, `${requestId}.jsonl`), JSON.stringify({
      schema_version: 1,
      id: randomId("oevt-", 20),
      operation_id: requestId,
      type,
      origin: "secret-runner",
      ts: now(),
      payload: sanitizeObject(payload)
    }));
  }

  private async audit(eventName: string, detail: string): Promise<void> {
    await appendLine(this.auditPath, `[${now()}] [${eventName}] ${sanitizeRemoteText(detail)}`);
  }

  private validate(request: OperationRequest): ValidatedSecretRequest {
    if (request.schema_version !== 1 || !REQUEST_RE.test(String(request.id ?? ""))) throw new Error("不支持的操作请求版本或 ID");
    if (!request.parameters || typeof request.parameters !== "object" || Array.isArray(request.parameters)) throw new Error("parameters 必须是 object");
    const risk = expectedRisk(request);
    if (!risk) throw new Error("请求不是受支持的 Secret Ops 操作");
    if (request.risk !== risk) throw new Error(`风险级别不匹配：${request.operation} 必须是 ${risk}`);
    const payload = canonicalPayload(request);
    if (sha256(payload as unknown as JsonValue) !== request.fingerprint) throw new Error("请求指纹校验失败，文件可能被篡改");
    const expiresAt = Date.parse(String(request.expires_at ?? ""));
    if (!Number.isFinite(expiresAt)) throw new Error("请求 expires_at 非法");
    const params = parameters(request.parameters);
    const envTarget = safeAlias(params.env_target, "env_target");
    const target = this.targets.get(envTarget);
    if (request.target !== target.serverAlias) throw new Error("请求 target 与密钥目标 server 不一致");
    const server = this.servers.get(request.target);
    if (request.operation === "inventory_env") {
      unknownParameters(params, ["env_target"], "inventory_env");
      return { server, target, payload, risk, plan: { env_target: envTarget } };
    }
    unknownParameters(params, ["env_target", "stage_ref", "stage_sha256", "expected_inventory_hash"], "patch_env");
    const stageRef = String(params.stage_ref ?? "").trim();
    const stageSha256 = String(params.stage_sha256 ?? "").trim();
    const expectedInventoryHash = String(params.expected_inventory_hash ?? "").trim();
    if (!SECRET_STAGE_RE.test(stageRef)) throw new Error("stage_ref 非法");
    if (!SHA256_RE.test(stageSha256)) throw new Error("stage_sha256 非法");
    if (!SHA256_RE.test(expectedInventoryHash)) throw new Error("expected_inventory_hash 非法");
    return {
      server,
      target,
      payload,
      risk,
      plan: {
        env_target: envTarget,
        stage_ref: stageRef,
        stage_sha256: stageSha256,
        expected_inventory_hash: expectedInventoryHash
      }
    };
  }

  private async decisionState(request: OperationRequest, payload: JsonObject): Promise<"approved" | "pending" | "denied" | "invalid"> {
    if (request.risk === "read") return "approved";
    const decision = await readJson<DecisionDocument | null>(join(this.decisions, `${request.id}.json`), null);
    if (!decision) return "pending";
    if (String(decision.request_id ?? "") !== request.id) return "invalid";
    if (String(decision.decision ?? "") === "deny") return "denied";
    if (String(decision.decision ?? "") !== "approve") return "invalid";
    if (String(decision.fingerprint ?? "") !== sha256(payload as unknown as JsonValue)) return "invalid";
    return "approved";
  }

  private baseResult(request: OperationRequest, status: string, commands: SecretEvidence[], extra: JsonObject = {}): JsonObject {
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

  private async loadStage(plan: JsonObject, target: ManagedSecretTarget): Promise<{ path: string; text: string; stage: SecretStage }> {
    const stageRef = String(plan.stage_ref);
    const path = resolve(this.staging, stageRef);
    if (!path.startsWith(`${this.staging}/`)) throw new Error("stage_ref 路径逃逸");
    const info = await lstat(path);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error("secret stage 必须是普通文件且不能是符号链接");
    if (info.size < 2 || info.size > MAX_STAGE_BYTES) throw new Error("secret stage 大小非法");
    if ((info.mode & 0o077) !== 0) throw new Error("secret stage 权限必须为 0600");
    const text = await readFile(path, "utf8");
    if (rawSha256(text) !== String(plan.stage_sha256)) throw new Error("secret stage 内容哈希与审批请求不一致");
    const stage = validateSecretStage(JSON.parse(text) as JsonValue, target);
    if (stage.expected_inventory_hash !== String(plan.expected_inventory_hash)) throw new Error("secret stage 与请求的 inventory hash 不一致");
    return { path, text, stage };
  }

  private async inventory(request: OperationRequest, server: ManagedServer, target: ManagedSecretTarget): Promise<JsonObject> {
    const commands: SecretEvidence[] = [];
    const remoteDir = `/tmp/agenelf-secret-${request.id}`;
    const scriptPath = `${remoteDir}/inventory.py`;
    const prepare = await this.transport.run(server, `umask 077; mkdir -p ${quoteRemote(remoteDir)}`, 60_000);
    commands.push(secureEvidence("prepare", prepare));
    if (prepare.exit_code !== 0) return this.baseResult(request, "failed", commands, { reason: "无法创建远程临时目录" });
    const written = await this.transport.writeText(server, scriptPath, INVENTORY_SCRIPT, 60_000);
    commands.push(secureEvidence("script_write", written));
    if (written.exit_code !== 0) return this.baseResult(request, "failed", commands, { reason: "无法写入远程清单脚本" });
    const outcome = await this.transport.run(
      server,
      `python3 ${quoteRemote(scriptPath)} ${quoteRemote(target.envFile)} ${quoteRemote(seatPayload(target))}`,
      120_000
    );
    commands.push(secureEvidence("inventory", outcome));
    await this.transport.run(server, `rm -rf ${quoteRemote(remoteDir)}`, 30_000);
    if (outcome.exit_code !== 0) return this.baseResult(request, "failed", commands, { reason: "远程密钥清单读取失败" });
    const inventory = parseSecretInventory(outcome.stdout.trim(), target);
    return this.baseResult(request, "succeeded", commands, { inventory: inventory as unknown as JsonValue });
  }

  private async patch(request: OperationRequest, server: ManagedServer, target: ManagedSecretTarget, plan: JsonObject): Promise<JsonObject> {
    const commands: SecretEvidence[] = [];
    const loaded = await this.loadStage(plan, target);
    const remoteDir = `/tmp/agenelf-secret-${request.id}`;
    const scriptPath = `${remoteDir}/patch.py`;
    const stagePath = `${remoteDir}/stage.json`;
    const backupDir = `${target.envFile}.agenelf-backups`;
    const backupPath = `${backupDir}/${request.id}.env`;
    let retainBackup = false;
    const run = async (phase: string, command: string, timeoutMs: number) => {
      await this.event(request.id, "secret.ssh.started", { phase, target: request.target });
      const result = await this.transport.run(server, command, timeoutMs);
      commands.push(secureEvidence(phase, result));
      await this.event(request.id, "secret.ssh.completed", { phase, exit_code: result.exit_code });
      return result;
    };
    const write = async (phase: string, path: string, content: string) => {
      const result = await this.transport.writeText(server, path, content, 120_000);
      commands.push(secureEvidence(phase, result));
      return result;
    };
    const backupExists = async (): Promise<boolean> => {
      const result = await this.transport.run(server, `sudo -n test -f ${quoteRemote(backupPath)}`, 30_000);
      return result.exit_code === 0;
    };
    const cleanupRemote = async () => {
      let command = `sudo -n rm -rf ${quoteRemote(remoteDir)}`;
      if (!retainBackup) command += `; sudo -n rm -f ${quoteRemote(backupPath)}`;
      await this.transport.run(server, command, 60_000);
    };
    const rollback = async () => {
      await this.event(request.id, "secret.rollback.started", { env_target: target.alias });
      let command = `sudo -n cp ${quoteRemote(backupPath)} ${quoteRemote(target.envFile)} && sudo -n chmod 600 ${quoteRemote(target.envFile)}`;
      if (target.reload.type === "compose") {
        command += ` && cd ${quoteRemote(target.reload.workdir)} && ${server.dockerCommand} compose -p ${quoteRemote(target.reload.project)} -f ${quoteRemote(target.reload.composeFile)} --env-file ${quoteRemote(target.envFile)} up -d --remove-orphans`;
      }
      const result = await run("rollback", command, 1_200_000);
      if (result.exit_code !== 0) retainBackup = await backupExists();
      await this.event(request.id, "secret.rollback.completed", {
        env_target: target.alias,
        exit_code: result.exit_code,
        backup_retained: retainBackup,
        ...(retainBackup ? { recovery_backup_path: backupPath } : {})
      });
      return result;
    };
    const failWithRollback = async (reason: string): Promise<JsonObject> => {
      if (!(await backupExists())) {
        return this.baseResult(request, "failed", commands, {
          reason: `${reason}；尚未生成回滚备份，远程原子脚本未完成替换`,
          env_target: target.alias,
          rollback_status: "not_required",
          plaintext_backup_retained: false
        });
      }
      const rollbackResult = await rollback();
      if (rollbackResult.exit_code === 0) {
        return this.baseResult(request, "failed", commands, {
          reason: `${reason}，已恢复旧配置`,
          env_target: target.alias,
          rollback_status: "succeeded",
          plaintext_backup_retained: false
        });
      }
      return this.baseResult(request, "failed", commands, {
        reason: retainBackup
          ? `${reason}；自动回滚失败，已保留 0600 恢复备份`
          : `${reason}；自动回滚失败且未发现可用恢复备份`,
        env_target: target.alias,
        rollback_status: "failed",
        plaintext_backup_retained: retainBackup,
        ...(retainBackup ? { recovery_backup_path: backupPath } : {})
      });
    };

    try {
      const prepared = await run("prepare", `umask 077; mkdir -p ${quoteRemote(remoteDir)}; sudo -n mkdir -p ${quoteRemote(backupDir)}; sudo -n chmod 700 ${quoteRemote(backupDir)}`, 60_000);
      if (prepared.exit_code !== 0) return this.baseResult(request, "failed", commands, { reason: "无法准备远程安全目录" });
      if ((await write("script_write", scriptPath, PATCH_SCRIPT)).exit_code !== 0) return this.baseResult(request, "failed", commands, { reason: "无法写入远程修改脚本" });
      if ((await write("stage_write", stagePath, loaded.text)).exit_code !== 0) return this.baseResult(request, "failed", commands, { reason: "无法安全传输密钥变更包" });
      const applied = await run(
        "atomic_patch",
        `sudo -n python3 ${quoteRemote(scriptPath)} ${quoteRemote(target.envFile)} ${quoteRemote(seatPayload(target))} ${quoteRemote(stagePath)} ${quoteRemote(backupPath)}`,
        180_000
      );
      if (applied.exit_code !== 0) return failWithRollback("密钥文件原子修改失败");
      const summary = parsePatchSummary(applied.stdout.trim());
      if (target.reload.type === "compose") {
        const reload = target.reload;
        const prefix = `cd ${quoteRemote(reload.workdir)} && ${server.dockerCommand} compose -p ${quoteRemote(reload.project)} -f ${quoteRemote(reload.composeFile)} --env-file ${quoteRemote(target.envFile)}`;
        const validate = await run("compose_validate", `${prefix} config --quiet`, 180_000);
        if (validate.exit_code !== 0) return failWithRollback("Compose 配置校验失败");
        const deploy = await run("compose_reload", `${prefix} up -d --remove-orphans`, 1_200_000);
        if (deploy.exit_code !== 0) return failWithRollback("服务重载失败");
        if (reload.healthContainer) {
          const container = quoteRemote(reload.healthContainer);
          const health = await run(
            "health_check",
            `state=$(${server.dockerCommand} inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' ${container}); printf '%s\\n' "$state"; case "$state" in 'running healthy'|'running ') exit 0;; *) exit 1;; esac`,
            120_000
          );
          if (health.exit_code !== 0) return failWithRollback("健康检查失败");
        }
      }
      await this.event(request.id, "secret.patch.applied", {
        env_target: target.alias,
        inventory_hash_after: summary.inventory_hash_after as JsonValue,
        changes: summary.changes as JsonValue
      });
      return this.baseResult(request, "succeeded", commands, {
        env_target: target.alias,
        inventory_hash_before: loaded.stage.expected_inventory_hash,
        inventory_hash_after: summary.inventory_hash_after as JsonValue,
        changes: summary.changes as JsonValue,
        staging_consumed: true,
        plaintext_backup_retained: false
      });
    } finally {
      await cleanupRemote();
      await unlink(loaded.path).catch(() => undefined);
    }
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

  private async discardStage(plan: JsonObject): Promise<void> {
    const stageRef = String(plan.stage_ref ?? "");
    if (!SECRET_STAGE_RE.test(stageRef)) return;
    const path = resolve(this.staging, stageRef);
    if (path.startsWith(`${this.staging}/`)) await unlink(path).catch(() => undefined);
  }

  async processRequest(path: string): Promise<string> {
    const initial = await readJson<OperationRequest | null>(path, null);
    if (!initial || !isSecretOperationRequest(initial)) return "skipped";
    const requestId = String(initial.id ?? "");
    if (!REQUEST_RE.test(requestId)) return "invalid";
    const resultPath = join(this.results, `${requestId}.json`);
    if (await readJson<JsonObject | null>(resultPath, null)) return "done";
    let validated: ValidatedSecretRequest;
    try { validated = this.validate(initial); }
    catch (error) {
      const failed = this.baseResult(initial, "failed", [], { reason: sanitizeRemoteText(error instanceof Error ? `${error.name}: ${error.message}` : String(error)) });
      return this.persistTerminal(initial, failed, "ops.failed");
    }
    if (Date.parse(initial.expires_at) <= Date.now()) {
      await this.discardStage(validated.plan);
      return this.persistTerminal(initial, this.baseResult(initial, "expired", [], { reason: "操作请求已过期，未连接服务器" }));
    }
    const preState = await this.decisionState(initial, validated.payload);
    if (preState === "pending") return "pending";

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
      if (!request || request.id !== requestId || !isSecretOperationRequest(request)) throw new Error("锁后请求不存在或语义已变化");
      validated = this.validate(request);
      await this.event(requestId, "secret.runner.claimed", {
        capability: request.capability,
        operation: request.operation,
        target: request.target,
        risk: validated.risk,
        env_target: validated.target.alias
      });
      if (Date.parse(request.expires_at) <= Date.now()) {
        await this.discardStage(validated.plan);
        return this.persistTerminal(request, this.baseResult(request, "expired", [], { reason: "操作请求已过期，未连接服务器" }));
      }
      const state = await this.decisionState(request, validated.payload);
      await this.event(requestId, "ops.approval.checked", { state, fingerprint: request.fingerprint });
      if (state !== "approved") {
        await this.discardStage(validated.plan);
        return this.persistTerminal(request, this.baseResult(request, "blocked", [], {
          reason: state === "denied" ? "主人拒绝" : "授权缺失、无效或已被撤销"
        }));
      }
      const result = request.operation === "inventory_env"
        ? await this.inventory(request, validated.server, validated.target)
        : await this.patch(request, validated.server, validated.target, validated.plan);
      const status = await this.persistTerminal(request, result);
      await this.audit(status, `${request.id} ${request.target} ${request.operation} ${validated.target.alias}`);
      return status;
    } catch (error) {
      const request = await readJson<OperationRequest | null>(path, initial);
      const reason = sanitizeRemoteText(error instanceof Error ? `${error.name}: ${error.message}` : String(error));
      await this.discardStage(validated.plan);
      const status = await this.persistTerminal(request, this.baseResult(request, "failed", [], { reason }), "ops.failed");
      await this.audit("failed", `${requestId} ${reason}`);
      return status;
    } finally {
      await lock.close();
      await rm(lockPath, { force: true });
    }
  }

  async processOnce(): Promise<Record<string, number>> {
    if (!this.initialized) await this.initialize();
    try { await this.targets.initialize(); }
    catch (error) { await this.audit("profiles_reload_failed", error instanceof Error ? error.message : String(error)); }
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
