import type { JsonObject, JsonValue } from "./types.ts";

const SECRET_NOUN = /(?:api[ _-]?key|key|密钥|密码|口令|token|secret|credential)/i;
const PLAINTEXT_WORD = /(?:明文|完整(?:的)?(?:\s*(?:api[ _-]?key|key|密钥|密码|口令|token))?)/i;
const REVEAL_VERB = /(?:显示|展示|查看|看看|列出|读取|给我|看下|看一下|输出)/i;
const CHANGE_VERB = /(?:删除|移除|去掉|停用|修改|更改|更新|替换|设置|设为|改成|改为)/i;
const TARGETS_VERB = /(?:目标|配置项|席位).{0,20}(?:有哪些|列表|列出|查看)|(?:列出|查看).{0,20}(?:目标|配置项|席位)/i;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const MAX_SECRET_CHARS = 32 * 1024;

const PROVIDER_TERMS: ReadonlyArray<readonly [string[], string[]]> = [
  [["智谱", "glm"], ["zhipu", "glm"]],
  [["openai", "gpt", "codex"], ["openai", "gpt", "codex"]],
  [["Claude", "克劳德", "Anthropic", "Opus"], ["claude", "anthropic", "opus"]],
  [["DeepSeek", "深度求索"], ["deepseek"]],
  [["通义", "千问", "Qwen"], ["qwen", "tongyi"]],
  [["Kimi", "月之暗面"], ["kimi", "moonshot"]],
  [["MiniMax"], ["minimax"]]
];

export interface DirectSecretChatClient {
  readonly enabled: boolean;
  targets(): Promise<JsonObject>;
  snapshot(envTarget: string, seatId?: string): Promise<JsonObject>;
  apply(envTarget: string, changes: JsonValue[], confirmTarget: string): Promise<JsonObject>;
}

export interface DirectSecretRouteResult {
  handled: boolean;
  sensitive: boolean;
  reply?: string;
  route?: "targets" | "reveal" | "apply" | "diagnostic";
}

interface SeatRecord {
  id: string;
  label: string;
  envName: string;
}

interface TargetRecord {
  alias: string;
  label: string;
  aliases: string[];
  server: string;
  envFile: string;
  seats: SeatRecord[];
}

interface ParsedChange {
  seat_id: string;
  action: "delete" | "set";
  value?: string;
}

function normalize(value: unknown): string {
  return String(value ?? "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[\s`'"“”‘’，。；;：:（）()\[\]{}<>《》/\\|_-]+/g, "");
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function asObject(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : null;
}

function textArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? "").trim()).filter(Boolean).slice(0, 32);
}

function parseCatalog(value: JsonObject): TargetRecord[] {
  if (!Array.isArray(value.targets)) throw new Error("Secret Chat Broker 目标目录格式非法");
  const records: TargetRecord[] = [];
  for (const raw of value.targets) {
    const item = asObject(raw);
    if (!item) continue;
    const alias = String(item.alias ?? "").trim();
    if (!SAFE_ID.test(alias)) continue;
    const seats: SeatRecord[] = [];
    if (Array.isArray(item.seats)) {
      for (const rawSeat of item.seats) {
        const seat = asObject(rawSeat);
        if (!seat) continue;
        const id = String(seat.id ?? seat.seat_id ?? "").trim();
        if (!SAFE_ID.test(id)) continue;
        seats.push({
          id,
          label: String(seat.label ?? id).trim().slice(0, 128) || id,
          envName: String(seat.env_name ?? seat.env ?? "").trim().slice(0, 128)
        });
      }
    }
    records.push({
      alias,
      label: String(item.label ?? alias).trim().slice(0, 128) || alias,
      aliases: textArray(item.aliases),
      server: String(item.server ?? "").trim().slice(0, 128),
      envFile: String(item.env_file ?? "").trim().slice(0, 512),
      seats
    });
  }
  return records;
}

function isSlashSecret(text: string): boolean {
  return /^\/secret(?:\s|$)/i.test(text.trim());
}

function isTargetsIntent(text: string): boolean {
  const trimmed = text.trim();
  return /^\/secret\s+targets?\s*$/i.test(trimmed)
    || (SECRET_NOUN.test(trimmed) && TARGETS_VERB.test(trimmed) && !PLAINTEXT_WORD.test(trimmed));
}

function isRevealIntent(text: string): boolean {
  const trimmed = text.trim();
  if (/^\/secret\s+(?:show|get|reveal)\b/i.test(trimmed)) return true;
  return SECRET_NOUN.test(trimmed) && PLAINTEXT_WORD.test(trimmed) && REVEAL_VERB.test(trimmed);
}

