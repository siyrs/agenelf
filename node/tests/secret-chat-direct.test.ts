import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import {
  isDirectSecretChatIntent,
  normalizeOwnerSecretActionClauses,
  routeOwnerSecretChat,
  type DirectSecretChatClient
} from "../packages/core/src/secret-chat-router.ts";
import type { JsonObject, JsonValue } from "../packages/core/src/types.ts";

class FakeSecretClient implements DirectSecretChatClient {
  enabled = true;
  readonly snapshots: Array<{ target: string; seat: string }> = [];
  readonly applies: Array<{ target: string; changes: JsonValue[]; confirm: string }> = [];

  async targets(): Promise<JsonObject> {
    return {
      schema_version: 1,
      targets: [
        {
          alias: "relay-zhipu",
          label: "中天中转站",
          aliases: ["中天", "中天中转站"],
          server: "relay-prod",
          env_file: "/srv/new-api/.env.secrets",
          seats: [
            { id: "zhipu-a", label: "智谱席位 A", env_name: "ZHIPU_SEAT_A_API_KEY" },
            { id: "zhipu-b", label: "智谱席位 B", env_name: "ZHIPU_SEAT_B_API_KEY" },
            { id: "zhipu-c", label: "智谱席位 C", env_name: "ZHIPU_SEAT_C_API_KEY" },
            { id: "zhipu-d", label: "智谱席位 D", env_name: "ZHIPU_SEAT_D_API_KEY" }
          ]
        },
        {
          alias: "relay-openai",
          label: "备用中转站",
          aliases: [],
          server: "relay-backup",
          env_file: "/srv/backup/.env.secrets",
          seats: [
            { id: "openai-a", label: "OpenAI A", env_name: "OPENAI_A_KEY" },
            { id: "openai-b", label: "OpenAI B", env_name: "OPENAI_B_KEY" }
          ]
        }
      ]
    };
  }

  async snapshot(envTarget: string, seatId = ""): Promise<JsonObject> {
    this.snapshots.push({ target: envTarget, seat: seatId });
    const all = [
      { seat_id: "zhipu-a", label: "智谱席位 A", env_name: "ZHIPU_SEAT_A_API_KEY", present: true, value: "zhipu-owner-key-A" },
      { seat_id: "zhipu-b", label: "智谱席位 B", env_name: "ZHIPU_SEAT_B_API_KEY", present: true, value: "zhipu-owner-key-B" },
      { seat_id: "zhipu-c", label: "智谱席位 C", env_name: "ZHIPU_SEAT_C_API_KEY", present: true, value: "zhipu-owner-key-C" },
      { seat_id: "zhipu-d", label: "智谱席位 D", env_name: "ZHIPU_SEAT_D_API_KEY", present: true, value: "zhipu-owner-key-D" }
    ];
    return {
      schema_version: 1,
      plaintext: true,
      env_target: envTarget,
      server: "relay-prod",
      env_file: "/srv/new-api/.env.secrets",
      seats: seatId ? all.filter((item) => item.seat_id === seatId) : all
    };
  }

  async apply(envTarget: string, changes: JsonValue[], confirmTarget: string): Promise<JsonObject> {
    this.applies.push({ target: envTarget, changes, confirm: confirmTarget });
    return {
      schema_version: 1,
      status: "succeeded",
      env_target: envTarget,
      changes: [
        { seat_id: "zhipu-a", action: "keep", old_fingerprint: "AAAA", new_fingerprint: "AAAA" },
        { seat_id: "zhipu-b", action: "delete", old_fingerprint: "BBBB", new_fingerprint: "" },
        { seat_id: "zhipu-c", action: "set", old_fingerprint: "CCCC", new_fingerprint: "C999" },
        { seat_id: "zhipu-d", action: "keep", old_fingerprint: "DDDD", new_fingerprint: "DDDD" }
      ],
      rollback_backup_retained: false
    };
  }
}

