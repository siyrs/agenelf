import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { NodeRepairRunner, type RepairRequest } from "../packages/core/src/repair.ts";
import { sha256 } from "../packages/core/src/canonical.ts";
import type { JsonObject, JsonValue } from "../packages/core/src/types.ts";

const PATCH = `diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def value():
-    return "old"
+    return "new"
`;

function run(cwd: string, argv: string[]): string {
  const process = spawnSync(argv[0], argv.slice(1), {
    cwd,
    encoding: "utf8",
    env: { ...globalThis.process.env, GIT_AUTHOR_NAME: "Agenelf CI", GIT_AUTHOR_EMAIL: "ci@example.invalid", GIT_COMMITTER_NAME: "Agenelf CI", GIT_COMMITTER_EMAIL: "ci@example.invalid" }
  });
  assert.equal(process.status, 0, `${argv.join(" ")} failed:\n${process.stdout}\n${process.stderr}`);
  return String(process.stdout).trim();
}

function rawSha(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function requestFor(patch: string, expectedBase = "", id = "repair-1111111111111111"): RepairRequest {
  const parameters = {
    test_profile: "python-unittest",
    patch_sha256: rawSha(patch),
    patch_bytes: Buffer.byteLength(patch, "utf8"),
    expected_base: expectedBase
  };
  const payload: JsonObject = {
    capability: "code.repair",
    operation: "apply_patch_and_test",
    target: "demo",
    parameters
  };
  return {
    schema_version: 1,
    id,
    capability: "code.repair",
    operation: "apply_patch_and_test",
    target: "demo",
    parameters,
    risk: "read",
    summary: "Repair app value in isolated worktree",
    patch,
    fingerprint: sha256(payload as unknown as JsonValue),
    created_at: new Date().toISOString(),
    created_by: "agenelf-node-agent"
  };
}

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-repair-test-"));
  const source = join(root, "code-workspaces", "demo");
  await mkdir(join(source, "tests"), { recursive: true });
  await mkdir(join(root, "local"), { recursive: true });
  await mkdir(join(root, "data", "repair-requests"), { recursive: true });
  await mkdir(join(root, "data", "repair-results"), { recursive: true });
  await mkdir(join(root, "data", "repair-locks"), { recursive: true });
  await mkdir(join(root, "data", "repair-events"), { recursive: true });
  await mkdir(join(root, "repair-space"), { recursive: true });
  await mkdir(join(root, "logs"), { recursive: true });
  await writeFile(join(source, "app.py"), 'def value():\n    return "old"\n');
  await writeFile(join(source, "tests", "test_app.py"), 'import unittest\nfrom app import value\n\nclass ValueTest(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value(), "new")\n');
  run(source, ["git", "init", "-q"]);
  run(source, ["git", "add", "."]);
  run(source, ["git", "commit", "-q", "-m", "initial"]);
  const base = run(source, ["git", "rev-parse", "HEAD"]);
  await writeFile(join(root, "local", "repositories.yaml"), `schema_version: 1
repositories:
  demo:
    source_dir: demo
    default_test_profile: python-unittest
    allowed_test_profiles: [python-unittest]
    protected_paths: [policy/, .github/workflows/]
    max_patch_files: 10
    max_patch_bytes: 262144
test_profiles:
  python-unittest:
    commands:
      - [python, -m, unittest, discover, -s, tests, -v]
    timeout_seconds: 120
`);
  return { root, source, base };
}

async function writeRequest(root: string, request: RepairRequest): Promise<string> {
  const path = join(root, "data", "repair-requests", `${request.id}.json`);
  await writeFile(path, `${JSON.stringify(request, null, 2)}\n`);
  return path;
}

test("raw patch and canonical request hashes match Python protocol", () => {
  const request = requestFor(PATCH);
  assert.equal(request.parameters.patch_sha256, "fe80cfc2a0e093c08cab048b5dc1cd2af3518b0ff6709863f6e1ba9079e03e8d");
  assert.equal(request.parameters.patch_bytes, 121);
  assert.equal(request.fingerprint, "e5c0eb2aa71df09315ae54f9368e693b58d307d8b5390ce7158486027b1503b6");
});

test("repair applies patch and tests only in isolated artifact", async () => {
  const { root, source, base } = await fixture();
  const request = requestFor(PATCH, base);
  await writeRequest(root, request);
  const runner = new NodeRepairRunner(root);
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), { succeeded: 1 });
  assert.equal(await readFile(join(source, "app.py"), "utf8"), 'def value():\n    return "old"\n');
  assert.equal(run(source, ["git", "status", "--porcelain"]), "");
  assert.equal(await readFile(join(root, "repair-space", request.id, "worktree", "app.py"), "utf8"), 'def value():\n    return "new"\n');
  const result = JSON.parse(await readFile(join(root, "data", "repair-results", `${request.id}.json`), "utf8"));
  assert.equal(result.status, "succeeded");
  assert.equal(result.base_commit, base);
  assert.deepEqual(result.changed_files, ["app.py"]);
  assert.equal(result.source_repository_modified, false);
  assert.equal(result.committed, false);
  assert.equal(result.pushed, false);
  assert.equal(result.merged, false);
  assert.equal(result.commands.some((item: JsonObject) => item.phase === "test" && item.exit_code === 0), true);
  const events = (await readFile(join(root, "data", "repair-events", `${request.id}.jsonl`), "utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(events[0].type, "repair.runner.claimed");
  assert.equal(events.some((event) => event.type === "repair.clone.started"), true);
  assert.equal(events.at(-1).type, "repair.result.persisted");
});

test("expected base mismatch blocks before patch apply", async () => {
  const { root } = await fixture();
  const request = requestFor(PATCH, "abcdef0", "repair-2222222222222222");
  await writeRequest(root, request);
  const runner = new NodeRepairRunner(root);
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), { blocked: 1 });
  const result = JSON.parse(await readFile(join(root, "data", "repair-results", `${request.id}.json`), "utf8"));
  assert.equal(result.status, "blocked");
  assert.match(result.summary, /expected_base/);
  assert.equal(result.commands.some((item: JsonObject) => item.phase === "patch_apply"), false);
});

test("tampered patch digest is blocked before source clone", async () => {
  const { root } = await fixture();
  const request = requestFor(PATCH, "", "repair-3333333333333333");
  request.parameters.patch_sha256 = "0".repeat(64);
  await writeRequest(root, request);
  const runner = new NodeRepairRunner(root);
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), { blocked: 1 });
  const result = JSON.parse(await readFile(join(root, "data", "repair-results", `${request.id}.json`), "utf8"));
  assert.match(result.summary, /摘要/);
  assert.equal(result.commands.length, 0);
});

test("protected workflow patch is rejected", async () => {
  const { root } = await fixture();
  const patch = `diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
new file mode 100644
--- /dev/null
+++ b/.github/workflows/ci.yml
@@ -0,0 +1 @@
+name: unsafe
`;
  const request = requestFor(patch, "", "repair-4444444444444444");
  await writeRequest(root, request);
  const runner = new NodeRepairRunner(root);
  await runner.initialize();
  assert.deepEqual(await runner.processOnce(), { blocked: 1 });
  const result = JSON.parse(await readFile(join(root, "data", "repair-results", `${request.id}.json`), "utf8"));
  assert.match(result.summary, /受保护路径/);
});
