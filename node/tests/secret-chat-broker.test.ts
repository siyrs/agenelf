import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtemp } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgenelfAgent } from "../packages/core/src/agent.ts";
import { createSecretChatServer, type SecretChatService } from "../apps/secret-chat-broker/src/main.ts";
import type { JsonObject, JsonValue } from "../packages/core/src/types.ts";

class FakeService implements SecretChatService {
  lastApply: JsonObject | null = null;
  async initialize(): Promise<void> {}
  async catalog(): Promise<JsonObject> {
    return { schema_version: 1, targets: [{ alias: "relay-zhipu" }] };
  }
  async snapshot(targetAlias: string, seatId = ""): Promise<JsonObject> {
    return {
      schema_version: 1,
      plaintext: true,
      env_target: targetAlias,
      seats: [{ seat_id: seatId || "zhipu-a", value: "owner-visible-plaintext-key" }]
    };
  }
  async apply(targetAlias: string, changes: unknown, confirmTarget: string): Promise<JsonObject> {
    this.lastApply = { env_target: targetAlias, confirm_target: confirmTarget, changes: changes as JsonValue };
    return { schema_version: 1, status: "succeeded", env_target: targetAlias };
  }
}

async function brokerFixture(): Promise<{
  service: FakeService;
  baseUrl: string;
  close(): Promise<void>;
}> {
  const root = await mkdtemp(join(tmpdir(), "agenelf-secret-chat-broker-"));
  const service = new FakeService();
  const server = createSecretChatServer(service, {
    token: "owner-chat-token",
    auditPath: join(root, "audit.log")
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("broker did not bind TCP port");
  return {
    service,
    baseUrl: `http://127.0.0.1:${address.port}`,
    close: async () => {
      server.close();
      await once(server, "close");
    }
  };
}

test("secret chat broker rejects unauthenticated access and returns exact plaintext to owner token", async (t) => {
  const fixture = await brokerFixture();
  t.after(fixture.close);

  const denied = await fetch(`${fixture.baseUrl}/v1/snapshot`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ env_target: "relay-zhipu" })
  });
  assert.equal(denied.status, 401);

  const allowed = await fetch(`${fixture.baseUrl}/v1/snapshot`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-agenelf-token": "owner-chat-token" },
    body: JSON.stringify({ env_target: "relay-zhipu", seat_id: "zhipu-a" })
  });
  assert.equal(allowed.status, 200);
  assert.match(String(allowed.headers.get("cache-control")), /no-store/);
  const document = await allowed.json() as JsonObject;
  assert.equal(((document.seats as JsonObject[])[0]).value, "owner-visible-plaintext-key");
});

test("secret chat broker accepts owner plaintext update without echoing value in response", async (t) => {
  const fixture = await brokerFixture();
  t.after(fixture.close);
  const replacement = "owner-new-plaintext-key";
  const response = await fetch(`${fixture.baseUrl}/v1/apply`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-agenelf-token": "owner-chat-token" },
    body: JSON.stringify({
      env_target: "relay-zhipu",
      confirm_target: "relay-zhipu",
      changes: [{ seat_id: "zhipu-c", action: "set", value: replacement }]
    })
  });
  assert.equal(response.status, 200);
  const text = await response.text();
  assert.equal(text.includes(replacement), false);
  assert.equal(String(((fixture.service.lastApply!.changes as JsonObject[])[0]).value), replacement);
});

test("only explicitly allowed sensitive tool results bypass redaction and sensitive ledger messages are not replayed", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-sensitive-chat-"));
  const agent = new AgenelfAgent(root);
  const access = agent as unknown as {
    safeToolResult(value: JsonValue, allowSensitiveResult?: boolean): JsonValue;
    history(sessionId: string, limit?: number): Promise<Array<{ role: string; content: string | null }>>;
  };
  const secret = { api_key: "owner-visible-plaintext-key" };
  assert.deepEqual(access.safeToolResult(secret), { api_key: "[REDACTED]" });
  assert.deepEqual(access.safeToolResult(secret, true), secret);

  await agent.ledger.append({
    sessionId: "owner",
    type: "message",
    origin: "runtime",
    payload: { role: "assistant", content: "ordinary answer", sensitive: false }
  });
  await agent.ledger.append({
    sessionId: "owner",
    type: "message",
    origin: "runtime",
    payload: { role: "assistant", content: "owner-visible-plaintext-key", sensitive: true }
  });
  const history = await access.history("owner", 20);
  assert.deepEqual(history.map((row) => row.content), ["ordinary answer"]);
});
