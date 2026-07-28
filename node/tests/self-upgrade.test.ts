import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, symlink, writeFile } from "node:fs/promises";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pythonCanonical } from "../packages/core/src/owner-approval.ts";
import {
  SelfUpgradeRunner,
  checkAuthorization,
  type SelfUpgradeRequest
} from "../packages/core/src/self-upgrade.ts";
import type { JsonObject, JsonValue } from "../packages/core/src/types.ts";

function digest(value: JsonValue): string {
  return createHash("sha256").update(pythonCanonical(value), "utf8").digest("hex");
}
function bytes(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}
async function writeJson(path: string, value: unknown) {
  await mkdir(join(path, ".."), { recursive: true });
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`);
}

interface FixtureOptions {
  files?: Array<{ path: string; before: string; after: string }>;
  decision?: "approve" | "deny" | "pending";
  requiredApprovers?: number;
}

async function fixture(options: FixtureOptions = {}) {
  const root = await mkdtemp(join(tmpdir(), "agenelf-self-upgrade-test-"));
  const candidateRoot = join(root, "app-tmp", "repo");
  const targetRoot = join(root, "upgrade-target");
  const sessionId = "upgrade-20260728-120000-abcdef12";
  const requestId = "self-upgrade-1111111111111111";
  const intentAuthId = "auth-intent111111";
  const candidateAuthId = "auth-candidate111";
  const files = options.files ?? [{ path: "docs/fixture.md", before: "old\n", after: "new\n" }];
  const changed: JsonObject[] = [];
  const candidateManifest: JsonObject = {};
  const baselineManifest: JsonObject = {};

  for (const item of files) {
    const candidatePath = join(candidateRoot, item.path);
    const targetPath = join(targetRoot, item.path);
    await mkdir(join(candidatePath, ".."), { recursive: true });
    await mkdir(join(targetPath, ".."), { recursive: true });
    await writeFile(candidatePath, item.after);
    await writeFile(targetPath, item.before);
    candidateManifest[item.path] = bytes(item.after);
    baselineManifest[item.path] = bytes(item.before);
    changed.push({
      path: item.path,
      before_sha256: bytes(item.before),
      after_sha256: bytes(item.after),
      changed_lines: 2,
      created: false
    });
  }

  const evidenceDir = join(root, "data", "authorized-upgrades", sessionId);
  await mkdir(evidenceDir, { recursive: true });
  const baselinePath = join(evidenceDir, "baseline-manifest.json");
  const testReportPath = join(evidenceDir, "test-report.json");
  await writeJson(baselinePath, baselineManifest);
  await writeJson(testReportPath, { status: "passed", fixture: true });
  const candidateTree = digest(candidateManifest);
  const candidateBinding: JsonObject = {
    schema_version: 1,
    kind: "owner_authorized_self_upgrade_candidate",
    session_id: sessionId,
    intent_auth_id: intentAuthId,
    goal_sha256: bytes("fixture"),
    scopes: ["docs"],
    allowed_paths: ["docs/"],
    changed_files: changed,
    candidate_tree_sha256: candidateTree,
    test_report_sha256: bytes(await readFile(testReportPath)),
    baseline_manifest_sha256: bytes(await readFile(baselinePath))
  };
  const session: JsonObject = {
    schema_version: 1,
    id: sessionId,
    status: "apply_queued",
    goal: "fixture",
    plan: { allowed_paths: ["docs/"] },
    intent_auth_id: intentAuthId,
    candidate_auth_id: candidateAuthId,
    intent_consumed: true,
    changed_file_records: changed,
    candidate_binding: candidateBinding,
    candidate_digest: candidateTree,
    baseline_manifest_path: baselinePath,
    test_report_path: testReportPath
  };
  await writeJson(join(root, "data", "authorized-upgrades", `${sessionId}.json`), session);
  const payload: JsonObject = {
    schema_version: 1,
    session_id: sessionId,
    intent_auth_id: intentAuthId,
    candidate_auth_id: candidateAuthId,
    candidate_binding: candidateBinding,
    candidate_digest: candidateTree,
    changed_files: changed,
    candidate_repo: candidateRoot
  };
  const request: SelfUpgradeRequest = {
    id: requestId,
    created_at: new Date().toISOString(),
    ...payload,
    fingerprint: digest(payload)
  } as SelfUpgradeRequest;
  await writeJson(join(root, "data", "self-upgrade-requests", `${requestId}.json`), request);
  const expires = new Date(Date.now() + 60_000).toISOString();
  await writeJson(join(root, "data", "auth-requests", `${candidateAuthId}.json`), {
    schema_version: 2,
    id: candidateAuthId,
    binding: candidateBinding,
    fingerprint: digest(candidateBinding),
    expires_at: expires,
    required_approvers: options.requiredApprovers ?? 1
  });
  if ((options.decision ?? "approve") !== "pending") {
    const required = options.requiredApprovers ?? 1;
    await writeJson(join(root, "data", "auth-decisions", `${candidateAuthId}.json`), {
      request_id: candidateAuthId,
      decision: options.decision ?? "approve",
      fingerprint: digest(candidateBinding),
      expires_at: expires,
      approvals: required > 1
        ? Array.from({ length: required }, (_, index) => ({ decided_by: `owner-${index + 1}` }))
        : []
    });
  }
  const runner = new SelfUpgradeRunner(root, {
    candidateRoot,
    targetRoot,
    testRunner: async () => ({ status: "passed", fixture: true })
  });
  await runner.initialize();
  return { root, candidateRoot, targetRoot, runner, request, session, candidateBinding, candidateAuthId, files };
}

test("exact approved candidate is tested, consumed, backed up and atomically applied", async () => {
  const { root, targetRoot, runner, request } = await fixture();
  const counts = await runner.processOnce();
  assert.equal(counts.succeeded, 1);
  assert.equal(await readFile(join(targetRoot, "docs", "fixture.md"), "utf8"), "new\n");
  const result = JSON.parse(await readFile(join(root, "data", "self-upgrade-results", `${request.id}.json`), "utf8"));
  assert.equal(result.status, "succeeded");
  assert.equal(result.restart_required, false);
  assert.deepEqual(result.changed_files, ["docs/fixture.md"]);
  assert.equal(await readFile(join(result.backup_dir, "files", "docs", "fixture.md"), "utf8"), "old\n");
  assert.ok(await readFile(join(root, "data", "auth-consumed", "auth-candidate111.json"), "utf8"));
  const events = (await readFile(join(root, "data", "self-upgrade-events", `${request.id}.jsonl`), "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(events.map((event) => event.type), [
    "upgrade.runner.claimed", "upgrade.authorization.checked", "upgrade.candidate.verified",
    "upgrade.tests.started", "upgrade.tests.completed", "upgrade.authorization.consumed",
    "upgrade.backup.created", "upgrade.file.applied", "upgrade.result.persisted"
  ]);
  const rerun = await runner.processOnce();
  assert.equal(rerun.done, 1);
});

test("pending authorization creates no lock, result or target mutation", async () => {
  const { root, targetRoot, runner, request } = await fixture({ decision: "pending" });
  const counts = await runner.processOnce();
  assert.equal(counts.pending, 1);
  assert.equal(await readFile(join(targetRoot, "docs", "fixture.md"), "utf8"), "old\n");
  await assert.rejects(readFile(join(root, "data", "self-upgrade-results", `${request.id}.json`), "utf8"));
});

test("owner denial introduced before lock wins", async () => {
  const base = await fixture();
  class DenyingRunner extends SelfUpgradeRunner {
    protected override async beforeLock(): Promise<void> {
      await writeJson(join(base.root, "data", "auth-decisions", `${base.candidateAuthId}.json`), {
        request_id: base.candidateAuthId,
        decision: "deny",
        fingerprint: digest(base.candidateBinding),
        expires_at: new Date(Date.now() + 60_000).toISOString()
      });
    }
  }
  const runner = new DenyingRunner(base.root, {
    candidateRoot: base.candidateRoot,
    targetRoot: base.targetRoot,
    testRunner: async () => ({ status: "passed" })
  });
  await runner.initialize();
  const counts = await runner.processOnce();
  assert.equal(counts.failed, 1);
  assert.equal(await readFile(join(base.targetRoot, "docs", "fixture.md"), "utf8"), "old\n");
  await assert.rejects(readFile(join(base.root, "data", "auth-consumed", `${base.candidateAuthId}.json`), "utf8"));
});

test("candidate tree or stale target changes after approval fail closed", async () => {
  const tampered = await fixture();
  await writeFile(join(tampered.candidateRoot, "docs", "fixture.md"), "tampered\n");
  assert.equal((await tampered.runner.processOnce()).failed, 1);
  assert.equal(await readFile(join(tampered.targetRoot, "docs", "fixture.md"), "utf8"), "old\n");

  const stale = await fixture();
  await writeFile(join(stale.targetRoot, "docs", "fixture.md"), "changed-outside-runner\n");
  assert.equal((await stale.runner.processOnce()).failed, 1);
  assert.equal(await readFile(join(stale.targetRoot, "docs", "fixture.md"), "utf8"), "changed-outside-runner\n");
});

test("distinct dual approvals are required and exact binding is enforced", async () => {
  const dual = await fixture({ requiredApprovers: 2 });
  assert.equal((await checkAuthorization(dual.root, dual.candidateAuthId, dual.candidateBinding)).state, "approved");
  const decisionPath = join(dual.root, "data", "auth-decisions", `${dual.candidateAuthId}.json`);
  const decision = JSON.parse(await readFile(decisionPath, "utf8"));
  decision.approvals = [{ decided_by: "same" }, { decided_by: "same" }];
  await writeJson(decisionPath, decision);
  assert.equal((await checkAuthorization(dual.root, dual.candidateAuthId, dual.candidateBinding)).state, "pending");
  decision.approvals = [{ decided_by: "owner-1" }, { decided_by: "owner-2" }];
  await writeJson(decisionPath, decision);
  const wrong = { ...dual.candidateBinding, candidate_tree_sha256: "0".repeat(64) };
  assert.equal((await checkAuthorization(dual.root, dual.candidateAuthId, wrong)).state, "binding_mismatch");
});

test("new redlines and candidate symlinks are rejected", async () => {
  const redline = await fixture({ files: [{ path: "docs/fixture.md", before: "safe\n", after: "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456\n" }] });
  assert.equal((await redline.runner.processOnce()).failed, 1);
  assert.equal(await readFile(join(redline.targetRoot, "docs", "fixture.md"), "utf8"), "safe\n");

  const linked = await fixture();
  await writeFile(join(linked.candidateRoot, "docs", "outside.md"), "outside\n");
  await writeFile(join(linked.candidateRoot, "docs", "fixture.md"), "temporary\n");
  await (await import("node:fs/promises")).unlink(join(linked.candidateRoot, "docs", "fixture.md"));
  await symlink("outside.md", join(linked.candidateRoot, "docs", "fixture.md"));
  assert.equal((await linked.runner.processOnce()).failed, 1);
});

test("partial application failure rolls back files in reverse order", async () => {
  const base = await fixture({ files: [
    { path: "docs/one.md", before: "one-old\n", after: "one-new\n" },
    { path: "docs/two.md", before: "two-old\n", after: "two-new\n" }
  ] });
  class FailingRunner extends SelfUpgradeRunner {
    protected override async beforeApplyFile(_path: string, index: number): Promise<void> {
      if (index === 1) throw new Error("injected write failure");
    }
  }
  const runner = new FailingRunner(base.root, {
    candidateRoot: base.candidateRoot,
    targetRoot: base.targetRoot,
    testRunner: async () => ({ status: "passed" })
  });
  await runner.initialize();
  assert.equal((await runner.processOnce()).failed, 1);
  assert.equal(await readFile(join(base.targetRoot, "docs", "one.md"), "utf8"), "one-old\n");
  assert.equal(await readFile(join(base.targetRoot, "docs", "two.md"), "utf8"), "two-old\n");
  const events = await readFile(join(base.root, "data", "self-upgrade-events", `${base.request.id}.jsonl`), "utf8");
  assert.match(events, /upgrade\.rollback\.started/);
  assert.match(events, /upgrade\.rollback\.completed/);
});
