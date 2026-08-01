import { randomBytes } from "node:crypto";
import type { JsonObject, JsonValue } from "./types.ts";
import {
  INVENTORY_SCRIPT,
  PATCH_SCRIPT,
  REVEAL_SCRIPT,
  parseSecretInventory,
  type SecretInventory,
  type SecretMutation,
  type SecretStage
} from "./secret-env.ts";
import { SecretTargetCatalog, type ManagedSecretTarget } from "./secret-targets.ts";
import { ServerCatalog, type ManagedServer } from "./server-catalog.ts";
import { OpenSshTransport, quoteRemote, type RemoteCommandResult } from "./open-ssh.ts";

const SHA256_RE = /^[0-9a-f]{64}$/;
const SEAT_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const MAX_SECRET_CHARS = 32 * 1024;

export interface SecretChatTransport {
  run(server: ManagedServer, command: string, timeoutMs: number): Promise<RemoteCommandResult>;
  writeText(server: ManagedServer, remotePath: string, content: string, timeoutMs?: number): Promise<RemoteCommandResult>;
}

export interface SecretChatChange extends JsonObject {
  seat_id: string;
  action: "keep" | "delete" | "set";
  value?: string;
}

function nonce(): string {
  return randomBytes(12).toString("hex");
}

function object(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} 必须是 object`);
  return value as JsonObject;
}

function assertOk(result: RemoteCommandResult, label: string): void {
  if (result.exit_code !== 0) throw new Error(`${label}失败；远程输出已抑制`);
}

function seatPayload(target: ManagedSecretTarget): string {
  return JSON.stringify([...target.seats.values()].map((seat) => ({ seat_id: seat.id, env_name: seat.envName })));
}

function revealValue(text: string): string {
  let parsed: JsonObject;
  try { parsed = object(JSON.parse(text) as JsonValue, "reveal result"); }
  catch { throw new Error("远程明文读取结果不是有效 JSON"); }
  if (Number(parsed.schema_version) !== 1 || typeof parsed.value_b64 !== "string") throw new Error("远程明文读取结果非法");
  const value = Buffer.from(parsed.value_b64, "base64").toString("utf8");
  if (!value || value.length > MAX_SECRET_CHARS || /[\0\r\n]/.test(value)) throw new Error("密钥包含不支持的控制字符或长度非法");
  return value;
}

function patchSummary(text: string): JsonObject {
  const document = object(JSON.parse(text) as JsonValue, "patch result");
  if (Number(document.schema_version) !== 1) throw new Error("远程修改结果版本非法");
  const inventoryHash = String(document.inventory_hash_after ?? "");
  if (!SHA256_RE.test(inventoryHash) || !Array.isArray(document.changes)) throw new Error("远程修改结果非法");
  return {
    schema_version: 1,
    inventory_hash_after: inventoryHash,
    changes: document.changes.map((raw) => {
      const item = object(raw, "patch change");
      const action = String(item.action ?? "");
      if (!["keep", "delete", "set"].includes(action)) throw new Error("远程修改 action 非法");
      return {
        seat_id: String(item.seat_id ?? ""),
        action,
        old_fingerprint: String(item.old_fingerprint ?? "").slice(0, 16),
        new_fingerprint: String(item.new_fingerprint ?? "").slice(0, 16),
        present: item.present === true
      };
    })
  };
}

function normalizeChanges(value: unknown, target: ManagedSecretTarget): Map<string, SecretChatChange> {
  if (!Array.isArray(value) || value.length < 1 || value.length > target.seats.size) {
    throw new Error(`changes 必须包含 1-${target.seats.size} 个席位变更`);
  }
  const rows = new Map<string, SecretChatChange>();
  for (const [index, raw] of value.entries()) {
    const item = object(raw, `changes[${index}]`);
    const unknown = Object.keys(item).filter((key) => !["seat_id", "action", "value"].includes(key));
    if (unknown.length) throw new Error(`changes[${index}] 含未知字段：${unknown.join(", ")}`);
    const seatId = String(item.seat_id ?? "").trim();
    if (!SEAT_RE.test(seatId) || !target.seats.has(seatId)) throw new Error(`未知席位：${seatId}`);
    if (rows.has(seatId)) throw new Error(`席位重复：${seatId}`);
    const action = String(item.action ?? "").trim() as SecretChatChange["action"];
    if (!["keep", "delete", "set"].includes(action)) throw new Error(`席位 ${seatId} action 非法`);
    const hasValue = Object.hasOwn(item, "value");
    const secret = hasValue ? String(item.value ?? "") : undefined;
    if (action === "set") {
      if (!secret || secret.length > MAX_SECRET_CHARS || /[\0\r\n]/.test(secret)) throw new Error(`席位 ${seatId} 新密钥格式非法`);
    } else if (hasValue) throw new Error(`席位 ${seatId} 的 ${action} 操作不得包含 value`);
    rows.set(seatId, { seat_id: seatId, action, ...(action === "set" ? { value: secret as string } : {}) });
  }
  return rows;
}

export class OwnerChatSecretController {
  readonly root: string;
  readonly servers: ServerCatalog;
  readonly targets: SecretTargetCatalog;
  readonly transport: SecretChatTransport;
  private initialized = false;

  constructor(root = process.env.AGENELF_ROOT || process.cwd(), options: {
    servers?: ServerCatalog;
    targets?: SecretTargetCatalog;
    transport?: SecretChatTransport;
  } = {}) {
    this.root = root;
    this.servers = options.servers ?? new ServerCatalog(root);
    this.targets = options.targets ?? new SecretTargetCatalog(root, this.servers);
    this.transport = options.transport ?? new OpenSshTransport(this.servers);
  }

  async initialize(): Promise<void> {
    await this.targets.initialize();
    this.initialized = true;
  }

  private async ready(): Promise<void> {
    if (!this.initialized) await this.initialize();
  }

  async catalog(): Promise<JsonObject> {
    await this.ready();
    return { schema_version: 1, targets: this.targets.list() as unknown as JsonValue };
  }

  async snapshot(targetAlias: string, seatId = ""): Promise<JsonObject> {
    await this.ready();
    const target = this.targets.get(targetAlias);
    if (seatId && (!SEAT_RE.test(seatId) || !target.seats.has(seatId))) throw new Error(`未知席位：${seatId}`);
    const server = this.servers.get(target.serverAlias);
    const remoteDir = `/tmp/agenelf-chat-secret-${nonce()}`;
    const inventoryPath = `${remoteDir}/inventory.py`;
    const revealPath = `${remoteDir}/reveal.py`;
    try {
      const prepared = await this.transport.run(server, `umask 077; mkdir -p ${quoteRemote(remoteDir)}`, 60_000);
      assertOk(prepared, "准备远程明文读取目录");
      assertOk(await this.transport.writeText(server, inventoryPath, INVENTORY_SCRIPT, 60_000), "写入远程清单脚本");
      assertOk(await this.transport.writeText(server, revealPath, REVEAL_SCRIPT, 60_000), "写入远程明文脚本");
      const inventoryResult = await this.transport.run(
        server,
        `python3 ${quoteRemote(inventoryPath)} ${quoteRemote(target.envFile)} ${quoteRemote(seatPayload(target))}`,
        120_000
      );
      assertOk(inventoryResult, "读取远程密钥清单");
      const inventory = parseSecretInventory(inventoryResult.stdout.trim(), target);
      const selected = inventory.seats.filter((row) => !seatId || row.seat_id === seatId);
      const seats: JsonObject[] = [];
      for (const row of selected) {
        let value = "";
        if (row.present) {
          const configured = target.seats.get(row.seat_id);
          if (!configured) throw new Error(`席位配置消失：${row.seat_id}`);
          const revealed = await this.transport.run(
            server,
            `python3 ${quoteRemote(revealPath)} ${quoteRemote(target.envFile)} ${quoteRemote(configured.envName)}`,
            120_000
          );
          assertOk(revealed, `读取席位 ${row.seat_id} 明文`);
          value = revealValue(revealed.stdout.trim());
        }
        seats.push({
          seat_id: row.seat_id,
          label: row.label,
          env_name: row.env_name,
          present: row.present,
          masked: row.masked,
          fingerprint: row.fingerprint,
          fingerprint_sha256: row.fingerprint_sha256,
          value
        });
      }
      return {
        schema_version: 1,
        plaintext: true,
        env_target: target.alias,
        server: target.serverAlias,
        env_file: target.envFile,
        inventory_hash: inventory.inventory_hash,
        seats: seats as unknown as JsonValue
      };
    } finally {
      await this.transport.run(server, `rm -rf ${quoteRemote(remoteDir)}`, 30_000).catch(() => undefined);
    }
  }

  async apply(targetAlias: string, rawChanges: unknown, confirmTarget: string): Promise<JsonObject> {
    await this.ready();
    const target = this.targets.get(targetAlias);
    if (confirmTarget !== target.alias) throw new Error("confirm_target 必须与 env_target 完全一致");
    const requested = normalizeChanges(rawChanges, target);
    const before = await this.snapshot(target.alias) as JsonObject;
    const inventoryHash = String(before.inventory_hash ?? "");
    if (!SHA256_RE.test(inventoryHash) || !Array.isArray(before.seats)) throw new Error("修改前清单非法");
    const beforeBySeat = new Map<string, JsonObject>();
    for (const raw of before.seats) {
      const row = object(raw, "snapshot seat");
      beforeBySeat.set(String(row.seat_id ?? ""), row);
    }
    const mutations: SecretMutation[] = [...target.seats.keys()].map((seatId) => {
      const current = beforeBySeat.get(seatId);
      if (!current) throw new Error(`修改前清单缺少席位：${seatId}`);
      const change = requested.get(seatId) ?? { seat_id: seatId, action: "keep" as const };
      return {
        seat_id: seatId,
        action: change.action,
        expected_fingerprint: String(current.fingerprint_sha256 ?? ""),
        ...(change.action === "set" ? { value: String(change.value) } : {})
      };
    });
    const changed = mutations.filter((item) => item.action !== "keep");
    if (!changed.length) return { schema_version: 1, status: "no_change", env_target: target.alias, inventory_hash: inventoryHash };

    const stage: SecretStage = {
      schema_version: 1,
      env_target: target.alias,
      expected_inventory_hash: inventoryHash,
      mutations,
      created_at: new Date().toISOString()
    };
    const stageText = `${JSON.stringify(stage, null, 2)}\n`;
    const server = this.servers.get(target.serverAlias);
    const remoteDir = `/tmp/agenelf-chat-secret-apply-${nonce()}`;
    const scriptPath = `${remoteDir}/patch.py`;
    const stagePath = `${remoteDir}/stage.json`;
    const backupDir = `${target.envFile}.agenelf-backups`;
    const backupPath = `${backupDir}/chat-${Date.now()}-${nonce()}.env`;
    let retainBackup = false;
    let patchApplied = false;

    const run = (command: string, timeoutMs: number) => this.transport.run(server, command, timeoutMs);
    const backupExists = async (): Promise<boolean> => (await run(`sudo -n test -f ${quoteRemote(backupPath)}`, 30_000)).exit_code === 0;
    const rollback = async (reason: string): Promise<never> => {
      if (!(await backupExists())) throw new Error(`${reason}；远程原子替换未完成，无需回滚`);
      let command = `sudo -n cp ${quoteRemote(backupPath)} ${quoteRemote(target.envFile)} && sudo -n chmod 600 ${quoteRemote(target.envFile)}`;
      if (target.reload.type === "compose") {
        command += ` && cd ${quoteRemote(target.reload.workdir)} && ${server.dockerCommand} compose -p ${quoteRemote(target.reload.project)} -f ${quoteRemote(target.reload.composeFile)} --env-file ${quoteRemote(target.envFile)} up -d --remove-orphans`;
      }
      const outcome = await run(command, 1_200_000);
      if (outcome.exit_code === 0) throw new Error(`${reason}；已自动恢复修改前配置`);
      retainBackup = true;
      throw new Error(`${reason}；自动回滚失败，已保留 0600 恢复备份：${backupPath}`);
    };

    try {
      assertOk(await run(`umask 077; mkdir -p ${quoteRemote(remoteDir)}; sudo -n mkdir -p ${quoteRemote(backupDir)}; sudo -n chmod 700 ${quoteRemote(backupDir)}`, 60_000), "准备远程修改目录");
      assertOk(await this.transport.writeText(server, scriptPath, PATCH_SCRIPT, 120_000), "写入远程原子修改脚本");
      assertOk(await this.transport.writeText(server, stagePath, stageText, 120_000), "传输密钥变更包");
      const applied = await run(
        `sudo -n python3 ${quoteRemote(scriptPath)} ${quoteRemote(target.envFile)} ${quoteRemote(seatPayload(target))} ${quoteRemote(stagePath)} ${quoteRemote(backupPath)}`,
        180_000
      );
      if (applied.exit_code !== 0) return rollback("密钥文件原子修改失败");
      patchApplied = true;
      const summary = patchSummary(applied.stdout.trim());
      if (target.reload.type === "compose") {
        const reload = target.reload;
        const prefix = `cd ${quoteRemote(reload.workdir)} && ${server.dockerCommand} compose -p ${quoteRemote(reload.project)} -f ${quoteRemote(reload.composeFile)} --env-file ${quoteRemote(target.envFile)}`;
        if ((await run(`${prefix} config --quiet`, 180_000)).exit_code !== 0) return rollback("Compose 配置校验失败");
        if ((await run(`${prefix} up -d --remove-orphans`, 1_200_000)).exit_code !== 0) return rollback("服务重载失败");
        if (reload.healthContainer) {
          const container = quoteRemote(reload.healthContainer);
          const health = await run(
            `state=$(${server.dockerCommand} inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' ${container}); printf '%s\\n' "$state"; case "$state" in 'running healthy'|'running ') exit 0;; *) exit 1;; esac`,
            120_000
          );
          if (health.exit_code !== 0) return rollback("健康检查失败");
        }
      }
      return {
        schema_version: 1,
        status: "succeeded",
        env_target: target.alias,
        server: target.serverAlias,
        inventory_hash_before: inventoryHash,
        inventory_hash_after: summary.inventory_hash_after as JsonValue,
        changes: summary.changes as JsonValue,
        rollback_backup_retained: false
      };
    } finally {
      await run(`rm -rf ${quoteRemote(remoteDir)}`, 30_000).catch(() => undefined);
      if (!retainBackup && (patchApplied || await backupExists().catch(() => false))) {
        await run(`sudo -n rm -f ${quoteRemote(backupPath)}`, 30_000).catch(() => undefined);
      }
    }
  }
}
