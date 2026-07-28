import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { OperationQueue } from "../packages/core/src/operation-queue.ts";
import { ReadOnlyOpsRunner, isSemanticReadRequest, type RemoteCommandResult } from "../packages/core/src/read-ops.ts";
import { ServerCatalog } from "../packages/core/src/server-catalog.ts";
import { sha256 } from "../packages/core/src/canonical.ts";
import type { JsonValue } from "../packages/core/src/types.ts";

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-read-ops-test-"));
  await mkdir(join(root, "local", "secrets"), { recursive: true });
  await mkdir(join(root, "data", "ops-results"), { recursive: true });
  await mkdir(join(root, "data", "ops-locks"), { recursive: true });
  await mkdir(join(root, "data", "ops-events"), { recursive: true });
  await mkdir(join(root, "logs"), { recursive: true });
  await writeFile(join(root, "local", "secrets", "test_ed25519"), "test-key", { mode: 0o600 });
  await writeFile(join(root, "local", "secrets", "known_hosts"), "localhost ssh-ed25519 AAAATEST\n", { mode: 0o600 });
  await writeFile(join(root, "local", "servers.yaml"), `servers:\n  primary:\n    host: 127.0.0.1\n    port: 2222\n    username: agenelf\n    connect_timeout: 5\n    auth:\n      type: private_key\n      private_key: test_ed25519\n    known_hosts: known_hosts\n    allow_unknown_host_key: false\n    docker_command: docker\n    allowed_operations: [inspect, docker_ps, service_status, apt_update]\n    allowed_docker_operations: [get_docker_logs, inspect_docker_container, run_docker_check, restart_docker_container]\n    allowed_containers: [demo]\n    allowed_services: [nginx]\n    docker_checks:\n      config:\n        container: demo\n        argv: [demo, check, --safe]\n`);
  let calls = 0;
  const executeRemote = async (_server: unknown, command: string): Promise<RemoteCommandResult> => {
    calls += 1;
    return { command, exit_code: 0, stdout: "token=secret-value\nvmess://private-node", stderr: "" };
  };
  const catalog = new ServerCatalog(root);
  const runner = new ReadOnlyOpsRunner(root, { catalog, executeRemote });
  await runner.initialize();
  return { root, runner, calls: () => calls };
}

test("Python-compatible canonical fingerprint stays stable", () => {
  const payload = {
    capability: "server.operations",
    operation: "service_status",
    target: "主机-a",
    parameters: { service: "nginx" }
  };
  assert.equal(
    sha256(payload as unknown as JsonValue),
    "84e961b52cc0723e7a6afb87b9965ecfe7ad0abad4cc3f6e946f59d769b85739"
  );
});

test("semantic partition ignores declared risk", () => {
  assert.equal(isSemanticReadRequest({ capability: "server.operations", operation: "inspect", risk: "change" }), true);
  assert.equal(isSemanticReadRequest({ capability: "server.operations", operation: "apt_update", risk: "read" }), false);
});

test("read request executes fixed command, redacts output and appends replay events", async () => {
  const { root, runner, calls } = await fixture();
  const queue = new OperationQueue(root);
  const request = await queue.submit({
    capability: "docker.operations",
    operation: "get_docker_logs",
    target: "primary",
    parameters: { container: "demo", tail: 20 },
    risk: "read",
    summary: "read logs"
  });
  const counts = await runner.processOnce();
  assert.equal(counts.succeeded, 1);
  assert.equal(calls(), 1);
  const result = JSON.parse(await readFile(join(root, "data", "ops-results", `${request.id}.json`), "utf8"));
  assert.equal(result.status, "succeeded");
  assert.match(result.commands[0].command, /docker logs --tail 20 'demo'/);
  assert.doesNotMatch(result.commands[0].stdout, /secret-value|private-node/);
  assert.match(result.commands[0].stdout, /\[REDACTED\]/);
  const events = (await readFile(join(root, "data", "ops-events", `${request.id}.jsonl`), "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(events.map((event) => event.type), ["ops.runner.claimed", "ssh.started", "ssh.completed", "ops.result.persisted"]);
  assert.equal(events.every((event) => event.origin === "runner"), true);
});

test("tampered risk fails closed before SSH", async () => {
  const { root, runner, calls } = await fixture();
  const queue = new OperationQueue(root);
  const request = await queue.submit({ capability: "server.operations", operation: "inspect", target: "primary", risk: "read", summary: "inspect" });
  const path = join(root, "data", "ops-requests", `${request.id}.json`);
  const document = JSON.parse(await readFile(path, "utf8"));
  document.risk = "change";
  await writeFile(path, `${JSON.stringify(document, null, 2)}\n`);
  const counts = await runner.processOnce();
  assert.equal(counts.failed, 1);
  assert.equal(calls(), 0);
  const result = JSON.parse(await readFile(join(root, "data", "ops-results", `${request.id}.json`), "utf8"));
  assert.match(result.reason, /read 风险/);
});

test("expired read request is finalized without SSH", async () => {
  const { root, runner, calls } = await fixture();
  const queue = new OperationQueue(root);
  const request = await queue.submit({ capability: "server.operations", operation: "docker_ps", target: "primary", risk: "read", summary: "ps" });
  const path = join(root, "data", "ops-requests", `${request.id}.json`);
  const document = JSON.parse(await readFile(path, "utf8"));
  document.expires_at = "2000-01-01T00:00:00.000Z";
  await writeFile(path, `${JSON.stringify(document, null, 2)}\n`);
  const counts = await runner.processOnce();
  assert.equal(counts.expired, 1);
  assert.equal(calls(), 0);
});

test("change request is left for Python runner", async () => {
  const { root, runner, calls } = await fixture();
  const queue = new OperationQueue(root);
  const request = await queue.submit({ capability: "server.operations", operation: "apt_update", target: "primary", risk: "change", summary: "apt" });
  const counts = await runner.processOnce();
  assert.equal(counts.skipped, 1);
  assert.equal(calls(), 0);
  await assert.rejects(readFile(join(root, "data", "ops-results", `${request.id}.json`), "utf8"));
});

test("catalog rejects credential path escape and unknown command parameters", async () => {
  const { root } = await fixture();
  const bad = join(root, "local", "bad.yaml");
  await writeFile(bad, `servers:\n  primary:\n    host: 127.0.0.1\n    username: agenelf\n    auth:\n      type: private_key\n      private_key: ../escape\n    known_hosts: known_hosts\n`);
  await assert.rejects(new ServerCatalog(root, bad).initialize(), /安全文件名/);
});
