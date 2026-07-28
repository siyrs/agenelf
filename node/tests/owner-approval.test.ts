import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  AmbiguousApprovalError,
  ApprovalRunner,
  bindingFingerprint,
  bindingFromRequest,
  OwnerApprovalStore,
  pythonCanonical
} from "../packages/core/src/owner-approval.ts";

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-approval-test-"));
  const store = new OwnerApprovalStore(root);
  await store.initialize();
  return { root, store, key: Buffer.from("k".repeat(64), "utf8") };
}

async function writeRequest(store: OwnerApprovalStore, id: string, target = "demo-api", overrides: Record<string, unknown> = {}) {
  const request = {
    schema_version: 1,
    id,
    capability: "server.operations",
    operation: "restart_service",
    target,
    parameters: { service: target },
    risk: "change",
    summary: `Restart ${target}`,
    created_at: new Date().toISOString(),
    fingerprint: "",
    ...overrides
  } as Record<string, unknown>;
  request.fingerprint = bindingFingerprint(bindingFromRequest(request));
  const base = id.startsWith("op-") ? store.opsRequests : store.authRequests;
  await writeFile(join(base, `${id}.json`), `${JSON.stringify(request, null, 2)}\n`);
  return request;
}

test("Python canonical form preserves sorted keys and escaped Unicode", () => {
  assert.equal(pythonCanonical({ z: "星", a: ["😀", true] }), '{"a":["\\ud83d\\ude00",true],"z":"\\u661f"}');
});

test("signed command is verified and consumed by Node approval runner", async () => {
  const { root, store, key } = await fixture();
  const request = await writeRequest(store, "op-1111111111111111");
  const command = await store.submitCommand(String(request.id), key, { action: "approve", reason: "owner confirmed", decidedBy: "owner-cli" });
  await store.verifyCommand(command, key, new Date(String(command.created_at)));
  const runner = new ApprovalRunner(root, key);
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), { succeeded: 1 });
  const result = JSON.parse(await readFile(join(store.commandResults, `${String(command.id)}.json`), "utf8"));
  const decision = JSON.parse(await readFile(join(store.authDecisions, `${String(request.id)}.json`), "utf8"));
  assert.equal(result.status, "succeeded");
  assert.equal(decision.decision, "approve");
  assert.equal(decision.fingerprint, request.fingerprint);
  assert.deepEqual(await runner.processOnce(), { done: 1 });
});

test("runner recovers after decisions were written before command result", async () => {
  const { root, store, key } = await fixture();
  const duplicate = await writeRequest(store, "op-1212121212121212");
  const selected = await writeRequest(store, "op-1313131313131313");
  const command = await store.submitCommand(String(selected.id), key, {
    action: "deny", reason: "owner denied", decidedBy: "owner-cli", duplicates: [String(duplicate.id)]
  });
  await store.applyDecision(String(selected.id), {
    action: "deny", reason: "owner denied", decidedBy: "owner-cli",
    expectedFingerprint: String(selected.fingerprint), duplicates: [String(duplicate.id)]
  });
  const runner = new ApprovalRunner(root, key);
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), { succeeded: 1 });
  const result = JSON.parse(await readFile(join(store.commandResults, `${String(command.id)}.json`), "utf8"));
  assert.equal(result.status, "succeeded");
  assert.equal(result.decision.idempotent, true);
  assert.deepEqual(result.decision.superseded_duplicates, [duplicate.id]);
});

test("tampered, expired, future and unknown commands fail closed", async () => {
  const { root, store, key } = await fixture();
  const request = await writeRequest(store, "op-2222222222222222");
  const command = await store.submitCommand(String(request.id), key, { action: "approve" });
  command.reason = "tampered";
  await writeFile(join(store.commands, `${String(command.id)}.json`), `${JSON.stringify(command, null, 2)}\n`);
  const runner = new ApprovalRunner(root, key);
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), { failed: 1 });
  const result = JSON.parse(await readFile(join(store.commandResults, `${String(command.id)}.json`), "utf8"));
  assert.match(result.error, /签名无效/);
  await assert.rejects(readFile(join(store.authDecisions, `${String(request.id)}.json`), "utf8"));

  const request2 = await writeRequest(store, "op-3333333333333333");
  const expired = await store.submitCommand(String(request2.id), key, { action: "deny" });
  expired.created_at = "2026-07-27T00:00:00.000Z";
  expired.expires_at = "2026-07-27T00:01:00.000Z";
  expired.signature = OwnerApprovalStore.signCommand(expired, key);
  await assert.rejects(() => store.verifyCommand(expired, key, new Date("2026-07-28T00:00:00.000Z")), /已过期/);

  const future = { ...expired };
  const created = new Date(Date.now() + 10 * 60 * 1000);
  future.created_at = created.toISOString();
  future.expires_at = new Date(created.getTime() + 60_000).toISOString();
  future.signature = OwnerApprovalStore.signCommand(future, key);
  await assert.rejects(() => store.verifyCommand(future, key), /时钟偏差/);

  const request3 = await writeRequest(store, "op-3434343434343434");
  const unknown = await store.submitCommand(String(request3.id), key, { action: "approve" });
  await assert.rejects(() => store.verifyCommand({ ...unknown, shell: "rm -rf /" }, key), /未知字段/);
  const missing = { ...unknown };
  delete missing.signature;
  await assert.rejects(() => store.verifyCommand(missing, key), /缺少字段/);
});

