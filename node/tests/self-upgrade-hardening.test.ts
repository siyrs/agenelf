import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { pythonCanonical } from "../packages/core/src/owner-approval.ts";
import { HardenedSelfUpgradeRunner } from "../packages/core/src/self-upgrade-hardening.ts";
import type { SelfUpgradeRequest } from "../packages/core/src/self-upgrade.ts";
import type { JsonObject, JsonValue } from "../packages/core/src/types.ts";

function digest(value: JsonValue): string {
  return createHash("sha256").update(pythonCanonical(value), "utf8").digest("hex");
}
function bytes(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}
async function writeJson(path: string, value: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`);
}

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-self-upgrade-hardening-"));
  const candidateRoot = join(root, "app-tmp", "repo");
  const targetRoot = join(root, "upgrade-target");
  const relativePath = "docs/fixture.md";
  const candidatePath = join(candidateRoot, relativePath);
  const targetPath = join(targetRoot, relativePath);
  await mkdir(dirname(candidatePath), { recursive: true });
  await mkdir(dirname(targetPath), { recursive: true });
  await writeFile(candidatePath, "new\n");
  await writeFile(targetPath, "old\n");

  const sessionId = "upgrade-20260728-140000-acde1234";
  const requestId = "self-upgrade-3333333333333333";
  const intentAuthId = "auth-hardening-intent";
  const candidateAuthId = "auth-hardening-candidate";
  const changed: JsonObject[] = [{
    path: relativePath,
    before_sha256: bytes("old\n"),
    after_sha256: bytes("new\n"),
    changed_lines: 2,
    created: false
  }];
  const candidateTree = digest({ [relativePath]: bytes("new\n") });
  const evidenceDir = join(root, "data", "authorized-upgrades", sessionId);
  const baselinePath = join(evidenceDir, "baseline-manifest.json");
  const reportPath = join(evidenceDir, "test-report.json");
  await writeJson(baselinePath, { [relativePath]: bytes("old\n") });
  await writeJson(reportPath, { status: "passed" });
  const binding: JsonObject = {
    schema_version: 1,
    kind: "owner_authorized_self_upgrade_candidate",
    session_id: sessionId,
    intent_auth_id: intentAuthId,
    goal_sha256: bytes("hardening"),
    scopes: ["docs"],
    allowed_paths: ["docs/"],
    changed_files: changed,
    candidate_tree_sha256: candidateTree,
    test_report_sha256: bytes(await readFile(reportPath)),
    baseline_manifest_sha256: bytes(await readFile(baselinePath))
  };
  await writeJson(join(root, "data", "authorized-upgrades", `${sessionId}.json`), {
    schema_version: 1,
    id: sessionId,
    status: "apply_queued",
    plan: { allowed_paths: ["docs/"] },
    intent_auth_id: intentAuthId,
    candidate_auth_id: candidateAuthId,
    intent_consumed: true,
    changed_file_records: changed,
    candidate_binding: binding,
    candidate_digest: candidateTree,
    baseline_manifest_path: baselinePath,
    test_report_path: reportPath
  });
  const payload: JsonObject = {
    schema_version: 1,
    session_id: sessionId,
    intent_auth_id: intentAuthId,
    candidate_auth_id: candidateAuthId,
    candidate_binding: binding,
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
    binding,
    fingerprint: digest(binding),
    expires_at: expires,
    required_approvers: 1
  });
  await writeJson(join(root, "data", "auth-decisions", `${candidateAuthId}.json`), {
    request_id: candidateAuthId,
    decision: "approve",
    fingerprint: digest(binding),
    expires_at: expires,
    decided_by: "owner-test"
  });
  return {
    root, candidateRoot, targetRoot, candidatePath, targetPath,
    request, candidateAuthId
  };
}

test("invalid authorization date is terminally rejected", async () => {
  const value = await fixture();
  const decisionPath = join(value.root, "data", "auth-decisions", `${value.candidateAuthId}.json`);
  const decision = JSON.parse(await readFile(decisionPath, "utf8"));
  decision.expires_at = "not-a-date";
  await writeJson(decisionPath, decision);
  const runner = new HardenedSelfUpgradeRunner(value.root, {
    candidateRoot: value.candidateRoot,
    targetRoot: value.targetRoot,
    trustedTestRunner: async () => ({ status: "passed" })
  });
  await runner.initialize();
  assert.equal((await runner.processOnce()).failed, 1);
  assert.equal(await readFile(value.targetPath, "utf8"), "old\n");
  await assert.rejects(readFile(join(value.root, "data", "auth-consumed", `${value.candidateAuthId}.json`), "utf8"));
  const result = await readFile(join(value.root, "data", "self-upgrade-results", `${value.request.id}.json`), "utf8");
  assert.match(result, /expires_at 非法/);
});

test("target parent symlink escape is rejected before candidate verification", async () => {
  const value = await fixture();
  const outside = join(value.root, "outside-docs");
  await mkdir(outside, { recursive: true });
  await writeFile(join(outside, "fixture.md"), "outside-old\n");
  await rm(join(value.targetRoot, "docs"), { recursive: true, force: true });
  await symlink(outside, join(value.targetRoot, "docs"));
  const runner = new HardenedSelfUpgradeRunner(value.root, {
    candidateRoot: value.candidateRoot,
    targetRoot: value.targetRoot,
    trustedTestRunner: async () => ({ status: "passed" })
  });
  await runner.initialize();
  assert.equal((await runner.processOnce()).failed, 1);
  assert.equal(await readFile(join(outside, "fixture.md"), "utf8"), "outside-old\n");
  await assert.rejects(readFile(join(value.root, "data", "auth-consumed", `${value.candidateAuthId}.json`), "utf8"));
});

test("candidate mutation during trusted tests is caught before authorization consume", async () => {
  const value = await fixture();
  const runner = new HardenedSelfUpgradeRunner(value.root, {
    candidateRoot: value.candidateRoot,
    targetRoot: value.targetRoot,
    trustedTestRunner: async () => {
      await writeFile(value.candidatePath, "mutated-by-test\n");
      return { status: "passed" };
    }
  });
  await runner.initialize();
  assert.equal((await runner.processOnce()).failed, 1);
  assert.equal(await readFile(value.targetPath, "utf8"), "old\n");
  await assert.rejects(readFile(join(value.root, "data", "auth-consumed", `${value.candidateAuthId}.json`), "utf8"));
  const result = await readFile(join(value.root, "data", "self-upgrade-results", `${value.request.id}.json`), "utf8");
  assert.match(result, /完整测试后候选文件发生变化/);
});
