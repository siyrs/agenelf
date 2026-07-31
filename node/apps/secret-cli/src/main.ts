import { randomBytes } from "node:crypto";
import { mkdir, readdir, rename, rm, stat, writeFile, chmod } from "node:fs/promises";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { pathToFileURL } from "node:url";
import { OperationQueue } from "../../../packages/core/src/operation-queue.ts";
import { OpenSshTransport, quoteRemote } from "../../../packages/core/src/open-ssh.ts";
import {
  INVENTORY_SCRIPT,
  REVEAL_SCRIPT,
  parseSecretInventory,
  rawSha256,
  type SecretInventory,
  type SecretMutation,
  type SecretStage
} from "../../../packages/core/src/secret-env.ts";
import { SecretTargetCatalog, type ManagedSecretTarget } from "../../../packages/core/src/secret-targets.ts";
import { ServerCatalog } from "../../../packages/core/src/server-catalog.ts";
import type { JsonObject } from "../../../packages/core/src/types.ts";

function nonce(): string { return randomBytes(12).toString("hex"); }

function usage(): string {
  return [
    "Agenelf Owner Secret Console（本进程不调用模型）",
    "",
    "用法：",
    "  make secret ARGS='targets'",
    "  make secret ARGS='list <env-target>'",
    "  make secret ARGS='reveal <env-target> <seat-id>'",
    "  make secret ARGS='patch <env-target>'",
    "  make secret ARGS='status <op-id>'",
    "  make secret ARGS='cleanup'",
    "  make secret ARGS='cleanup --all'",
    "",
    "完整密钥只在此本地 TTY 中显示或输入，不进入 Agent 对话、模型上下文、操作请求或审计日志。"
  ].join("\n");
}

async function hiddenInput(prompt: string): Promise<string> {
  if (!input.isTTY) throw new Error("安全输入需要交互式 TTY");
  const script = [
    "set -eu",
    "old=$(stty -g < /dev/tty)",
    "trap 'stty \"$old\" < /dev/tty' EXIT HUP INT TERM",
    "printf '%s' \"$AGENELF_SECRET_PROMPT\" > /dev/tty",
    "stty -echo < /dev/tty",
    "IFS= read -r value < /dev/tty",
    "stty \"$old\" < /dev/tty",
    "trap - EXIT HUP INT TERM",
    "printf '\\n' > /dev/tty",
    "printf '%s' \"$value\""
  ].join("\n");
  return new Promise<string>((resolvePromise, reject) => {
    const child = spawn("/bin/sh", ["-c", script], {
      env: { ...process.env, AGENELF_SECRET_PROMPT: prompt },
      stdio: ["ignore", "pipe", "inherit"]
    });
    let value = "";
    child.stdout.on("data", (chunk: Buffer) => { value += chunk.toString("utf8"); });
    child.once("error", reject);
    child.once("close", (code) => code === 0 ? resolvePromise(value) : reject(new Error(`安全输入失败：exit ${code}`)));
  });
}

function seatsJson(target: ManagedSecretTarget): string {
  return JSON.stringify([...target.seats.values()].map((seat) => ({ seat_id: seat.id, env_name: seat.envName })));
}

async function runRemoteScript(
  transport: OpenSshTransport,
  target: ManagedSecretTarget,
  servers: ServerCatalog,
  script: string,
  args: string[],
  label: string
): Promise<string> {
  const server = servers.get(target.serverAlias);
  const remoteDir = `/tmp/agenelf-owner-secret-${nonce()}`;
  const scriptPath = `${remoteDir}/${label}.py`;
  const prepared = await transport.run(server, `umask 077; mkdir -p ${quoteRemote(remoteDir)}`, 60_000);
  if (prepared.exit_code !== 0) throw new Error("无法创建远程临时目录");
  try {
    const written = await transport.writeText(server, scriptPath, script, 60_000);
    if (written.exit_code !== 0) throw new Error("无法写入远程固定脚本");
    const command = `python3 ${quoteRemote(scriptPath)} ${args.map(quoteRemote).join(" ")}`;
    const result = await transport.run(server, command, 120_000);
    if (result.exit_code !== 0) throw new Error(`${label} 失败；远程输出已被安全抑制`);
    return result.stdout.trim();
  } finally {
    await transport.run(server, `rm -rf ${quoteRemote(remoteDir)}`, 30_000).catch(() => undefined);
  }
}