test("pending resolver converges same binding and rejects ambiguous bindings", async () => {
  const { store } = await fixture();
  const first = await writeRequest(store, "op-4444444444444444");
  const selected = await writeRequest(store, "op-5555555555555555");
  const explicit = await store.resolvePending(String(selected.id));
  assert.equal(explicit.selected.id, selected.id);
  assert.deepEqual(explicit.duplicates, [first.id]);

  await writeRequest(store, "op-6666666666666666", "other-api");
  await assert.rejects(() => store.resolvePending(), AmbiguousApprovalError);
});

test("mismatched duplicate is rejected before any decision write", async () => {
  const { store } = await fixture();
  const primary = await writeRequest(store, "op-7777777777777777", "demo-api");
  const mismatch = await writeRequest(store, "op-8888888888888888", "other-api");
  await assert.rejects(
    () => store.applyDecision(String(primary.id), { action: "approve", duplicates: [String(mismatch.id)], decidedBy: "owner-cli" }),
    /指纹不同/
  );
  await assert.rejects(readFile(join(store.authDecisions, `${String(primary.id)}.json`), "utf8"));
  await assert.rejects(readFile(join(store.authDecisions, `${String(mismatch.id)}.json`), "utf8"));
});

test("request tampering, file ID mismatch, short keys and conflicting decisions are rejected", async () => {
  const { store, key } = await fixture();
  const request = await writeRequest(store, "op-9999999999999999");
  request.target = "tampered-api";
  await writeFile(join(store.opsRequests, `${String(request.id)}.json`), `${JSON.stringify(request, null, 2)}\n`);
  await assert.rejects(() => store.submitCommand(String(request.id), key), /指纹不匹配/);

  const mismatched = await writeRequest(store, "op-aaaaaaaaaaaaaaaa");
  mismatched.id = "op-bbbbbbbbbbbbbbbb";
  mismatched.fingerprint = bindingFingerprint(bindingFromRequest(mismatched));
  await writeFile(join(store.opsRequests, "op-aaaaaaaaaaaaaaaa.json"), `${JSON.stringify(mismatched, null, 2)}\n`);
  await assert.rejects(() => store.loadRequest("op-aaaaaaaaaaaaaaaa"), /文档 ID 不一致/);

  const conflict = await writeRequest(store, "op-cccccccccccccccc");
  await assert.rejects(() => store.submitCommand(String(conflict.id), Buffer.from("short")), /太短/);
  await store.applyDecision(String(conflict.id), { action: "approve", reason: "first", decidedBy: "owner-cli" });
  await assert.rejects(
    () => store.applyDecision(String(conflict.id), { action: "deny", reason: "different", decidedBy: "owner-cli" }),
    /不同裁决/
  );
});

test("command symlinks are ignored and filename/document ID mismatch fails", async () => {
  const { root, store, key } = await fixture();
  const outside = join(root, "outside-command.json");
  await writeFile(outside, "{}\n");
  await mkdir(store.commands, { recursive: true });
  await symlink(outside, join(store.commands, "apc-dddddddddddddddd.json"));
  const runner = new ApprovalRunner(root, key);
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), {});

  const request = await writeRequest(store, "op-eeeeeeeeeeeeeeee");
  const command = await store.submitCommand(String(request.id), key, { action: "approve" });
  const mismatchedPath = join(store.commands, "apc-ffffffffffffffff.json");
  await writeFile(mismatchedPath, `${JSON.stringify(command, null, 2)}\n`);
  assert.equal(await runner.processPath(mismatchedPath), "failed");
  const result = JSON.parse(await readFile(join(store.commandResults, "apc-ffffffffffffffff.json"), "utf8"));
  assert.match(result.error, /文档 ID 不一致/);
});
