import { lstat, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { parseSimpleYaml } from "./simple-yaml.ts";
import { ServerCatalog, type ManagedServer } from "./server-catalog.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const ALIAS_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const SEAT_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const ENV_RE = /^[A-Z_][A-Z0-9_]{0,127}$/;
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$/;

export interface SecretSeat {
  id: string;
  envName: string;
  label: string;
}

export type SecretReload =
  | { type: "none" }
  | {
      type: "compose";
      workdir: string;
      composeFile: string;
      project: string;
      healthContainer?: string;
    };

export interface ManagedSecretTarget {
  alias: string;
  label?: string;
  aliases?: string[];
  serverAlias: string;
  envFile: string;
  seats: Map<string, SecretSeat>;
  reload: SecretReload;
}

function object(value: JsonValue | undefined, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} 必须是 mapping`);
  return value;
}

function safeAlias(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!ALIAS_RE.test(text)) throw new Error(`${label} 非法`);
  return text;
}

function displayText(value: unknown, fallback: string, label: string): string {
  const text = String(value ?? fallback).trim();
  if (!text || text.length > 128 || /[\0\r\n]/.test(text)) throw new Error(`${label} 必须是 1-128 字符的单行文本`);
  return text;
}

function displayAliases(value: JsonValue | undefined, label: string): string[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.length > 16) throw new Error(`${label} 必须是最多 16 项的 list`);
  const aliases = value.map((item, index) => displayText(item, "", `${label}[${index}]`));
  return [...new Set(aliases)];
}

function safeAbsolute(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!text.startsWith("/") || /[\0\r\n]/.test(text)) throw new Error(`${label} 必须是绝对 Linux 路径`);
  const normalized = resolve(text);
  if (normalized === "/") throw new Error(`${label} 禁止使用宿主机根目录`);
  return normalized;
}

function requireUnder(path: string, root: string, label: string): void {
  if (path !== root && !path.startsWith(`${root}/`)) throw new Error(`${label} 必须位于服务器 managed_root 下`);
}

function parseReload(value: JsonValue | undefined, server: ManagedServer, envFile: string, label: string): SecretReload {
  if (value === undefined || value === null) return { type: "none" };
  const config = object(value, label);
  const type = String(config.type ?? "none").trim();
  if (type === "none") return { type: "none" };
  if (type !== "compose") throw new Error(`${label}.type 仅支持 none 或 compose`);
  const workdir = safeAbsolute(config.workdir ?? dirname(envFile), `${label}.workdir`);
  const composeFile = safeAbsolute(config.compose_file ?? `${workdir}/compose.yaml`, `${label}.compose_file`);
  requireUnder(workdir, server.managedRoot, `${label}.workdir`);
  requireUnder(composeFile, server.managedRoot, `${label}.compose_file`);
  const project = String(config.project ?? "").trim();
  if (!NAME_RE.test(project)) throw new Error(`${label}.project 非法`);
  const healthContainer = String(config.health_container ?? "").trim() || undefined;
  if (healthContainer && !NAME_RE.test(healthContainer)) throw new Error(`${label}.health_container 非法`);
  if (healthContainer && server.allowedContainers && !server.allowedContainers.has(healthContainer)) {
    throw new Error(`${label}.health_container 不在服务器 allowed_containers 中`);
  }
  return { type: "compose", workdir, composeFile, project, healthContainer };
}

export class SecretTargetCatalog {
  readonly root: string;
  readonly servers: ServerCatalog;
  readonly configFile: string;
  private records = new Map<string, ManagedSecretTarget>();

  constructor(
    root: string,
    servers = new ServerCatalog(root),
    configFile = process.env.AGENELF_ENV_SECRETS_FILE || resolve(root, "local", "env-secrets.yaml")
  ) {
    this.root = resolve(root);
    this.servers = servers;
    this.configFile = resolve(configFile);
  }

  async initialize(): Promise<void> {
    await this.servers.initialize();
    const info = await lstat(this.configFile);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error(`密钥目标配置不是普通文件：${this.configFile}`);
    const parsed = parseSimpleYaml(await readFile(this.configFile, "utf8"));
    if (Number(parsed.schema_version ?? 1) !== 1) throw new Error("env-secrets.yaml schema_version 必须为 1");
    const targets = object(parsed.targets, "targets");
    const next = new Map<string, ManagedSecretTarget>();
    for (const [alias, raw] of Object.entries(targets)) {
      if (!ALIAS_RE.test(alias)) throw new Error(`非法密钥目标别名：${alias}`);
      const value = object(raw, `targets.${alias}`);
      const label = displayText(value.label, alias, `targets.${alias}.label`);
      const aliases = displayAliases(value.aliases, `targets.${alias}.aliases`)
        .filter((item) => item !== alias && item !== label);
      const serverAlias = safeAlias(value.server, `targets.${alias}.server`);
      const server = this.servers.get(serverAlias);
      const envFile = safeAbsolute(value.env_file, `targets.${alias}.env_file`);
      requireUnder(envFile, server.managedRoot, `targets.${alias}.env_file`);
      const seatValues = object(value.seats, `targets.${alias}.seats`);
      const entries = Object.entries(seatValues);
      if (!entries.length || entries.length > 64) throw new Error(`targets.${alias}.seats 必须包含 1-64 个席位`);
      const seats = new Map<string, SecretSeat>();
      const envNames = new Set<string>();
      for (const [seatId, seatRaw] of entries) {
        if (!SEAT_RE.test(seatId)) throw new Error(`targets.${alias}.seats 含非法席位 ID：${seatId}`);
        let envName = "";
        let seatLabel = seatId;
        if (typeof seatRaw === "string") envName = seatRaw.trim();
        else {
          const seat = object(seatRaw, `targets.${alias}.seats.${seatId}`);
          envName = String(seat.env ?? seat.env_name ?? "").trim();
          seatLabel = displayText(seat.label, seatId, `targets.${alias}.seats.${seatId}.label`);
        }
        if (!ENV_RE.test(envName)) throw new Error(`席位 ${seatId} 的环境变量名非法`);
        if (envNames.has(envName)) throw new Error(`密钥目标 ${alias} 重复映射环境变量：${envName}`);
        envNames.add(envName);
        seats.set(seatId, { id: seatId, envName, label: seatLabel });
      }
      next.set(alias, {
        alias,
        label,
        aliases,
        serverAlias,
        envFile,
        seats,
        reload: parseReload(value.reload, server, envFile, `targets.${alias}.reload`)
      });
    }
    this.records = next;
  }

  list(): JsonObject[] {
    return [...this.records.values()].map((target) => ({
      alias: target.alias,
      label: target.label ?? target.alias,
      aliases: target.aliases ?? [],
      server: target.serverAlias,
      env_file: target.envFile,
      seats: [...target.seats.values()].map((seat) => ({ id: seat.id, env_name: seat.envName, label: seat.label })),
      reload: target.reload.type
    }));
  }

  get(alias: string): ManagedSecretTarget {
    if (!ALIAS_RE.test(alias)) throw new Error(`非法密钥目标别名：${alias}`);
    const target = this.records.get(alias);
    if (!target) throw new Error(`未知密钥目标：${alias}`);
    return target;
  }
}
