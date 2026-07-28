import { createHash } from "node:crypto";
import { dirname, join, resolve, sep } from "node:path";
import { lstat, readFile, realpath } from "node:fs/promises";
import { appendLine, atomicWriteJson, readJson } from "./fs-store.ts";
import { randomId } from "./canonical.ts";
import { redactSensitiveText, sanitizeObject } from "./privacy.ts";
import {
  SelfUpgradeRunner,
  type SelfUpgradeOptions,
  type SelfUpgradeRequest
} from "./self-upgrade.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const REQUEST_RE = /^self-upgrade-[0-9a-f]{16}$/;

function now(): string { return new Date().toISOString(); }
function within(root: string, candidate: string): boolean {
  return candidate === root || candidate.startsWith(`${root}${sep}`);
}
async function sha256(path: string): Promise<string> {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}
async function nearestExisting(path: string): Promise<string> {
  let current = resolve(path);
  while (true) {
    try { await lstat(current); return current; }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      const parent = dirname(current);
      if (parent === current) throw new Error(`找不到可验证的父目录：${path}`);
      current = parent;
    }
  }
}
function validDate(value: JsonValue | undefined): boolean {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

export interface HardenedSelfUpgradeOptions extends SelfUpgradeOptions {
  trustedTestRunner?: (candidateRepo: string, baselineManifest: string) => Promise<JsonObject>;
}

/**
 * Production wrapper around the protocol-compatible SelfUpgradeRunner.
 *
 * Root tokens intentionally remain explicit for owner-authorized governance:
 * preflightRealPaths, validateAuthorizationDates, revalidateApprovedFilesAfterTests.
 */
export class HardenedSelfUpgradeRunner extends SelfUpgradeRunner {
  private activeRequest: SelfUpgradeRequest | null = null;
  private readonly trustedTestRunner?: (candidateRepo: string, baselineManifest: string) => Promise<JsonObject>;

  constructor(root: string, options: HardenedSelfUpgradeOptions = {}) {
    const { trustedTestRunner, testRunner: _ignored, ...baseOptions } = options;
    super(root, baseOptions);
    this.trustedTestRunner = trustedTestRunner;
  }

  private async persistPreflightFailure(request: SelfUpgradeRequest, error: unknown): Promise<string> {
    const reason = redactSensitiveText(
      error instanceof Error ? `${error.name}: ${error.message}` : String(error)
    ).slice(-8000);
    try {
      await atomicWriteJson(join(this.results, `${request.id}.json`), {
        schema_version: 1,
        id: request.id,
        status: "failed",
        finished_at: now(),
        error: reason
      }, true);
    } catch (writeError) {
      if ((writeError as NodeJS.ErrnoException).code === "EEXIST") return "done";
      throw writeError;
    }
    await appendLine(join(this.events, `${request.id}.jsonl`), JSON.stringify({
      schema_version: 1,
      id: randomId("uevt-", 20),
      upgrade_id: request.id,
      type: "upgrade.failed",
      origin: "runner",
      ts: now(),
      payload: sanitizeObject({ status: "failed", reason })
    }));
    return "failed";
  }

  private async validateAuthorizationDates(request: SelfUpgradeRequest): Promise<void> {
    const authRequest = await readJson<JsonObject | null>(
      join(this.root, "data", "auth-requests", `${request.candidate_auth_id}.json`),
      null
    );
    if (authRequest && !validDate(authRequest.expires_at)) {
      throw new Error("候选授权请求 expires_at 非法，按过期处理");
    }
    const decision = await readJson<JsonObject | null>(
      join(this.root, "data", "auth-decisions", `${request.candidate_auth_id}.json`),
      null
    );
    if (decision && !validDate(decision.expires_at)) {
      throw new Error("候选授权决定 expires_at 非法，按过期处理");
    }
  }

  private async assertRealPath(root: string, path: string, label: string, mustExist: boolean): Promise<void> {
    const rootReal = await realpath(root);
    if (mustExist) {
      const info = await lstat(path);
      if (!info.isFile() || info.isSymbolicLink()) throw new Error(`${label} 必须是普通文件`);
      const actual = await realpath(path);
      if (!within(rootReal, actual)) throw new Error(`${label} 通过父目录符号链接逃逸：${actual}`);
      return;
    }
    const ancestor = await nearestExisting(dirname(path));
    const actualParent = await realpath(ancestor);
    if (!within(rootReal, actualParent)) throw new Error(`${label} 父目录通过符号链接逃逸：${actualParent}`);
  }

  private async preflightRealPaths(request: SelfUpgradeRequest): Promise<void> {
    for (const record of request.changed_files) {
      const path = String(record.path ?? "");
      const candidate = resolve(this.candidateRoot, path);
      const target = resolve(this.targetRoot, path);
      await this.assertRealPath(this.candidateRoot, candidate, `候选 ${path}`, true);
      await this.assertRealPath(this.targetRoot, target, `目标 ${path}`, Boolean(record.before_sha256));
    }
  }

  private async revalidateApprovedFilesAfterTests(): Promise<void> {
    const request = this.activeRequest;
    if (!request) throw new Error("缺少当前 Self-upgrade 请求上下文");
    await this.preflightRealPaths(request);
    for (const record of request.changed_files) {
      const path = String(record.path ?? "");
      const candidate = resolve(this.candidateRoot, path);
      const actual = await sha256(candidate);
      if (actual !== String(record.after_sha256 ?? "")) {
        throw new Error(`完整测试后候选文件发生变化：${path}`);
      }
    }
  }

  override async runTrustedTests(candidateRepo: string, baselineManifest: string): Promise<JsonObject> {
    const report = this.trustedTestRunner
      ? await this.trustedTestRunner(candidateRepo, baselineManifest)
      : await super.runTrustedTests(candidateRepo, baselineManifest);
    await this.revalidateApprovedFilesAfterTests();
    return report;
  }

  override async processRequest(path: string): Promise<string> {
    const request = await readJson<SelfUpgradeRequest | null>(path, null);
    if (!request || !REQUEST_RE.test(String(request.id ?? ""))) return super.processRequest(path);
    if (await readJson<JsonObject | null>(join(this.results, `${request.id}.json`), null)) return "done";
    try {
      await this.validateAuthorizationDates(request);
      await this.preflightRealPaths(request);
    } catch (error) {
      return this.persistPreflightFailure(request, error);
    }
    this.activeRequest = request;
    try {
      return await super.processRequest(path);
    } finally {
      this.activeRequest = null;
    }
  }
}
