import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ValidationQueue, ValidationRunner } from "../packages/core/src/validation.ts";

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-validation-test-"));
  await mkdir(join(root, "local"), { recursive: true });
  const server = createServer((_request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ status: "ok", nested: { value: 7 } }));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("missing test server address");
  await writeFile(join(root, "local", "validation.yaml"), [
    "checks:",
    "  health:",
    "    type: http",
    "    description: Local health",
    `    url: http://127.0.0.1:${address.port}/health`,
    "    method: GET",
    "    expected_status: [200]",
    "    contains: [ok]",
    "    json_equals:",
    "      status: ok",
    "      nested.value: 7",
    "    timeout_seconds: 3",
    "    tags: [local, smoke]",
    "suites:",
    "  smoke:",
    "    checks:",
    "      - health",
    ""
  ].join("\n"));
  return { root, server };
}

test("Node validation preserves alias-only immutable queue and trusted evidence", async () => {
  const { root, server } = await fixture();
  try {
    const queue = new ValidationQueue(root);
    await queue.initialize();
    const catalog = queue.catalog();
    assert.deepEqual((catalog.checks as Array<{ name: string }>).map((item) => item.name), ["health"]);
    assert.equal(JSON.stringify(catalog).includes("127.0.0.1"), false);

    const check = await queue.submit("run_check", "health", "single check");
    const suite = await queue.submit("run_suite", "smoke", "suite check");
    assert.match(String(check.id), /^val-[0-9a-f]{16}$/);
    assert.equal(check.risk, "read");
    assert.deepEqual(check.parameters, {});
    assert.equal(typeof check.fingerprint, "string");

    const runner = new ValidationRunner(root);
    await runner.initialize();
    const counts = await runner.processOnce();
    assert.equal(counts.succeeded, 2);
    const checkState = await queue.get(String(check.id));
    const suiteState = await queue.get(String(suite.id));
    assert.equal(checkState.status, "succeeded");
    assert.equal(suiteState.status, "succeeded");
    assert.equal((checkState.result as { checks: Array<{ passed: boolean }> }).checks[0].passed, true);
  } finally { server.close(); }
});

test("Node validation fails closed when request fingerprint is tampered", async () => {
  const { root, server } = await fixture();
  try {
    const queue = new ValidationQueue(root);
    await queue.initialize();
    const request = await queue.submit("run_check", "health", "tamper test");
    const path = join(root, "data", "validation-requests", `${String(request.id)}.json`);
    const value = JSON.parse(await readFile(path, "utf8"));
    value.fingerprint = "0".repeat(64);
    await writeFile(path, `${JSON.stringify(value, null, 2)}\n`);

    const runner = new ValidationRunner(root);
    await runner.initialize();
    const counts = await runner.processOnce();
    assert.equal(counts.failed, 1);
    const state = await queue.get(String(request.id));
    assert.equal(state.status, "failed");
    assert.match(String((state.result as { reason: string }).reason), /指纹不匹配/);
  } finally { server.close(); }
});
