import { timingSafeEqual } from "node:crypto";
import { createReadStream } from "node:fs";
import { lstat } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { AgenelfAgent } from "../../../packages/core/src/agent.ts";
import { EventCursorExpired } from "../../../packages/core/src/agent-events.ts";
import type { JsonObject } from "../../../packages/core/src/types.ts";

const MAX_BODY_BYTES = 1024 * 1024;
const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"
};

function rootDir(): string { return resolve(process.env.AGENELF_ROOT || process.cwd()); }
function sendJson(response: ServerResponse, status: number, value: unknown): void {
  const body = JSON.stringify(value);
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(body) });
  response.end(body);
}
function securityHeaders(response: ServerResponse): void {
  response.setHeader("x-content-type-options", "nosniff");
  response.setHeader("x-frame-options", "DENY");
  response.setHeader("referrer-policy", "no-referrer");
  response.setHeader("content-security-policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'");
}
function authorized(request: IncomingMessage): boolean {
  const expected = String(process.env.AGENELF_API_TOKEN || "").trim();
  if (!expected) return process.env.AGENELF_API_ALLOW_INSECURE === "1";
  const actual = String(request.headers["x-agenelf-token"] || "");
  const expectedBytes = Buffer.from(expected);
  const actualBytes = Buffer.from(actual);
  return expectedBytes.length === actualBytes.length && timingSafeEqual(expectedBytes, actualBytes);
}
async function readJsonBody(request: IncomingMessage): Promise<JsonObject> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_BODY_BYTES) throw new Error("request body 超过 1 MiB");
    chunks.push(buffer);
  }
  if (!chunks.length) return {};
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("JSON body 必须是 object");
  return value as JsonObject;
}
function parsePath(request: IncomingMessage): URL { return new URL(request.url || "/", "http://localhost"); }

