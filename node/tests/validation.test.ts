import test from "node:test";
import assert from "node:assert/strict";
import { createServer as createHttpServer } from "node:http";
import { createServer as createTcpServer } from "node:net";
import { once } from "node:events";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ValidationQueue, ValidationRunner } from "../packages/core/src/validation.ts";
import type { JsonObject } from "../packages/core/src/types.ts";

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-validation-test-"));
  await mkdir(join(root, "local"), { recursive: true });

  const http = createHttpServer((request, response) => {
    if (request.url === "/redirect") {
      response.writeHead(302, { location: "/json" });
      response.end();
      return;
    }
    if (request.url === "/loop") {
      response.writeHead(302, { location: "/loop" });
      response.end();
      return;
    }
    if (request.url === "/large") {
      response.writeHead(200, { "content-type": "text/plain" });
      response.end("x".repeat(1_100_000));
      return;
    }
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ status: "ok", nested: { value: 7 }, text: "hello validation" }));
  });
  http.listen(0, "127.0.0.1");
  await once(http, "listening");
  const httpAddress = http.address();
  if (!httpAddress || typeof httpAddress === "string") throw new Error("missing HTTP address");

  const tcp = createTcpServer((socket) => socket.end());
  tcp.listen(0, "127.0.0.1");
  await once(tcp, "listening");
  const tcpAddress = tcp.address();
  if (!tcpAddress || typeof tcpAddress === "string") throw new Error("missing TCP address");

  const config = `
checks:
  http-ok:
    type: http
    description: local HTTP check
    url: http://127.0.0.1:${httpAddress.port}/json
    method: GET
    expected_status: [200]
    contains: [hello, validation]
    json_equals:
      status: ok
      nested.value: 7
    timeout_seconds: 3
    max_latency_ms: 5000
    tags: [http, smoke]
  redirect-ok:
    type: http
    url: http://127.0.0.1:${httpAddress.port}/redirect
    expected_status: [200]
    json_equals:
      status: ok
  redirect-loop:
    type: http
    url: http://127.0.0.1:${httpAddress.port}/loop
    expected_status: [200]
  large-body:
    type: http
    url: http://127.0.0.1:${httpAddress.port}/large
    expected_status: [200]
  tcp-ok:
    type: tcp
    host: 127.0.0.1
    port: ${tcpAddress.port}
    timeout_seconds: 3
    max_latency_ms: 5000
suites:
  smoke:
    description: local suite
    checks:
      - http-ok
      - redirect-ok
      - tcp-ok
`;
  const validationFile = join(root, "local", "validation.yaml");
  await writeFile(validationFile, config, "utf8");
  return { root, validationFile, http, tcp };
}

test("Node validation queue keeps Python-compatible canonical fingerprint", async () => {
  const value = await fixture();
  try {
    const queue = new ValidationQueue(value.root, value.validationFile);
    await queue.initialize();
    const payload = ValidationQueue.canonicalPayload("run_check", "http-ok");
    assert.deepEqual(payload, {
      capability: "software.validation",
      operation: "run_check",
      target: "http-ok",
      parameters: {}
    });
    assert.equal(
      ValidationQueue.fingerprint(payload),
      "25562f04df0aa497c7d9156cea4735d87d244d482cc9f64e9651b74032128c21"
    );
    assert.equal(queue.hasCheck("http-ok"), true);
    assert.equal(queue.hasSuite("smoke"), true);
    assert.deepEqual(queue.suiteMembers("smoke"), ["http-ok", "redirect-ok", "tcp-ok"]);
    const catalog = queue.catalog();
    assert.equal((catalog.checks as JsonObject[]).length, 5);
    assert.equal((catalog.suites as JsonObject[]).length, 1);
    await assert.rejects(queue.submit("run_check", "unknown", "bad"), /未知验证检查/);
  } finally {
    value.http.close();
    value.tcp.close();
  }
});