test("the exact screenshot request bypasses the model and reveals all four Zhipu plaintext keys", async () => {
  const client = new FakeSecretClient();
  const result = await routeOwnerSecretChat("显示中天中转站的4个智谱完整key明文", client);
  assert.equal(result.handled, true);
  assert.equal(result.route, "reveal");
  assert.equal(result.sensitive, true);
  assert.deepEqual(client.snapshots, [{ target: "relay-zhipu", seat: "" }]);
  for (const value of ["zhipu-owner-key-A", "zhipu-owner-key-B", "zhipu-owner-key-C", "zhipu-owner-key-D"]) {
    assert.match(result.reply ?? "", new RegExp(value));
  }
  assert.match(result.reply ?? "", /确定性 Secret Chat 路由直接返回/);
});

test("natural language deletes B, replaces C and leaves omitted seats unchanged", async () => {
  const client = new FakeSecretClient();
  const text = "中天中转站：删除 zhipu-b，把 zhipu-c 改成 zhipu-new-owner-key-C9，其他席位不动，直接更新上去";
  assert.match(normalizeOwnerSecretActionClauses(text), /删除 zhipu-b；把 zhipu-c 改成/);
  const result = await routeOwnerSecretChat(text, client);
  assert.equal(result.handled, true);
  assert.equal(result.route, "apply");
  assert.equal(client.applies.length, 1);
  assert.equal(client.applies[0].target, "relay-zhipu");
  assert.equal(client.applies[0].confirm, "relay-zhipu");
  assert.deepEqual(client.applies[0].changes, [
    { seat_id: "zhipu-b", action: "delete" },
    { seat_id: "zhipu-c", action: "set", value: "zhipu-new-owner-key-C9" }
  ]);
  assert.match(result.reply ?? "", /未列出的席位保持不动/);
  assert.doesNotMatch(result.reply ?? "", /zhipu-new-owner-key-C9/);
});

test("slash commands provide a model-independent recovery path", async () => {
  const client = new FakeSecretClient();
  const reveal = await routeOwnerSecretChat("/secret show relay-zhipu zhipu-b", client);
  assert.deepEqual(client.snapshots.at(-1), { target: "relay-zhipu", seat: "zhipu-b" });
  assert.match(reveal.reply ?? "", /zhipu-owner-key-B/);

  const update = await routeOwnerSecretChat("/secret set relay-zhipu zhipu-c direct-slash-key-C", client);
  assert.equal(update.route, "apply");
  assert.deepEqual(client.applies.at(-1)?.changes, [
    { seat_id: "zhipu-c", action: "set", value: "direct-slash-key-C" }
  ]);
});

test("disabled or unavailable plaintext mode returns diagnostics instead of a model refusal", async () => {
  const disabled = new FakeSecretClient();
  disabled.enabled = false;
  const disabledResult = await routeOwnerSecretChat("显示中天中转站完整 key 明文", disabled);
  assert.equal(disabledResult.handled, true);
  assert.match(disabledResult.reply ?? "", /AGENELF_CHAT_PLAINTEXT_SECRETS/);

  const unavailable: DirectSecretChatClient = {
    enabled: true,
    targets: async () => { throw new Error("connect ECONNREFUSED"); },
    snapshot: async () => ({}),
    apply: async () => ({})
  };
  const unavailableResult = await routeOwnerSecretChat("显示中天中转站完整 key 明文", unavailable);
  assert.equal(unavailableResult.handled, true);
  assert.match(unavailableResult.reply ?? "", /Broker 不可用/);
  assert.match(unavailableResult.reply ?? "", /ECONNREFUSED/);
});

test("ordinary API-key discussion is not intercepted", async () => {
  const client = new FakeSecretClient();
  assert.equal(isDirectSecretChatIntent("请解释 API Key 的工作原理"), false);
  const result = await routeOwnerSecretChat("请解释 API Key 的工作原理", client);
  assert.equal(result.handled, false);
  assert.equal(client.snapshots.length, 0);
  assert.equal(client.applies.length, 0);
});

test("Agent invokes the deterministic router before any model request", async () => {
  const source = await readFile(resolve("node/packages/core/src/agent.ts"), "utf8");
  const directIndex = source.indexOf("await routeOwnerSecretChat(text, this.secretChat)");
  const modelIndex = source.indexOf("this.model.streamChat(messages, tools");
  assert.ok(directIndex > 0, "direct secret route must be wired");
  assert.ok(modelIndex > directIndex, "model must run only after the direct route declines the request");
  assert.match(source, /secret-chat-router\.ts/);
  assert.match(source, /reason:\s*"direct_secret_route"/);
});
