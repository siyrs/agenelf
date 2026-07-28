import { chmod, lstat, mkdtemp, open, readdir, rm, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { appendLine, atomicWriteJson, readJson } from "./fs-store.ts";
import { randomId, sha256 } from "./canonical.ts";
import { redactSensitiveText, sanitizeObject } from "./privacy.ts";
import { ServerCatalog, type ManagedServer } from "./server-catalog.ts";
import type { OperationRequest } from "./operation-queue.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const REQUEST_RE = /^op-[0-9a-f]{16}$/;
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$/;
const ALIAS_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const MAX_OUTPUT = 100_000;

export const READ_SERVER_OPERATIONS = new Set(["inspect", "docker_ps", "service_status"]);
export const READ_DOCKER_OPERATIONS = new Set(["get_docker_logs", "inspect_docker_container", "run_docker_check"]);

const PROXY_URI_RE = /\b(vmess|vless|trojan|ss|ssr|hysteria2?|tuic):\/\/[^\s"']+/gi;
const URL_SECRET_RE = /([?&](?:token|secret|password|passwd|api[_-]?key|key)=)[^&\s"']+/gi;
const INSPECT_FORMAT = '{"Name":{{json .Name}},"Image":{{json .Config.Image}},"State":{{json .State}},"Mounts":{{json .Mounts}},"Labels":{{json .Config.Labels}},"RestartPolicy":{{json .HostConfig.RestartPolicy}},"NetworkMode":{{json .HostConfig.NetworkMode}},"Networks":{{json .NetworkSettings.Networks}}}';

function now(): string { return new Date().toISOString(); }
function truncate(value: string): string { return value.length <= MAX_OUTPUT ? value : `${value.slice(0, MAX_OUTPUT)}\n...（输出已截断）`; }
function sanitizeText(value: unknown): string {
  return redactSensitiveText(value)
    .replace(PROXY_URI_RE, (_match, scheme) => `${scheme}://[REDACTED]`)
    .replace(URL_SECRET_RE, "$1[REDACTED]");
}
function quote(value: string): string { return `'${value.replace(/'/g, `'"'"'`)}'`; }
function parameters(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("parameters 必须是 object");
  return value as JsonObject;
}
function safeName(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!NAME_RE.test(text)) throw new Error(`${label} 非法`);
  return text;
}
function safeAlias(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!ALIAS_RE.test(text)) throw new Error(`${label} 非法`);
  return text;
}
function canonicalPayload(request: OperationRequest): JsonObject {
  return {
    capability: request.capability.trim(),
    operation: request.operation.trim(),
    target: request.target.trim(),
    parameters: request.parameters
  };
}

export function isSemanticReadRequest(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const request = value as Partial<OperationRequest>;
  return (request.capability === "server.operations" && READ_SERVER_OPERATIONS.has(String(request.operation ?? "")))
    || (request.capability === "docker.operations" && READ_DOCKER_OPERATIONS.has(String(request.operation ?? "")));
}

export interface RemoteCommandResult {
  command: string;
  exit_code: number;
  stdout: string;
  stderr: string;
}

export type RemoteExecutor = (server: ManagedServer, command: string, timeoutMs: number) => Promise<RemoteCommandResult>;

async function checkedSecret(path: string, label: string): Promise<void> {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error(`${label} 必须是普通文件`);
  if (info.size < 1 || info.size > 128 * 1024) throw new Error(`${label} 大小非法`);
}

async function askpass(value: string): Promise<{ path: string; cleanup(): Promise<void> }> {
  if (!value || /[\r\n\0]/.test(value)) throw new Error("SSH 密码或私钥口令非法");
  const directory = await mkdtemp(join(tmpdir(), "agenelf-ssh-askpass-"));
  const path = join(directory, "askpass.mjs");
  await writeFile(path, "process.stdout.write(process.env.AGENELF_SSH_ASKPASS_VALUE ?? '');\n", { mode: 0o700 });
  await chmod(path, 0o700);
  return { path, cleanup: () => rm(directory, { recursive: true, force: true }) };
}

export function createOpenSshExecutor(catalog: ServerCatalog): RemoteExecutor {
  return async (server, remoteCommand, timeoutMs) => {
    const args = [
      "-p", String(server.port),
      "-o", `ConnectTimeout=${server.connectTimeout}`,
      "-o", "ConnectionAttempts=1",
      "-o", "ServerAliveInterval=10",
      "-o", "ServerAliveCountMax=1",
      "-o", "LogLevel=ERROR",
      "-o", "BatchMode=no"
    ];
    let askpassFile: { path: string; cleanup(): Promise<void> } | null = null;
    const environment = { ...process.env };
    const knownHosts = catalog.secretPath(server.knownHosts);
    if (server.allowUnknownHostKey) {
      args.push("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null");
    } else {
      await checkedSecret(knownHosts, "known_hosts");
      args.push("-o", "StrictHostKeyChecking=yes", "-o", `UserKnownHostsFile=${knownHosts}`);
    }
    if (server.auth.type === "private_key") {
      const identity = catalog.secretPath(server.auth.privateKey);
      await checkedSecret(identity, "SSH 私钥");
      args.push("-o", "IdentitiesOnly=yes", "-i", identity);
      if (server.auth.passphraseEnv) {
        const value = String(process.env[server.auth.passphraseEnv] ?? "");
        if (!value) throw new Error(`SSH 私钥口令环境变量未设置：${server.auth.passphraseEnv}`);
        askpassFile = await askpass(value);
      }
    } else {
      const value = String(process.env[server.auth.passwordEnv] ?? "");
      if (!value) throw new Error(`SSH 密码环境变量未设置：${server.auth.passwordEnv}`);
      askpassFile = await askpass(value);
      args.push("-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no");
    }
    if (askpassFile) {
      environment.SSH_ASKPASS = askpassFile.path;
      environment.SSH_ASKPASS_REQUIRE = "force";
      environment.DISPLAY = environment.DISPLAY || "agenelf:0";
      environment.AGENELF_SSH_ASKPASS_VALUE = server.auth.type === "private_key"
        ? String(process.env[server.auth.passphraseEnv ?? ""] ?? "")
        : String(process.env[server.auth.passwordEnv] ?? "");
    }
    args.push(`${server.username}@${server.host}`, remoteCommand);
    try {
      return await new Promise<RemoteCommandResult>((resolvePromise, reject) => {
        const child = spawn("ssh", args, { env: environment, shell: false, stdio: ["ignore", "pipe", "pipe"] });
        let stdout = "";
        let stderr = "";
        let total = 0;
        const collect = (kind: "stdout" | "stderr", chunk: Buffer) => {
          total += chunk.length;
          if (total > MAX_OUTPUT * 2) {
            child.kill("SIGKILL");
            return;
          }
          if (kind === "stdout") stdout += chunk.toString("utf8"); else stderr += chunk.toString("utf8");
        };
        child.stdout.on("data", (chunk: Buffer) => collect("stdout", chunk));
        child.stderr.on("data", (chunk: Buffer) => collect("stderr", chunk));
        const timer = setTimeout(() => child.kill("SIGKILL"), Math.max(1_000, Math.min(timeoutMs, 15 * 60_000)));
        child.once("error", (error) => { clearTimeout(timer); reject(error); });
        child.once("close", (code, signal) => {
          clearTimeout(timer);
          resolvePromise({
            command: remoteCommand,
            exit_code: typeof code === "number" ? code : signal ? 124 : 126,
            stdout: truncate(sanitizeText(stdout)),
            stderr: truncate(sanitizeText(stderr))
          });
        });
      });
    } finally {
      delete environment.AGENELF_SSH_ASKPASS_VALUE;
      await askpassFile?.cleanup();
    }
  };
}

function commandFor(request: OperationRequest, server: ManagedServer): { command: string; timeoutMs: number } {
  const params = parameters(request.parameters);
  if (request.capability === "server.operations") {
    if (server.allowedOperations && !server.allowedOperations.has(request.operation)) throw new Error(`服务器策略未允许操作：${request.operation}`);
    if (request.operation === "inspect") {
      if (Object.keys(params).length) throw new Error("inspect 不接受 parameters");
      return {
        command: "set -eu; echo '=== identity ==='; hostname; id; uname -a; uptime; echo '=== disk ==='; df -h; echo '=== memory ==='; (free -h || true); echo '=== docker ==='; (command -v docker >/dev/null && docker version --format '{{.Server.Version}}' && docker ps --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}') || true",
        timeoutMs: 60_000
      };
    }
    if (request.operation === "docker_ps") {
      if (Object.keys(params).length) throw new Error("docker_ps 不接受 parameters");
      return { command: `${server.dockerCommand} ps -a --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}'`, timeoutMs: 60_000 };
    }
    if (request.operation === "service_status") {
      if (Object.keys(params).some((key) => key !== "service")) throw new Error("service_status 含未知参数");
      const service = safeName(params.service, "service");
      if (!server.allowedServices.has(service)) throw new Error(`服务不在允许清单：${service}`);
      return { command: `systemctl status --no-pager --full ${quote(service)}`, timeoutMs: 60_000 };
    }
  }
  if (request.capability === "docker.operations") {
    if (server.allowedDockerOperations && !server.allowedDockerOperations.has(request.operation)) throw new Error(`目标 Docker 策略未允许操作：${request.operation}`);
    if (request.operation === "get_docker_logs") {
      if (Object.keys(params).some((key) => !["container", "tail"].includes(key))) throw new Error("get_docker_logs 含未知参数");
      const container = safeName(params.container, "container");
      if (server.allowedContainers && !server.allowedContainers.has(container)) throw new Error(`容器不在允许清单：${container}`);
      const tail = Number(params.tail ?? 100);
      if (!Number.isInteger(tail) || tail < 1 || tail > 1_000) throw new Error("tail 必须在 1-1000");
      return { command: `${server.dockerCommand} logs --tail ${tail} ${quote(container)}`, timeoutMs: 120_000 };
    }
    if (request.operation === "inspect_docker_container") {
      if (Object.keys(params).some((key) => key !== "container")) throw new Error("inspect_docker_container 含未知参数");
      const container = safeName(params.container, "container");
      if (server.allowedContainers && !server.allowedContainers.has(container)) throw new Error(`容器不在允许清单：${container}`);
      return { command: `${server.dockerCommand} inspect --type container --format ${quote(INSPECT_FORMAT)} ${quote(container)}`, timeoutMs: 120_000 };
    }
    if (request.operation === "run_docker_check") {
      if (Object.keys(params).some((key) => key !== "check")) throw new Error("run_docker_check 含未知参数");
      const alias = safeAlias(params.check, "check");
      const check = server.dockerChecks.get(alias);
      if (!check) throw new Error(`未配置 Docker 诊断别名：${alias}`);
      if (server.allowedContainers && !server.allowedContainers.has(check.container)) throw new Error(`容器不在允许清单：${check.container}`);
      return { command: `${server.dockerCommand} exec ${quote(check.container)} ${check.argv.map(quote).join(" ")}`, timeoutMs: 300_000 };
    }
  }
  throw new Error("请求不是受支持的只读操作");
}

export class ReadOnlyOpsRunner {
  readonly root: string;
  readonly catalog: ServerCatalog;
  readonly executeRemote: RemoteExecutor;
  readonly requests: string;
  readonly results: string;
  readonly locks: string;
  readonly events: string;
  readonly auditPath: string;
  private initialized = false;

  constructor(root: string, options: { catalog?: ServerCatalog; executeRemote?: RemoteExecutor } = {}) {
    this.root = root;
    this.catalog = options.catalog ?? new ServerCatalog(root);
    this.executeRemote = options.executeRemote ?? createOpenSshExecutor(this.catalog);
    this.requests = join(root, "data", "ops-requests");
    this.results = join(root, "data", "ops-results");
    this.locks = join(root, "data", "ops-locks");
    this.events = join(root, "data", "ops-events");
    this.auditPath = join(root, "logs", "read-ops-runner.log");
  }

  async initialize(): Promise<void> {
    await this.catalog.initialize();
    this.initialized = true;
  }

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

  private async audit(event: string, detail: string): Promise<void> {
    await appendLine(this.auditPath, `[${now()}] [${event}] ${sanitizeText(detail)}`);
  }

  async processRequest(path: string): Promise<string> {
    const request = await readJson<OperationRequest | null>(path, null);
    if (!request || !isSemanticReadRequest(request)) return "skipped";
    const requestId = String(request.id ?? "");
    if (!REQUEST_RE.test(requestId)) return "invalid";
    const resultPath = join(this.results, `${requestId}.json`);
    if (await readJson<JsonObject | null>(resultPath, null)) return "done";
    const lockPath = join(this.locks, `${requestId}.lock`);
    let lock;
    try {
      lock = await open(lockPath, "wx", 0o600);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return "locked";
      throw error;
    }
    try {
      if (await readJson<JsonObject | null>(resultPath, null)) return "done";
      await this.event(requestId, "ops.runner.claimed", { capability: request.capability, operation: request.operation, target: request.target });
      if (request.schema_version !== 1) throw new Error("不支持的操作请求版本");
      if (request.risk !== "read") throw new Error(`只读操作必须声明 read 风险，实际为 ${request.risk}`);
      if (!request.parameters || typeof request.parameters !== "object" || Array.isArray(request.parameters)) throw new Error("parameters 必须是 object");
      const fingerprint = sha256(canonicalPayload(request) as unknown as JsonValue);
      if (fingerprint !== request.fingerprint) throw new Error("请求指纹校验失败，文件可能被篡改");
      const expiresAt = Date.parse(String(request.expires_at ?? ""));
      if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
        const expired = { schema_version: 1, id: requestId, status: "expired", capability: request.capability, operation: request.operation, target: request.target, reason: "操作请求已过期，未连接服务器", finished_at: now(), commands: [] };
        await atomicWriteJson(resultPath, expired, true);
        await this.event(requestId, "ops.result.persisted", { status: "expired" });
        return "expired";
      }
      const server = this.catalog.get(request.target);
      const planned = commandFor(request, server);
      const startedAt = now();
      await this.event(requestId, "ssh.started", { target: request.target, operation: request.operation });
      const command = await this.executeRemote(server, planned.command, planned.timeoutMs);
      await this.event(requestId, "ssh.completed", { exit_code: command.exit_code });
      const result = {
        schema_version: 1,
        id: requestId,
        status: command.exit_code === 0 ? "succeeded" : "failed",
        target: request.target,
        capability: request.capability,
        operation: request.operation,
        started_at: startedAt,
        finished_at: now(),
        commands: [{
          command: sanitizeText(command.command),
          exit_code: command.exit_code,
          stdout: truncate(sanitizeText(command.stdout)),
          stderr: truncate(sanitizeText(command.stderr))
        }]
      };
      await atomicWriteJson(resultPath, result, true);
      await this.event(requestId, "ops.result.persisted", { status: result.status, exit_code: command.exit_code });
      await this.audit(result.status, `${requestId} ${request.target} ${request.operation}`);
      return result.status;
    } catch (error) {
      const reason = `${error instanceof Error ? error.name : "Error"}: ${error instanceof Error ? error.message : String(error)}`;
      const failed = { schema_version: 1, id: requestId, status: "failed", reason: sanitizeText(reason), finished_at: now(), commands: [] };
      try { await atomicWriteJson(resultPath, failed, true); } catch { /* another trusted writer won */ }
      try { await this.event(requestId, "ops.failed", { reason: sanitizeText(reason) }); } catch { /* preserve primary failure */ }
      await this.audit("failed", `${requestId} ${reason}`);
      return "failed";
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