async function inventory(
  transport: OpenSshTransport,
  target: ManagedSecretTarget,
  servers: ServerCatalog
): Promise<SecretInventory> {
  const text = await runRemoteScript(transport, target, servers, INVENTORY_SCRIPT, [target.envFile, seatsJson(target)], "inventory");
  return parseSecretInventory(text, target);
}

async function reveal(
  transport: OpenSshTransport,
  target: ManagedSecretTarget,
  servers: ServerCatalog,
  seatId: string
): Promise<string> {
  const seat = target.seats.get(seatId);
  if (!seat) throw new Error(`未知席位：${seatId}`);
  const text = await runRemoteScript(transport, target, servers, REVEAL_SCRIPT, [target.envFile, seat.envName], "reveal");
  let document: JsonObject;
  try { document = JSON.parse(text) as JsonObject; }
  catch { throw new Error("远程 reveal 结果不是有效 JSON"); }
  if (Number(document.schema_version) !== 1 || typeof document.value_b64 !== "string") throw new Error("远程 reveal 结果非法");
  const value = Buffer.from(document.value_b64, "base64").toString("utf8");
  if (!value || /[\0\r\n\x1b]/.test(value)) throw new Error("密钥包含不适合终端展示的控制字符");
  return value;
}

function printInventory(target: ManagedSecretTarget, data: SecretInventory): void {
  console.table(data.seats.map((seat) => ({
    id: seat.seat_id,
    label: seat.label,
    env: seat.env_name,
    status: seat.present ? "present" : "missing",
    masked: seat.masked,
    fingerprint: seat.fingerprint
  })));
  console.log(`inventory_hash: ${data.inventory_hash}`);
  console.log(`target: ${target.alias} · server: ${target.serverAlias} · file: ${target.envFile}`);
}

function stagingDirectory(root: string): string {
  return resolve(process.env.AGENELF_SECRET_STAGING_DIR || join(root, "local", "secret-staging"));
}

async function writeStage(root: string, stage: SecretStage): Promise<{ ref: string; sha256: string }> {
  const directory = stagingDirectory(root);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await chmod(directory, 0o700);
  const ref = `secret-stage-${nonce()}.json`;
  const finalPath = join(directory, ref);
  const temporary = `${finalPath}.tmp-${process.pid}`;
  const text = `${JSON.stringify(stage, null, 2)}\n`;
  await writeFile(temporary, text, { mode: 0o600, flag: "wx" });
  await chmod(temporary, 0o600);
  await rename(temporary, finalPath);
  return { ref, sha256: rawSha256(text) };
}

async function patchInteractive(
  root: string,
  target: ManagedSecretTarget,
  data: SecretInventory,
  operations: OperationQueue
): Promise<void> {
  printInventory(target, data);
  const rl = createInterface({ input, output });
  const mutations: SecretMutation[] = [];
  try {
    for (const row of data.seats) {
      const answer = (await rl.question(`${row.seat_id} [k=保持/d=删除/u=更新，默认 k] > `)).trim().toLowerCase();
      const action = answer === "d" || answer === "delete" ? "delete" : answer === "u" || answer === "update" ? "set" : "keep";
      if (action === "set") {
        const first = await hiddenInput(`输入 ${row.seat_id} 新密钥 > `);
        const second = await hiddenInput(`再次输入 ${row.seat_id} 新密钥 > `);
        if (!first || first !== second) throw new Error(`${row.seat_id} 两次输入不一致`);
        if (first.length > 32 * 1024 || /[\0\r\n]/.test(first)) throw new Error(`${row.seat_id} 新密钥格式非法`);
        mutations.push({ seat_id: row.seat_id, action, expected_fingerprint: row.fingerprint_sha256, value: first });
      } else {
        mutations.push({ seat_id: row.seat_id, action, expected_fingerprint: row.fingerprint_sha256 });
      }
    }
    const changed = mutations.filter((mutation) => mutation.action !== "keep");
    if (!changed.length) {
      console.log("没有选择任何变更，未创建请求。");
      return;
    }
    console.table(mutations.map((mutation) => ({
      seat: mutation.seat_id,
      action: mutation.action,
      expected: mutation.expected_fingerprint ? mutation.expected_fingerprint.slice(0, 12).toUpperCase() : "missing"
    })));
    const confirmed = (await rl.question("确认创建上述精确变更请求？输入 YES > ")).trim();
    if (confirmed !== "YES") {
      console.log("已取消，未写入 staging，也未创建操作请求。");
      return;
    }
    const stage: SecretStage = {
      schema_version: 1,
      env_target: target.alias,
      expected_inventory_hash: data.inventory_hash,
      mutations,
      created_at: new Date().toISOString()
    };
    const staged = await writeStage(root, stage);
    try {
      const request = await operations.submit({
        capability: "server.secrets",
        operation: "patch_env",
        target: target.serverAlias,
        parameters: {
          env_target: target.alias,
          stage_ref: staged.ref,
          stage_sha256: staged.sha256,
          expected_inventory_hash: data.inventory_hash
        },
        risk: "change",
        summary: `Patch managed env secrets for ${target.alias}: ${changed.map((item) => `${item.seat_id}:${item.action}`).join(", ")}`,
        ttlSeconds: 1800
      });
      console.log("\n已创建精确绑定的变更请求：");
      console.log(JSON.stringify({
        operation_id: request.id,
        target: target.alias,
        server: target.serverAlias,
        actions: mutations.map((item) => ({ seat_id: item.seat_id, action: item.action })),
        request_fingerprint: request.fingerprint,
        stage_sha256: staged.sha256,
        expires_at: request.expires_at
      }, null, 2));
      console.log(`\n批准方式：/approve ${request.id} env-secret-patch`);
      console.log(`或宿主机执行：make approve REQ=${request.id}`);
      console.log("完整密钥未写入操作请求、聊天记录或审计日志；staging 会在成功、拒绝、过期或失败后由 Secret Ops Runner 删除。");
    } catch (error) {
      await rm(join(stagingDirectory(root), staged.ref), { force: true });
      throw error;
    }
  } finally {
    rl.close();
  }
}