function isApplyIntent(text: string): boolean {
  const trimmed = text.trim();
  if (/^\/secret\s+(?:set|delete|patch|apply)\b/i.test(trimmed)) return true;
  return CHANGE_VERB.test(trimmed) && (SECRET_NOUN.test(trimmed) || /[A-Za-z0-9][A-Za-z0-9._-]{1,63}/.test(trimmed));
}

export function isDirectSecretChatIntent(text: string): boolean {
  return isSlashSecret(text) || isTargetsIntent(text) || isRevealIntent(text) || isApplyIntent(text);
}

function targetSearchText(target: TargetRecord): string {
  return normalize([
    target.alias,
    target.label,
    ...target.aliases,
    target.server,
    target.envFile,
    ...target.seats.flatMap((seat) => [seat.id, seat.label, seat.envName])
  ].join(" "));
}

function targetScore(text: string, target: TargetRecord): number {
  const normalizedText = normalize(text);
  const source = targetSearchText(target);
  let score = 0;
  for (const [value, weight] of [
    [target.alias, 140],
    [target.label, 130],
    [target.server, 90],
    ...target.aliases.map((item) => [item, 120] as [string, number])
  ] as Array<[string, number]>) {
    const normalizedValue = normalize(value);
    if (normalizedValue && normalizedText.includes(normalizedValue)) score += weight;
  }
  for (const seat of target.seats) {
    for (const value of [seat.id, seat.label, seat.envName]) {
      const normalizedValue = normalize(value);
      if (normalizedValue && normalizedText.includes(normalizedValue)) score += 25;
    }
  }
  for (const [userTerms, targetTerms] of PROVIDER_TERMS) {
    if (userTerms.some((term) => normalizedText.includes(normalize(term)))
      && targetTerms.some((term) => source.includes(normalize(term)))) score += 70;
  }
  const count = text.match(/(\d{1,2})\s*个/);
  if (count && Number(count[1]) === target.seats.length) score += 35;
  return score;
}

function resolveTarget(text: string, targets: TargetRecord[]): { target?: TargetRecord; message?: string } {
  if (!targets.length) return { message: "当前没有配置可管理的 .env 密钥目标。请先检查 local/env-secrets.yaml。" };
  if (targets.length === 1) return { target: targets[0] };
  const ranked = targets
    .map((target) => ({ target, score: targetScore(text, target) }))
    .sort((left, right) => right.score - left.score || left.target.alias.localeCompare(right.target.alias));
  const first = ranked[0];
  const second = ranked[1];
  if (first.score > 0 && first.score > second.score) return { target: first.target };
  return {
    message: [
      "我已识别到明文密钥请求，但无法唯一确定目标。请在消息中写出下面的 alias 或中文 label：",
      ...targets.map((target) => `- ${target.alias}（${target.label}，${target.seats.length} 个席位，服务器 ${target.server}）`),
      "也可以使用：/secret show <target-alias> all"
    ].join("\n")
  };
}

function slashArguments(text: string): string[] {
  const match = text.trim().match(/^\/secret\s+(.+)$/is);
  return match ? match[1].trim().split(/\s+/) : [];
}

function explicitSlashTarget(text: string): string {
  const args = slashArguments(text);
  if (!args.length) return "";
  const command = args[0].toLowerCase();
  if (["show", "get", "reveal", "set", "delete", "patch", "apply"].includes(command)) return args[1] ?? "";
  return "";
}

function resolveTargetWithSlash(text: string, targets: TargetRecord[]): { target?: TargetRecord; message?: string } {
  const explicit = explicitSlashTarget(text);
  if (explicit) {
    const exact = targets.find((target) => target.alias === explicit || target.label === explicit || target.aliases.includes(explicit));
    if (exact) return { target: exact };
    return { message: `未知密钥目标：${explicit}\n可用目标：${targets.map((target) => target.alias).join(", ")}` };
  }
  return resolveTarget(text, targets);
}

function resolveSeat(text: string, target: TargetRecord): string {
  const args = slashArguments(text);
  if (args.length >= 3 && ["show", "get", "reveal"].includes(args[0].toLowerCase())) {
    const requested = args[2];
    if (["all", "全部", "所有"].includes(requested.toLowerCase())) return "";
    const exact = target.seats.find((seat) => seat.id === requested || seat.label === requested);
    if (!exact) throw new Error(`未知席位：${requested}`);
    return exact.id;
  }
  const normalizedText = normalize(text);
  const matched = target.seats.filter((seat) => [seat.id, seat.label, seat.envName]
    .map(normalize)
    .some((value) => value && normalizedText.includes(value)));
  return matched.length === 1 ? matched[0].id : "";
}