test("Node validation runner executes HTTP, redirects, TCP, suites and bounded bodies", async () => {
  const value = await fixture();
  try {
    const runner = new ValidationRunner(value.root, value.validationFile);
    await runner.initialize();

    const httpResult = await runner.runCheck("http-ok");
    assert.equal(httpResult.passed, true);
    assert.equal((httpResult.observed as JsonObject).status_code, 200);
    assert.equal((httpResult.assertions as JsonObject[]).every((item) => item.passed === true), true);

    const redirect = await runner.runCheck("redirect-ok");
    assert.equal(redirect.passed, true);

    const loop = await runner.runCheck("redirect-loop");
    assert.equal(loop.passed, false);
    assert.match(JSON.stringify(loop.assertions), /重定向超过/);

    const large = await runner.runCheck("large-body");
    assert.equal(large.passed, true);
    assert.equal((large.observed as JsonObject).body_truncated, true);
    assert.equal((large.observed as JsonObject).body_bytes, 1_000_000);

    const tcp = await runner.runCheck("tcp-ok");
    assert.equal(tcp.passed, true);

    const request = await runner.queue.submit("run_suite", "smoke", "local integration suite");
    const counts = await runner.processOnce();
    assert.equal(counts.succeeded, 1);
    const state = await runner.queue.get(String(request.id));
    assert.equal(state.status, "succeeded");
    assert.equal((state.result as JsonObject).passed, 3);
    assert.equal((state.result as JsonObject).failed, 0);

    const resultPath = join(value.root, "data", "validation-results", `${request.id}.json`);
    const before = await readFile(resultPath, "utf8");
    const second = await runner.processOnce();
    assert.equal(second.done, 1);
    assert.equal(await readFile(resultPath, "utf8"), before, "trusted result must remain immutable");
    assert.match(await readFile(join(value.root, "logs", "validation.log"), "utf8"), /validation_submitted/);
  } finally {
    value.http.close();
    value.tcp.close();
  }
});

test("Node validation runner rejects tampering, free parameters and unknown aliases", async () => {
  const value = await fixture();
  try {
    const runner = new ValidationRunner(value.root, value.validationFile);
    await runner.initialize();

    const tampered = await runner.queue.submit("run_check", "http-ok", "tamper test");
    const tamperedPath = join(value.root, "data", "validation-requests", `${tampered.id}.json`);
    const tamperedValue = JSON.parse(await readFile(tamperedPath, "utf8")) as JsonObject;
    tamperedValue.fingerprint = "0".repeat(64);
    await writeFile(tamperedPath, `${JSON.stringify(tamperedValue, null, 2)}\n`, "utf8");
    assert.equal(await runner.processRequest(tamperedPath), "failed");
    const tamperedResult = JSON.parse(await readFile(join(value.root, "data", "validation-results", `${tampered.id}.json`), "utf8"));
    assert.match(String(tamperedResult.reason), /指纹不匹配/);

    const free = await runner.queue.submit("run_check", "http-ok", "parameter test");
    const freePath = join(value.root, "data", "validation-requests", `${free.id}.json`);
    const freeValue = JSON.parse(await readFile(freePath, "utf8")) as JsonObject;
    freeValue.parameters = { url: "http://attacker.invalid" };
    await writeFile(freePath, `${JSON.stringify(freeValue, null, 2)}\n`, "utf8");
    assert.equal(await runner.processRequest(freePath), "failed");
    const freeResult = JSON.parse(await readFile(join(value.root, "data", "validation-results", `${free.id}.json`), "utf8"));
    assert.match(String(freeResult.reason), /不得携带自由参数/);

    const id = "val-0000000000000001";
    const unknownPayload = ValidationQueue.canonicalPayload("run_check", "missing-alias");
    const unknownPath = join(value.root, "data", "validation-requests", `${id}.json`);
    await writeFile(unknownPath, `${JSON.stringify({
      schema_version: 1,
      id,
      ...unknownPayload,
      risk: "read",
      summary: "unknown alias",
      fingerprint: ValidationQueue.fingerprint(unknownPayload),
      created_at: new Date().toISOString(),
      created_by: "test"
    }, null, 2)}\n`, "utf8");
    assert.equal(await runner.processRequest(unknownPath), "failed");
    const unknownResult = JSON.parse(await readFile(join(value.root, "data", "validation-results", `${id}.json`), "utf8"));
    assert.match(String(unknownResult.reason), /未知验证检查/);
  } finally {
    value.http.close();
    value.tcp.close();
  }
});