async function cleanupStaging(root: string, removeAll = false): Promise<void> {
  const directory = stagingDirectory(root);
  let names: string[] = [];
  try { names = await readdir(directory); } catch { return; }
  const cutoff = Date.now() - 24 * 60 * 60 * 1_000;
  let removed = 0;
  for (const name of names) {
    if (!/^secret-stage-[0-9a-f]{24}\.json$/.test(name)) continue;
    const path = join(directory, name);
    const info = await stat(path).catch(() => null);
    if (info && (removeAll || info.mtimeMs < cutoff)) {
      await rm(path, { force: true });
      removed += 1;
    }
  }
  console.log(removeAll
    ? `已清理 ${removed} 个 staging 文件。`
    : `已清理 ${removed} 个超过 24 小时的 staging 文件。`);
}

export async function runSecretCli(root = process.env.AGENELF_ROOT || process.cwd(), argv = process.argv.slice(2)): Promise<void> {
  const resolvedRoot = resolve(root);
  const servers = new ServerCatalog(resolvedRoot);
  const targets = new SecretTargetCatalog(resolvedRoot, servers);
  await targets.initialize();
  const transport = new OpenSshTransport(servers);
  const operations = new OperationQueue(resolvedRoot);
  const [command = "help", first = "", second = ""] = argv;

  if (command === "help" || command === "--help" || command === "-h") { console.log(usage()); return; }
  if (command === "targets") { console.table(targets.list()); return; }
  if (command === "cleanup") {
    if (first && first !== "--all") throw new Error("cleanup 只接受可选参数 --all");
    await cleanupStaging(resolvedRoot, first === "--all");
    return;
  }
  if (command === "status") {
    if (!/^op-[0-9a-f]{16}$/.test(first)) throw new Error("status 需要合法 op-id");
    console.log(JSON.stringify(await operations.get(first), null, 2));
    return;
  }
  const target = targets.get(first);
  if (command === "list") {
    printInventory(target, await inventory(transport, target, servers));
    return;
  }
  if (command === "reveal") {
    if (!second) throw new Error("reveal 需要 seat-id");
    const value = await reveal(transport, target, servers, second);
    console.log(`\n${target.alias}/${second} 完整密钥（仅本地 TTY）：\n${value}\n`);
    console.log("提示：终端滚动区可能保留显示内容；该值未发送给 Agent 或模型。\n");
    return;
  }
  if (command === "patch") {
    await patchInteractive(resolvedRoot, target, await inventory(transport, target, servers), operations);
    return;
  }
  throw new Error(`未知命令：${command}\n\n${usage()}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  runSecretCli().catch((error) => {
    console.error(`Secret Console 失败：${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  });
}