async function serveUi(response: ServerResponse, pathname: string, root: string): Promise<boolean> {
  const webRoot = resolve(root, "web");
  const relative = pathname === "/ui" || pathname === "/ui/" ? "index.html" : pathname.replace(/^\/ui\//, "");
  const candidate = resolve(webRoot, normalize(relative));
  if (!candidate.startsWith(`${webRoot}/`) && candidate !== join(webRoot, "index.html")) return false;
  try {
    const info = await lstat(candidate);
    if (!info.isFile() || info.isSymbolicLink()) return false;
    response.writeHead(200, { "content-type": MIME[extname(candidate).toLowerCase()] || "application/octet-stream", "cache-control": relative === "index.html" ? "no-store" : "public, max-age=300" });
    createReadStream(candidate).pipe(response);
    return true;
  } catch { return false; }
}

async function streamEvents(response: ServerResponse, stream: ReturnType<AgenelfAgent["startChat"]>["stream"], afterSeq: number, request: IncomingMessage): Promise<void> {
  response.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache, no-transform",
    connection: "keep-alive", "x-accel-buffering": "no"
  });
  let cursor = afterSeq;
  let closed = false;
  request.on("close", () => { closed = true; });
  response.write(": connected\n\n");
  while (!closed) {
    try {
      const events = await stream.waitAfter(cursor, 15_000);
      if (!events.length) {
        response.write(`: heartbeat ${Date.now()}\n\n`);
        if (stream.isTerminal) break;
        continue;
      }
      for (const event of events) {
        cursor = event.seq;
        response.write(`id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
      }
      if (stream.isTerminal && cursor >= stream.snapshot().last_seq) break;
    } catch (error) {
      if (error instanceof EventCursorExpired) {
        response.write(`event: replay.required\ndata: ${JSON.stringify({ error: error.message })}\n\n`);
      } else {
        response.write(`event: error\ndata: ${JSON.stringify({ error: error instanceof Error ? error.message : String(error) })}\n\n`);
      }
      break;
    }
  }
  if (!closed) response.end();
}

export async function createAgenelfServer(options: { root?: string } = {}) {
  const root = resolve(options.root || rootDir());
  const agent = new AgenelfAgent(root);
  await agent.initialize();

  return createServer(async (request, response) => {
    securityHeaders(response);
    const url = parsePath(request);
    try {
      if (request.method === "GET" && url.pathname === "/") {
        response.writeHead(302, { location: "/ui/" }); response.end(); return;
      }
      if (request.method === "GET" && url.pathname.startsWith("/ui")) {
        if (!(await serveUi(response, url.pathname, root))) sendJson(response, 404, { error: "UI resource not found" });
        return;
      }
      if (request.method === "GET" && url.pathname === "/health") {
        sendJson(response, 200, { status: "ok", version: "0.9.0", runtime: "node-typescript" }); return;
      }
      if (!authorized(request)) {
        const configured = Boolean(process.env.AGENELF_API_TOKEN);
        sendJson(response, configured ? 401 : 503, { error: configured ? "无效的 Agenelf API Token" : "AGENELF_API_TOKEN 未配置，API fail-closed" }); return;
      }
      if (request.method === "GET" && url.pathname === "/status") { sendJson(response, 200, await agent.status()); return; }
      if (request.method === "GET" && url.pathname === "/capabilities") { sendJson(response, 200, { capabilities: agent.registry.catalog() }); return; }
      if (request.method === "GET" && url.pathname === "/resources") { sendJson(response, 200, { resources: agent.resources.catalog() }); return; }
      if (request.method === "POST" && url.pathname === "/chat") {
        const body = await readJsonBody(request);
        const reply = await agent.chat(String(body.message ?? ""), { sessionId: String(body.session_id ?? "default"), subject: String(body.channel ?? "http") });
        sendJson(response, 200, { reply }); return;
      }
      if (request.method === "POST" && url.pathname === "/v1/chat/runs") {
        const body = await readJsonBody(request);
        const run = agent.startChat(String(body.message ?? ""), { sessionId: String(body.session_id ?? "default"), subject: String(body.channel ?? "http") });
        run.completion.catch(() => undefined);
        sendJson(response, 202, { session_id: run.stream.sessionId, run_id: run.stream.runId, events: `/v1/sessions/${run.stream.sessionId}/runs/${run.stream.runId}/events` }); return;
      }
      const eventMatch = url.pathname.match(/^\/v1\/sessions\/([A-Za-z0-9][A-Za-z0-9._-]{0,63})\/runs\/(run-[0-9a-f]{16})\/events$/);
      if (request.method === "GET" && eventMatch) {
        const stream = agent.events.get(eventMatch[2]);
        if (stream.sessionId !== eventMatch[1]) { sendJson(response, 404, { error: "run 不属于该 session" }); return; }
        const after = Number(request.headers["last-event-id"] || url.searchParams.get("after_seq") || 0);
        await streamEvents(response, stream, Number.isFinite(after) ? after : 0, request); return;
      }
      if (request.method === "POST" && url.pathname === "/chat/stream") {
        const body = await readJsonBody(request);
        const run = agent.startChat(String(body.message ?? ""), { sessionId: String(body.session_id ?? "default"), subject: String(body.channel ?? "http") });
        run.completion.catch(() => undefined);
        await streamEvents(response, run.stream, 0, request); return;
      }
      sendJson(response, 404, { error: "Not found" });
    } catch (error) {
      if (!response.headersSent) sendJson(response, 500, { error: error instanceof Error ? error.message : String(error) });
      else response.end();
    }
  });
}

async function main() {
  const server = await createAgenelfServer();
  const host = process.env.AGENELF_HOST || "0.0.0.0";
  const port = Number(process.env.AGENELF_PORT || 8000);
  server.listen(port, host, () => console.log(`Agenelf Node API listening on http://${host}:${port}`));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main().catch((error) => { console.error(error); process.exitCode = 1; });
