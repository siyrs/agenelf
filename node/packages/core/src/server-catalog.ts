import { lstat, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { parseSimpleYaml } from "./simple-yaml.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const ALIAS_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const HOST_RE = /^[A-Za-z0-9][A-Za-z0-9.:[\]_-]{0,252}$/;
const USER_RE = /^[A-Za-z_][A-Za-z0-9._-]{0,63}$/;
const FILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const ENV_RE = /^[A-Z_][A-Z0-9_]{0,127}$/;
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$/;

export type ServerAuth =
  | { type: "private_key"; privateKey: string; passphraseEnv?: string }
  | { type: "password_env"; passwordEnv: string };

export interface DockerCheck {
  container: string;
  argv: string[];
}

export interface ManagedServer {
  alias: string;
  host: string;
  port: number;
  username: string;
  connectTimeout: number;
  auth: ServerAuth;
  knownHosts: string;
  allowUnknownHostKey: boolean;
  dockerCommand: "docker" | "sudo -n docker";
  allowedOperations: Set<string> | null;
  allowedDockerOperations: Set<string> | null;
  allowedContainers: Set<string> | null;
  allowedServices: Set<string>;
  dockerChecks: Map<string, DockerCheck>;
}

function object(value: JsonValue | undefined, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} 必须是 mapping`);
  return value;
}

function stringList(value: JsonValue | undefined, label: string, pattern = NAME_RE): string[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error(`${label} 必须是 list`);
  if (value.length > 256) throw new Error(`${label} 项数过多`);
  return value.map((item) => {
    const text = String(item ?? "").trim();
    if (!pattern.test(text)) throw new Error(`${label} 含非法值：${text}`);
    return text;
  });
}

function boundedInteger(value: JsonValue | undefined, fallback: number, min: number, max: number, label: string): number {
  const parsed = Number(value ?? fallback);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) throw new Error(`${label} 必须在 ${min}-${max}`);
  return parsed;
}

function safeFile(value: unknown, label: string, fallback?: string): string {
  const text = String(value ?? fallback ?? "").trim();
  if (!FILE_RE.test(text) || text.includes("..")) throw new Error(`${label} 必须是 secrets 目录内的安全文件名`);
  return text;
}

function optionalEnv(value: unknown, label: string): string | undefined {
  const text = String(value ?? "").trim();
  if (!text) return undefined;
  if (!ENV_RE.test(text)) throw new Error(`${label} 环境变量名非法`);
  return text;
}

export class ServerCatalog {
  readonly root: string;
  readonly serversFile: string;
  readonly secretsDir: string;
  private records = new Map<string, ManagedServer>();

  constructor(
    root: string,
    serversFile = process.env.AGENELF_SERVERS_FILE || join(root, "local", "servers.yaml"),
    secretsDir = process.env.AGENELF_SECRETS_DIR || join(root, "local", "secrets")
  ) {
    this.root = resolve(root);
    this.serversFile = resolve(serversFile);
    this.secretsDir = resolve(secretsDir);
  }

  async initialize(): Promise<void> {
    const info = await lstat(this.serversFile);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error(`服务器配置不是普通文件：${this.serversFile}`);
    const parsed = parseSimpleYaml(await readFile(this.serversFile, "utf8"));
    const servers = object(parsed.servers, "servers");
    const next = new Map<string, ManagedServer>();
    for (const [alias, raw] of Object.entries(servers)) {
      if (!ALIAS_RE.test(alias)) throw new Error(`非法服务器别名：${alias}`);
      const value = object(raw, `servers.${alias}`);
      const host = String(value.host ?? "").trim();
      const username = String(value.username ?? "").trim();
      if (!HOST_RE.test(host) || /\s/.test(host)) throw new Error(`服务器 ${alias} host 非法`);
      if (!USER_RE.test(username)) throw new Error(`服务器 ${alias} username 非法`);
      const authValue = object(value.auth, `servers.${alias}.auth`);
      const authType = String(authValue.type ?? "private_key").trim();
      let auth: ServerAuth;
      if (authType === "private_key") {
        auth = {
          type: "private_key",
          privateKey: safeFile(authValue.private_key, `服务器 ${alias} private_key`, "id_ed25519"),
          passphraseEnv: optionalEnv(authValue.passphrase_env, `服务器 ${alias} passphrase_env`)
        };
      } else if (authType === "password_env") {
        const passwordEnv = optionalEnv(authValue.password_env, `服务器 ${alias} password_env`);
        if (!passwordEnv) throw new Error(`服务器 ${alias} password_env 不能为空`);
        auth = { type: "password_env", passwordEnv };
      } else throw new Error(`服务器 ${alias} auth.type 不受支持：${authType}`);

      const dockerCommand = String(value.docker_command ?? "docker").trim();
      if (dockerCommand !== "docker" && dockerCommand !== "sudo -n docker") throw new Error(`服务器 ${alias} docker_command 非法`);
      const checks = new Map<string, DockerCheck>();
      const rawChecks = value.docker_checks === undefined ? {} : object(value.docker_checks, `servers.${alias}.docker_checks`);
      for (const [checkAlias, checkRaw] of Object.entries(rawChecks)) {
        if (!ALIAS_RE.test(checkAlias)) throw new Error(`服务器 ${alias} Docker 检查别名非法`);
        const check = object(checkRaw, `servers.${alias}.docker_checks.${checkAlias}`);
        const container = String(check.container ?? "").trim();
        if (!NAME_RE.test(container)) throw new Error(`Docker 检查 ${checkAlias} container 非法`);
        if (!Array.isArray(check.argv) || check.argv.length < 1 || check.argv.length > 32) throw new Error(`Docker 检查 ${checkAlias} argv 必须是 1-32 项`);
        const argv = check.argv.map((item) => {
          const text = String(item ?? "");
          if (!text || text.length > 500 || /[\n\r\0]/.test(text)) throw new Error(`Docker 检查 ${checkAlias} argv 非法`);
          return text;
        });
        checks.set(checkAlias, { container, argv });
      }

      next.set(alias, {
        alias,
        host,
        port: boundedInteger(value.port, 22, 1, 65_535, `服务器 ${alias} port`),
        username,
        connectTimeout: boundedInteger(value.connect_timeout, 10, 1, 120, `服务器 ${alias} connect_timeout`),
        auth,
        knownHosts: safeFile(value.known_hosts, `服务器 ${alias} known_hosts`, "known_hosts"),
        allowUnknownHostKey: value.allow_unknown_host_key === true,
        dockerCommand,
        allowedOperations: value.allowed_operations === undefined ? null : new Set(stringList(value.allowed_operations, `服务器 ${alias} allowed_operations`, ALIAS_RE)),
        allowedDockerOperations: value.allowed_docker_operations === undefined ? null : new Set(stringList(value.allowed_docker_operations, `服务器 ${alias} allowed_docker_operations`, ALIAS_RE)),
        allowedContainers: value.allowed_containers === undefined ? null : new Set(stringList(value.allowed_containers, `服务器 ${alias} allowed_containers`, NAME_RE)),
        allowedServices: new Set(stringList(value.allowed_services, `服务器 ${alias} allowed_services`, NAME_RE)),
        dockerChecks: checks
      });
    }
    if (!next.size) throw new Error("servers.yaml 没有可用服务器");
    this.records = next;
  }

  list(): JsonObject[] {
    return [...this.records.values()].map((server) => ({
      alias: server.alias,
      host: server.host,
      port: server.port,
      username: server.username,
      auth: server.auth.type,
      allow_unknown_host_key: server.allowUnknownHostKey,
      allowed_operations: server.allowedOperations ? [...server.allowedOperations].sort() : [],
      allowed_docker_operations: server.allowedDockerOperations ? [...server.allowedDockerOperations].sort() : []
    }));
  }

  get(alias: string): ManagedServer {
    if (!ALIAS_RE.test(alias)) throw new Error(`非法服务器别名：${alias}`);
    const server = this.records.get(alias);
    if (!server) throw new Error(`未知服务器别名：${alias}`);
    return server;
  }

  secretPath(file: string): string {
    const candidate = resolve(this.secretsDir, safeFile(file, "secret file"));
    if (!candidate.startsWith(`${this.secretsDir}/`)) throw new Error("secret file 路径逃逸");
    return candidate;
  }
}