function formatTargets(targets: TargetRecord[]): string {
  if (!targets.length) return "当前没有配置可管理的 .env 密钥目标。";
  return [
    "当前可在聊天中管理的密钥目标：",
    ...targets.map((target) => [
      `- ${target.alias}（${target.label}）`,
      `  服务器：${target.server}`,
      `  文件：${target.envFile}`,
      `  席位：${target.seats.map((seat) => `${seat.id}[${seat.label}]`).join("、") || "无"}`
    ].join("\n"))
  ].join("\n");
}

function formatSnapshot(value: JsonObject): string {
  const target = String(value.env_target ?? "");
  const server = String(value.server ?? "");
  const envFile = String(value.env_file ?? "");
  if (!Array.isArray(value.seats)) throw new Error("Broker 明文快照缺少 seats");
  const rows = value.seats.map((raw) => {
    const seat = asObject(raw);
    if (!seat) return "";
    const id = String(seat.seat_id ?? "");
    const label = String(seat.label ?? id);
    const envName = String(seat.env_name ?? "");
    const present = seat.present === true;
    const plaintext = present ? String(seat.value ?? "") : "<未配置>";
    return [
      `【${id}】${label}`,
      envName ? `${envName}=${plaintext}` : plaintext
    ].join("\n");
  }).filter(Boolean);
  return [
    `已直接从 ${target}${server ? ` / ${server}` : ""} 读取 ${rows.length} 个席位的完整明文：`,
    envFile ? `文件：${envFile}` : "",
    "",
    ...rows.map((row) => `${row}\n`),
    "以上内容由确定性 Secret Chat 路由直接返回，没有交给模型判断是否允许。"
  ].filter(Boolean).join("\n");
}

function validSecret(value: string): boolean {
  return Boolean(value)
    && value.length <= MAX_SECRET_CHARS
    && !/[\0\r\n]/.test(value)
    && !/^(?:<.*>|新的?key|新密钥|这里粘贴|下面这个|保持不动)$/i.test(value);
}

function changeTerms(seat: SeatRecord): string[] {
  return [seat.id, seat.label, seat.envName]
    .map((item) => item.trim())
    .filter((item, index, all) => item && all.indexOf(item) === index)
    .sort((left, right) => right.length - left.length);
}

function parseSlashChanges(text: string, target: TargetRecord): ParsedChange[] | null {
  const args = slashArguments(text);
  if (!args.length) return null;
  const command = args[0].toLowerCase();
  if (command === "delete" && args.length >= 3) {
    const seat = target.seats.find((item) => item.id === args[2] || item.label === args[2]);
    if (!seat) throw new Error(`未知席位：${args[2]}`);
    return [{ seat_id: seat.id, action: "delete" }];
  }
  if (command === "set" && args.length >= 4) {
    const seat = target.seats.find((item) => item.id === args[2] || item.label === args[2]);
    if (!seat) throw new Error(`未知席位：${args[2]}`);
    const value = args.slice(3).join(" ");
    if (!validSecret(value)) throw new Error(`${seat.id} 的新密钥格式非法`);
    return [{ seat_id: seat.id, action: "set", value }];
  }
  if (["patch", "apply"].includes(command)) return [];
  return null;
}

function parseNaturalChanges(text: string, target: TargetRecord): ParsedChange[] {
  const rows: ParsedChange[] = [];
  for (const seat of target.seats) {
    let deleteMatched = false;
    let setValue = "";
    for (const term of changeTerms(seat)) {
      const escaped = escapeRegExp(term);
      const deletePatterns = [
        new RegExp(`(?:删除|移除|去掉|停用)[^\\n。；;]{0,30}${escaped}`, "i"),
        new RegExp(`${escaped}[^\\n。；;]{0,30}(?:删除|移除|去掉|停用)`, "i")
      ];
      if (deletePatterns.some((pattern) => pattern.test(text))) deleteMatched = true;
      const setPatterns = [
        new RegExp(`(?:把|将)?\\s*${escaped}\\s*(?:的\\s*(?:api[ _-]?key|key|密钥|token)?\\s*)?(?:改成|改为|更新为|替换为|设置为|设为)\\s*(?:下面(?:这个)?(?:完整)?(?:api[ _-]?key|key|密钥)?\\s*[:：]?\\s*)?([^\\s，。；;]+)`, "i"),
        new RegExp(`${escaped}\\s*(?:=|=>|:|：)\\s*([^\\s，。；;]+)`, "i")
      ];
      for (const pattern of setPatterns) {
        const match = text.match(pattern);
        if (match && validSecret(match[1])) {
          setValue = match[1];
          break;
        }
      }
      if (setValue) break;
    }
    if (deleteMatched && setValue) throw new Error(`席位 ${seat.id} 同时要求删除和更新，请只保留一个动作`);
    if (setValue) rows.push({ seat_id: seat.id, action: "set", value: setValue });
    else if (deleteMatched) rows.push({ seat_id: seat.id, action: "delete" });
  }
  return rows;
}

