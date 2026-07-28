import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ChangeOpsRunner, validateComposeYaml, type ChangeTransport } from "../packages/core/src/change-ops.ts";
import { OperationQueue, type OperationRequest } from "../packages/core/src/operation-queue.ts";
import { ServerCatalog, type ManagedServer } from "../packages/core/src/server-catalog.ts";
import type { RemoteCommandResult } from "../packages/core/src/open-ssh.ts";

class FakeTransport implements ChangeTransport {
  readonly calls: Array<{ kind: "run" | "write"; command: string; content?: string }> = [];
  readonly responder: (command: string) => number;

  constructor(responder: (command: string) => number = () => 0) {
    this.responder = responder;
  }

  async run(_server: ManagedServer, command: string): Promise<RemoteCommandResult> {
    this.calls.push({ kind: "run", command });
    return { command, exit_code: this.responder(command), stdout: "ok", stderr: "" };
  }

  async writeText(_server: ManagedServer, path: string, content: string): Promise<RemoteCommandResult> {
    this.calls.push({ kind: "write", command: `write:${path}`, content });
    return { command: `umask 077; cat > '${path}'`, exit_code: this.responder(`write:${path}`), stdout: "", stderr: "" };
  }
}

async function fixture(responder?: (command: string) => number) {
  const root = await mkdtemp(join(tmpdir(), "agenelf-change-ops-test-"));
  await mkdir(join(root, "local", "secrets"), { recursive: true });
  await mkdir(join(root, "data", "auth-decisions"), { recursive: true });
  await mkdir(join(root, "data", "ops-results"), { recursive: true });
  await mkdir(join(root, "data", "ops-locks"), { recursive: true });
  await mkdir(join(root, "data", "ops-events"), { recursive: true });
  await mkdir(join(root, "logs"), { recursive: true });
  await writeFile(join(root, "local", "secrets", "test_ed25519"), "test-key", { mode: 0o600 });
  await writeFile(join(root, "local", "secrets", "known_hosts"), "localhost ssh-ed25519 AAAATEST\n", { mode: 0o600 });
  await writeFile(join(root, "local", "servers.yaml"), `servers:\n  primary:\n    host: 127.0.0.1\n    port: 2222\n    username: agenelf\n    connect_timeout: 5\n    auth:\n      type: private_key\n      private_key: test_ed25519\n    known_hosts: known_hosts\n    allow_unknown_host_key: false\n    docker_command: docker\n    managed_root: /srv/agenelf\n    allowed_bind_roots: [/srv/data]\n    allowed_operations: [inspect, docker_ps, service_status, apt_update, compose_deploy, compose_down, service_restart, docker_install]\n    allowed_docker_operations: [get_docker_logs, inspect_docker_container, run_docker_check, restart_docker_container]\n    allowed_containers: [demo]\n    allowed_services: [nginx]\n`);
  const transport = new FakeTransport(responder);
  const catalog = new ServerCatalog(root);
  const runner = new ChangeOpsRunner(root, { catalog, transport });
  await runner.initialize();
  return { root, runner, transport, catalog };
}

async function approve(root: string, request: OperationRequest, decision: "approve" | "deny" = "approve") {
  await writeFile(join(root, "data", "auth-decisions", `${request.id}.json`), `${JSON.stringify({
    schema_version: 1,
    request_id: request.id,
    decision,
    fingerprint: request.fingerprint,
    decided_at: new Date().toISOString(),
    decided_by: "owner-test",
    reason: "test"
  }, null, 2)}\n`);
}

test("pending change request never connects SSH", async () => {
  const { root, runner, transport } = await fixture();
  const request = await new OperationQueue(root).submit({ capability: "server.operations", operation: "apt_update", target: "primary", risk: "change", summary: "apt" });
  const counts = await runner.processOnce();
  assert.equal(counts.pending, 1);
  assert.equal(transport.calls.length, 0);
  await assert.rejects(readFile(join(root, "data", "ops-results", `${request.id}.json`), "utf8"));
});

