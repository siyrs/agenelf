import { chmod, lstat, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { parseSimpleYaml } from "./simple-yaml.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const ALIAS_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const HOST_RE = /^[A-Za-z0-9][A-Za-z0-9.:[\]_-]{0,252}$/;
const USER_RE = /^[A-Za-z_][A-Za-z0-9._-]{0,63}$/;
const SECRET_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export type ServerAuth = "key" | "password";

export interface ManagedServer {
  alias: string;
  description: string;
  host: string;
  port: number;
  user: string;
  auth: ServerAuth;
  secretFile: string;
}

export interface PreparedCredential {
  mode: ServerAuth;
  path: string;
  cleanup(): Promise<void>;
}

function asObject(value: JsonValue | undefined, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} 必须是 mapping`);
  return value;
}

function boundedPort(value: JsonValue | undefined): number {
  const parsed = Number(value ?? 22);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65_535) throw new Error("服务器端口必须在 1-65535");
  return parsed;
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
    const servers = asObject(parsed.servers, "servers");
    const next = new Map<string, ManagedServer>();
    for (const [alias, raw] of Object.entries(servers)) {
      if (!ALIAS_RE.test(alias)) throw new Error(`非法服务器别名：${alias}`);
      const value = asObject(raw, `servers.${alias}`);
      const host = String(value.host ?? "").trim();
      const user = String(value.user ?? value.username ?? "").trim();
      const auth = String(value.auth ?? "key").trim().toLowerCase();
      const secretFile = String(value.secret_file ?? value.secretFile ?? "").trim();
      if (!HOST_RE.test(host) || /\s/.test(host)) throw new Error(`服务器 ${alias} host 非法`);
      if (!USER_RE.test(user)) throw new Error(`服务器 ${alias} user 非法`);
      if (auth !== "key" && auth !== "password") throw new Error(`服务器 ${alias} auth 仅支持 key/password`);
      if (!SECRET_RE.test(secretFile) || secretFile.includes("..")) throw new Error(`服务器 ${alias} secret_file 非法`);
      if (next.has(alias)) throw new Error(`服务器别名重复：${alias}`);
      next.set(alias, {
        alias,
        description: String(value.description ?? "").trim().slice(0, 500),
        host,
        port: boundedPort(value.port),
        user,
        auth,
        secretFile
      });
    }
    this.records = next;
  }

  list(): JsonObject[] {
    return [...this.records.values()].map((server) => ({
      alias: server.alias,
      description: server.description,
      host: server.host,
      port: server.port,
      user: server.user,
      auth: server.auth
    }));
  }

  get(alias: string): ManagedServer {
    if (!ALIAS_RE.test(alias)) throw new Error(`非法服务器别名：${alias}`);
    const server = this.records.get(alias);
    if (!server) throw new Error(`未知服务器别名：${alias}`);
    return server;
  }

  private secretPath(server: ManagedServer): string {
    const candidate = resolve(this.secretsDir, server.secretFile);
    if (!candidate.startsWith(`${this.secretsDir}/`)) throw new Error("secret_file 路径逃逸");
    return candidate;
  }

  async prepareCredential(server: ManagedServer): Promise<PreparedCredential> {
    const source = this.secretPath(server);
    const info = await lstat(source);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error(`服务器 ${server.alias} 凭据不是普通文件`);
    if (info.size < 1 || info.size > 128 * 1024) throw new Error(`服务器 ${server.alias} 凭据大小非法`);
    const value = await readFile(source);
    if (value.includes(0)) throw new Error(`服务器 ${server.alias} 凭据包含 NUL`);
    const directory = await mkdtemp(join(tmpdir(), "agenelf-read-ops-"));
    const target = join(directory, server.auth === "key" ? "identity" : "password");
    if (server.auth === "password") {
      const text = value.toString("utf8").replace(/\r?\n$/, "");
      if (!text || text.includes("\n") || text.includes("\r")) {
        await rm(directory, { recursive: true, force: true });
        throw new Error(`服务器 ${server.alias} 密码文件必须只包含一行`);
      }
      await writeFile(target, `${text}\n`, { mode: 0o600 });
    } else {
      await writeFile(target, value, { mode: 0o600 });
    }
    await chmod(target, 0o600);
    let cleaned = false;
    return {
      mode: server.auth,
      path: target,
      cleanup: async () => {
        if (cleaned) return;
        cleaned = true;
        await rm(directory, { recursive: true, force: true });
      }
    };
  }
}
