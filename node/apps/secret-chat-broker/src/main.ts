import { timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { appendLine } from "../../../packages/core/src/fs-store.ts";
import { sanitizeRemoteText } from "../../../packages/core/src/open-ssh.ts";
import { OwnerChatSecretController } from "../../../packages/core/src/chat-secret-env.ts";
import type { JsonObject, JsonValue } from "../../../packages/core/src/types.ts";

const MAX_BODY = 256 * 1024;
const MAX_RESPONSE = 1024 * 1024;

export interface SecretChatService {
  initialize(): Promise<void>;
  catalog(): Promise<JsonObject>;
  snapshot(targetAlias: string, seatId?: string): Promise<JsonObject>;
  apply(targetAlias: string, changes: unknown, confirmTarget: string): Promise<JsonObject>;
}

function tokenMatches(expected: string, actual: string): boolean {
  const left = Buffer.from(expected, "utf8");
  const right = Buffer.from(actual, "utf8");
  return left.length === right.length && left.length > 0 && timingSafeEqual(left, right);
}

async function readBody(request: IncomingMessage): Promise<JsonObject> {
  let text = "";
  let bytes = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    bytes += buffer.length;
    if (bytes > MAX_BODY) throw new Error("请求体超过 256 KiB 上限");
    text += buffer.toString("utf8");
  }
  if (!text.trim()) return {};
  const value = JSON.parse(text) as JsonValue;
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("请求体必须是 JSON object");
  return value as JsonObject;
}

function send(response: ServerResponse, status: number, payload: JsonObject): void {
  const body = JSON.stringify(payload);
  if (Buffer.byteLength(body, "utf8") > MAX_RESPONSE) {
    response.writeHead(500, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
    response.end(JSON.stringify({ error: "响应超过安全上限" }));
    return;
  }
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff"
  });
  response.end(body);
}

function plaintextEnabled(): boolean {
  const value = String(process.env.AGENELF_CHAT_PLAINTEXT_SECRETS ?? "true").trim().toLowerCase();
  return !["0", "false", "off", "no"].includes(value);
}

export function createSecretChatServer(service: SecretChatService, options: {
  token: string;
  auditPath?: string;
}): ReturnType<typeof createServer> {
  const token = options.token;
  if (!token) throw new Error("AGENELF_API_TOKEN 不能为空");
  const auditPath = options.auditPath || resolve(process.env.AGENELF_ROOT || process.cwd(), "logs", "secret-chat-broker.log");
  const audit = (event: string, detail: string) => appendLine(auditPath, `[${new Date().toISOString()}] [${event}] ${sanitizeRemoteText(detail)}\n`);

  return createServer(async (request, response) => {
    const path = new URL(request.url || "/", "http://secret-chat-broker").pathname;
    if (request.method === "GET" && path === "/health") {
      send(response, 200, { status: "ok", runtime: "node-typescript", plaintext_enabled: plaintextEnabled() });
      return;
    }
    const supplied = String(request.headers["x-agenelf-token"] ?? "");
    if (!tokenMatches(token, supplied)) {
      await audit("unauthorized", `${request.socket.remoteAddress || "unknown"} ${request.method || ""} ${path}`).catch(() => undefined);
      send(response, 401, { error: "unauthorized" });
      return;
    }
    if (!plaintextEnabled()) {
      send(response, 403, { error: "主人聊天明文密钥模式未启用" });
      return;
    }
    try {
      if (request.method === "GET" && path === "/v1/targets") {
        send(response, 200, await service.catalog());
        return;
      }
      if (request.method === "POST" && path === "/v1/snapshot") {
        const body = await readBody(request);
        const target = String(body.env_target ?? "").trim();
        const seatId = String(body.seat_id ?? "").trim();
        const result = await service.snapshot(target, seatId);
        await audit("plaintext_revealed", `${target}${seatId ? `/${seatId}` : "/all"}`);
        send(response, 200, result);
        return;
      }
      if (request.method === "POST" && path === "/v1/apply") {
        const body = await readBody(request);
        const target = String(body.env_target ?? "").trim();
        const result = await service.apply(target, body.changes, String(body.confirm_target ?? "").trim());
        const actions = Array.isArray(body.changes)
          ? body.changes.map((raw) => raw && typeof raw === "object" && !Array.isArray(raw)
            ? `${String((raw as JsonObject).seat_id ?? "")}:${String((raw as JsonObject).action ?? "")}`
            : "invalid").join(",")
          : "invalid";
        await audit("plaintext_applied", `${target} ${actions} status=${String(result.status ?? "unknown")}`);
        send(response, 200, result);
        return;
      }
      send(response, 404, { error: "not_found" });
    } catch (error) {
      const message = sanitizeRemoteText(error instanceof Error ? `${error.name}: ${error.message}` : String(error));
      await audit("failed", `${request.method || ""} ${path} ${message}`).catch(() => undefined);
      send(response, 400, { error: message });
    }
  });
}

export async function runSecretChatBroker(root = process.env.AGENELF_ROOT || process.cwd()): Promise<void> {
  const controller = new OwnerChatSecretController(root);
  await controller.initialize();
  const host = String(process.env.AGENELF_SECRET_CHAT_HOST ?? "0.0.0.0");
  const port = Math.max(1, Math.min(Number(process.env.AGENELF_SECRET_CHAT_PORT ?? 8097), 65_535));
  const server = createSecretChatServer(controller, { token: String(process.env.AGENELF_API_TOKEN ?? "") });
  await new Promise<void>((resolvePromise, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => resolvePromise());
  });
  console.log(`Secret Chat Broker listening on ${host}:${port}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  runSecretChatBroker().catch((error) => {
    console.error(`Secret Chat Broker 启动失败：${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  });
}