test("exact owner approval executes fixed service restart and persists replay events", async () => {
  const { root, runner, transport } = await fixture();
  const request = await new OperationQueue(root).submit({
    capability: "server.operations",
    operation: "service_restart",
    target: "primary",
    parameters: { service: "nginx" },
    risk: "change",
    summary: "restart nginx"
  });
  await approve(root, request);
  const counts = await runner.processOnce();
  assert.equal(counts.succeeded, 1);
  assert.equal(transport.calls.length, 1);
  assert.match(transport.calls[0].command, /^sudo -n systemctl restart 'nginx'/);
  const result = JSON.parse(await readFile(join(root, "data", "ops-results", `${request.id}.json`), "utf8"));
  assert.equal(result.status, "succeeded");
  const events = (await readFile(join(root, "data", "ops-events", `${request.id}.jsonl`), "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(events.map((event) => event.type), ["ops.runner.claimed", "ops.approval.checked", "ssh.started", "ssh.completed", "ops.result.persisted"]);
});

test("owner revocation before shared lock wins and prevents SSH", async () => {
  const { root, transport, catalog } = await fixture();
  const request = await new OperationQueue(root).submit({ capability: "server.operations", operation: "apt_update", target: "primary", risk: "change", summary: "apt" });
  await approve(root, request);
  class RevokingRunner extends ChangeOpsRunner {
    protected override async beforeLock(value: OperationRequest): Promise<void> {
      await approve(root, value, "deny");
    }
  }
  const runner = new RevokingRunner(root, { catalog, transport });
  await runner.initialize();
  const counts = await runner.processOnce();
  assert.equal(counts.blocked, 1);
  assert.equal(transport.calls.length, 0);
  const result = JSON.parse(await readFile(join(root, "data", "ops-results", `${request.id}.json`), "utf8"));
  assert.match(result.reason, /拒绝/);
});

test("tampered fingerprint and expired request fail closed without SSH", async () => {
  const { root, runner, transport } = await fixture();
  const queue = new OperationQueue(root);
  const tampered = await queue.submit({ capability: "docker.operations", operation: "restart_docker_container", target: "primary", parameters: { container: "demo", timeout_seconds: 2 }, risk: "change", summary: "restart" });
  await approve(root, tampered);
  const tamperedPath = join(root, "data", "ops-requests", `${tampered.id}.json`);
  const tamperedDocument = JSON.parse(await readFile(tamperedPath, "utf8"));
  tamperedDocument.parameters.timeout_seconds = 3;
  await writeFile(tamperedPath, `${JSON.stringify(tamperedDocument, null, 2)}\n`);
  const expired = await queue.submit({ capability: "server.operations", operation: "apt_update", target: "primary", risk: "change", summary: "expired" });
  await approve(root, expired);
  const expiredPath = join(root, "data", "ops-requests", `${expired.id}.json`);
  const expiredDocument = JSON.parse(await readFile(expiredPath, "utf8"));
  expiredDocument.expires_at = "2000-01-01T00:00:00.000Z";
  await writeFile(expiredPath, `${JSON.stringify(expiredDocument, null, 2)}\n`);
  const counts = await runner.processOnce();
  assert.equal(counts.failed, 1);
  assert.equal(counts.expired, 1);
  assert.equal(transport.calls.length, 0);
});

test("Compose redlines reject privileged, Docker Socket and unapproved host binds", async () => {
  const { catalog } = await fixture();
  const server = catalog.get("primary");
  assert.throws(() => validateComposeYaml("services:\n  bad:\n    image: demo\n    privileged: true\n", server), /privileged/);
  assert.throws(() => validateComposeYaml("services:\n  bad:\n    image: demo\n    volumes: [/var/run/docker.sock:/var/run/docker.sock]\n", server), /Docker Socket/);
  assert.throws(() => validateComposeYaml("services:\n  bad:\n    image: demo\n    volumes: [/etc:/etc:ro]\n", server), /未获允许/);
  assert.doesNotThrow(() => validateComposeYaml("services:\n  good:\n    image: demo\n    volumes: [/srv/data/demo:/data:ro]\n", server));
});

test("Compose deploy writes via stdin and rolls back after pull failure", async () => {
  const { root, runner, transport } = await fixture((command) => command.includes(" compose pull") ? 1 : 0);
  const composeYaml = "services:\n  app:\n    image: example/app:1\n";
  const request = await new OperationQueue(root).submit({
    capability: "server.operations",
    operation: "compose_deploy",
    target: "primary",
    parameters: { project: "demo", compose_yaml: composeYaml, pull: true },
    risk: "change",
    summary: "deploy demo"
  });
  await approve(root, request);
  const counts = await runner.processOnce();
  assert.equal(counts.failed, 1);
  const write = transport.calls.find((call) => call.kind === "write");
  assert.equal(write?.content, composeYaml);
  assert.equal(transport.calls.some((call) => call.command.includes("compose pull")), true);
  assert.equal(transport.calls.some((call) => call.command.includes(".agenelf-backups") && call.command.includes("compose up -d")), true);
  assert.equal(transport.calls.some((call) => call.command.includes(composeYaml)), false);
  const result = JSON.parse(await readFile(join(root, "data", "ops-results", `${request.id}.json`), "utf8"));
  assert.equal(result.status, "failed");
  assert.equal(result.commands.some((item: { phase: string }) => item.phase === "rollback"), true);
  const events = await readFile(join(root, "data", "ops-events", `${request.id}.jsonl`), "utf8");
  assert.match(events, /compose\.rollback\.started/);
  assert.match(events, /compose\.rollback\.completed/);
});

test("privileged Docker install is a fixed template with no model parameters", async () => {
  const { root, runner, transport } = await fixture();
  const request = await new OperationQueue(root).submit({ capability: "server.operations", operation: "docker_install", target: "primary", risk: "privileged", summary: "install docker" });
  await approve(root, request);
  const counts = await runner.processOnce();
  assert.equal(counts.succeeded, 1);
  assert.equal(transport.calls.length, 1);
  assert.equal(transport.calls[0].command, "sudo -n apt-get update && sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 && sudo -n systemctl enable --now docker && sudo -n docker version");
});
