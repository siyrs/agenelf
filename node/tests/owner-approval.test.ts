import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
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
  const key = Buffer.from("k".repeat(64), "utf8");
  return { root, store, key };
}

async function writeRequest(store: OwnerApprovalStore, id: string, overrides: Record<string, unknown> = {}) {
  const request = {
    schema_version: 1,
    id,
    capability: "server.operations",
    operation: "restart_service",
    target: "demo-api",
    parameters: { service: "demo-api" },
    risk: "change",
    summary: "Restart demo API",
    created_at: "2026-07-28T00:00:00+00:00",
    fingerprint: "",
    ...overrides
  } as Record<string, unknown>;
  request.fingerprint = bindingFingerprint(bindingFromRequest(request));
  await writeFile(join(store.opsRequests, `${id}.json`), `${JSON.stringify(request, null, 2)}\n`);
  return request;
}

test("python canonical form preserves sorted ASCII and escaped Unicode compatibility", () => {
  assert.equal(
    pythonCanonical({ z: "星", a: ["😀", true] }),
    '{"a":["\\ud83d\\ude00",true],"z":"\\u661f"}'
  );
});

test("signed command is verified and consumed by Node approval runner", async () => {
  const { root, store, key } = await fixture();
  const request = await writeRequest(store, "op-1111111111111111");
  const command = await store.submitCommand(String(request.id), key, {
    action: "approve",
    reason: "owner confirmed",
    decidedBy: "owner-cli"
  });
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

test("tampered and expired commands fail closed without writing decisions", async () => {
  const { root, store, key } = await fixture();
  const request = await writeRequest(store, "op-2222222222222222");
  const command = await store.submitCommand(String(request.id), key, { action: "approve" });
  command.reason = "tampered";
  await writeFile(join(store.commands, `${String(command.id)}.json`), `${JSON.stringify(command, null, 2)}\n`);

  const runner = new ApprovalRunner(root, key);
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), { failed: 1 });
  const result = JSON.parse(await readFile(join(store.commandResults, `${String(command.id)}.json`), "utf8"));
  assert.equal(result.status, "failed");
  assert.match(result.error, /签名无效/);
  await assert.rejects(readFile(join(store.authDecisions, `${String(request.id)}.json`), "utf8"));

  const request2 = await writeRequest(store, "op-3333333333333333");
  const expired = await store.submitCommand(String(request2.id), key, { action: "deny" });
  expired.created_at = "2026-07-27T00:00:00.000Z";
  expired.expires_at = "2026-07-27T00:01:00.000Z";
  expired.signature = OwnerApprovalStore.signCommand(expired, key);
  await assert.rejects(() => store.verifyCommand(expired, key, new Date("2026-07-28T00:00:00.000Z")), /已过期/);
});

test("pending resolver deduplicates one binding and rejects different bindings", async () => {
  const { store } = await fixture();
  await writeRequest(store, "op-4444444444444444");
  await writeRequest(store, "op-5555555555555555");
  const resolved = await store.resolvePending();
  assert.equal(resolved.selected.id, "op-5555555555555555");
  assert.deepEqual(resolved.duplicates, ["op-4444444444444444"]);

  await writeRequest(store, "op-6666666666666666", { target: "other-api", parameters: { service: "other-api" } });
  await assert.rejects(() => store.resolvePending(), AmbiguousApprovalError);
});

test("approval request fingerprint tampering is rejected before command creation", async () => {
  const { store, key } = await fixture();
  const request = await writeRequest(store, "op-7777777777777777");
  request.target = "tampered-api";
  await writeFile(join(store.opsRequests, `${String(request.id)}.json`), `${JSON.stringify(request, null, 2)}\n`);
  await assert.rejects(() => store.submitCommand(String(request.id), key), /指纹不匹配/);
});