function parseChanges(text: string, target: TargetRecord): ParsedChange[] {
  const slash = parseSlashChanges(text, target);
  if (slash && slash.length) return slash;
  return parseNaturalChanges(text, target);
}

function formatApply(value: JsonObject): string {
  const status = String(value.status ?? "unknown");
  const target = String(value.env_target ?? "");
  if (status === "no_change") return `${target} 没有需要执行的变更。`;
  if (!Array.isArray(value.changes)) return `${target} 密钥更新已返回状态：${status}`;
  const changes = value.changes
    .map((raw) => asObject(raw))
    .filter((item): item is JsonObject => Boolean(item))
    .map((item) => `- ${String(item.seat_id ?? "")}: ${String(item.action ?? "")}（${String(item.old_fingerprint ?? "-")} → ${String(item.new_fingerprint ?? "-")}）`);
  return [
    `${target} 明文密钥更新结果：${status}`,
    ...changes,
    value.rollback_backup_retained === true ? `自动回滚失败，恢复备份：${String(value.recovery_backup_path ?? "请查看 Broker 日志")}` : "",
    "未列出的席位保持不动；新密钥没有出现在更新结果或 SSH 命令参数中。"
  ].filter(Boolean).join("\n");
}

function applySyntax(target: TargetRecord): string {
  const sample = target.seats.slice(0, 2);
  return [
    `已识别目标 ${target.alias}，但没有解析到可执行的席位变更。请使用以下任一种格式：`,
    sample[0] ? `删除 ${sample[0].id}` : "删除 <seat-id>",
    sample[1] ? `设置 ${sample[1].id}=<新的完整 Key>` : "设置 <seat-id>=<新的完整 Key>",
    "其他席位不写即自动保持不动。",
    `也可以使用：/secret set ${target.alias} <seat-id> <新 Key>`
  ].join("\n");
}

function safeError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/[\0\r\n]+/g, " ").slice(0, 1000);
}

export async function routeOwnerSecretChat(
  text: string,
  client: DirectSecretChatClient
): Promise<DirectSecretRouteResult> {
  const request = String(text ?? "").trim();
  if (!request || !isDirectSecretChatIntent(request)) return { handled: false, sensitive: false };
  if (!client.enabled) {
    return {
      handled: true,
      sensitive: true,
      route: "diagnostic",
      reply: "已识别到主人明文密钥请求，但 AGENELF_CHAT_PLAINTEXT_SECRETS 当前被关闭。请设置为 true，并重新创建 agenelf 与 secret-chat-broker 容器。"
    };
  }

  let targets: TargetRecord[];
  try {
    targets = parseCatalog(await client.targets());
  } catch (error) {
    return {
      handled: true,
      sensitive: true,
      route: "diagnostic",
      reply: `已识别到主人明文密钥请求，但 Secret Chat Broker 不可用：${safeError(error)}\n请确认 secret-chat-broker 已启动，并重新构建 agenelf 容器。`
    };
  }

  if (isTargetsIntent(request) || /^\/secret\s+targets?\s*$/i.test(request)) {
    return { handled: true, sensitive: false, route: "targets", reply: formatTargets(targets) };
  }

  const resolved = resolveTargetWithSlash(request, targets);
  if (!resolved.target) return { handled: true, sensitive: true, route: "diagnostic", reply: resolved.message ?? "无法确定密钥目标" };
  const target = resolved.target;

  if (isRevealIntent(request)) {
    try {
      const seatId = resolveSeat(request, target);
      return {
        handled: true,
        sensitive: true,
        route: "reveal",
        reply: formatSnapshot(await client.snapshot(target.alias, seatId))
      };
    } catch (error) {
      return { handled: true, sensitive: true, route: "diagnostic", reply: `明文读取失败：${safeError(error)}` };
    }
  }

  if (isApplyIntent(request)) {
    try {
      const changes = parseChanges(request, target);
      if (!changes.length) return { handled: true, sensitive: true, route: "diagnostic", reply: applySyntax(target) };
      const payload = changes.map((change) => ({
        seat_id: change.seat_id,
        action: change.action,
        ...(change.action === "set" ? { value: String(change.value) } : {})
      })) as JsonValue[];
      return {
        handled: true,
        sensitive: true,
        route: "apply",
        reply: formatApply(await client.apply(target.alias, payload, target.alias))
      };
    } catch (error) {
      return { handled: true, sensitive: true, route: "diagnostic", reply: `明文更新失败：${safeError(error)}` };
    }
  }

  return { handled: false, sensitive: false };
}
