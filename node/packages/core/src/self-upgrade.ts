import { createHash, randomUUID } from "node:crypto";
import {
  chmod,
  lstat,
  mkdir,
  open,
  readFile,
  readdir,
  rename,
  rm,
  stat,
  unlink,
  writeFile
} from "node:fs/promises";
import { spawn } from "node:child_process";
import { dirname, join, relative, resolve, sep } from "node:path";
import { appendLine, atomicWriteJson, readJson } from "./fs-store.ts";
import { pythonCanonical } from "./owner-approval.ts";
import { randomId } from "./canonical.ts";
import { sanitizeObject, redactSensitiveText } from "./privacy.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const REQUEST_RE = /^self-upgrade-[0-9a-f]{16}$/;
const SESSION_RE = /^upgrade-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$/;
const AUTH_RE = /^[A-Za-z0-9._-]+$/;
const MAX_OUTPUT = 60_000;
const FORBIDDEN_PREFIXES = [
  ".git/", "local/", "data/", "logs/", "workspace/", "app-tmp/", "app-space/",
  "code-workspaces/", "repair-space/", "secrets/"
];
const FORBIDDEN_EXACT = new Set([".env", ".ops-runner.env"]);
const ALLOWED_SUFFIXES = new Set([
  ".py", ".sh", ".ps1", ".yaml", ".yml", ".json", ".toml", ".md", ".txt",
  ".ts", ".mts", ".cts"
]);
const ALLOWED_BASENAMES = new Set([
  "Dockerfile", "Dockerfile.node", "Dockerfile.control-plane", "Dockerfile.ops-read",
  "Dockerfile.ops-change", "Dockerfile.repair", "Makefile", "README.md", "compose.yaml",
  "compose.override.yaml", "docker-compose.yml", "docker-compose.python.yml",
  "docker-compose.node-approval.yml", "package.json", "package-lock.json", ".node-version",
  ".env.example", ".ops-runner.env.example", ".gitignore", ".gitleaks.toml"
]);
const SKIP_DIRECTORIES = new Set(["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]);

const REDLINE_PATTERNS: Array<[string, RegExp]> = [
  ["Docker Socket", /\/var\/run\/docker\.sock|docker\.sock/i],
  ["新增凭据读取", /local\/secrets|(?:read_text|read_bytes|open)\([^\n]{0,180}(?:\.env|approval\/key)|(?:\.env|approval\/key)[^\n]{0,180}(?:read_text|read_bytes|open)|AGENELF_APPROVAL_KEY[^\n]{0,180}(?:read|open)|auth-decisions[^\n]{0,180}(?:write_text|write_bytes|open\([^)]*['"]w)/i],
  ["自我批准", /self[_ -]?approve|自动批准|伪造授权|forge[_ -]?owner|decision\s*=\s*['"]approve['"]/i],
  ["审计破坏", /(?:unlink|remove|rmtree|truncate)[^\n]{0,160}(?:audit|auth-decisions|promotion-history|self-upgrade-results)/i],
  ["测试或门禁绕过", /monkey.?patch[^\n]{0,160}(?:test|gate|policy)|disable[^\n]{0,100}(?:test|gate|audit|policy)|skip[^\n]{0,100}(?:governance|security|existing[_ -]?test)/i],
  ["危险远程脚本", /(?:curl|wget)[^\n|]{0,240}\|\s*(?:sudo\s+)?(?:ba)?sh\b/i],
  ["直接主分支发布", /git[^\n]{0,160}(?:push|merge)[^\n]{0,160}\bmain\b/i],
  ["Node 任意 Shell", /(?:from\s+['"]node:child_process['"]|require\(['"](?:node:)?child_process['"]\))[\s\S]{0,1600}(?:\bexec(?:Sync)?\s*\(|\bspawn(?:Sync)?\s*\([^\n]{0,360}\bshell\s*:\s*true)/i],
  ["Node 动态代码执行", /\b(?:eval|Function)\s*\(|\bvm\.(?:runIn|compileFunction)/i],
  ["关闭 TLS 校验", /NODE_TLS_REJECT_UNAUTHORIZED\s*[:=]\s*['"]?0/i],
  ["npm 生命周期脚本", /['"](?:preinstall|install|postinstall|prepublish|prepublishOnly|prepare)['"]\s*:/i],
  ["明显 API Key", /sk-[A-Za-z0-9_-]{20,}/]
];

const REQUIRED_TOKENS: Record<string, string[]> = {
  "policy/safety-constraints.v1.yaml": [
    "owner_authorized_upgrade:",
    "owner_authorization_cannot_be_generated_by_model_output",
    "no_self_approval_or_forged_owner_decision",
    "no_access_to_env_local_secrets_ssh_keys_or_approval_key",
    "no_test_gate_policy_or_audit_weakening_to_force_success",
    "no_direct_push_or_merge_main_from_autonomous_runtime"
  ],
  "scripts/validate_governance.py": [
    "REQUIRED_UPGRADE_REDLINE", "validate_owner_authorized_upgrade",
    "authorized_upgrade_runner_isolated", "backup_and_rollback_evidence_archived"
  ],
  "scripts/self_upgrade_runner.py": [
    "verify_candidate", "rerun_tests", "backup_targets", "rollback", "consume_auth"
  ],
  "scripts/run_authorized_upgrade_tests.py": [
    "verify_existing_tests", "validate_governance.py", "unittest"
  ],
  "app/core/authorized_upgrade.py": [
    "_PERMANENTLY_FORBIDDEN_PREFIXES", "_request_candidate_approval",
    "candidate_tree_sha256", "scan_redlines"
  ],
  "app/core/node_upgrade_policy.py": [
    "_NODE_SCOPES", "_prepare_changes", "_validate_node_syntax", "_FORBIDDEN_LIFECYCLE_SCRIPTS"
  ],
  "app/core/cli_approval.py": [
    "parse_owner_decision", "submit_owner_command", "_advance_upgrade_after_approval"
  ],
  "app/skills/authorized_self_upgrade.py": [
    "request_authorized_self_upgrade", "continue_authorized_self_upgrade", "_ORDINARY_SANDBOX_PROTECTED"
  ],
  "app/tests/test_node_candidate_contract.py": [
    ".agenelf-evolution-workspace.json", "\"ci\", \"--ignore-scripts\"", "\"run\", \"test:node\""
  ],
  "Dockerfile.control-plane": [
    "FROM node:24.18.0-bookworm-slim AS node-runtime", "FROM python:3.12-slim",
    "npm_config_ignore_scripts=true", "USER agenelf"
  ],
  "node/packages/core/src/self-upgrade.ts": [
    "verifyCandidate", "runTrustedTests", "consumeAuthorization", "backupTargets", "rollback"
  ]
};

export interface SelfUpgradeRequest extends JsonObject {
  id: string;
  schema_version: number;
  session_id: string;
  intent_auth_id: string;
  candidate_auth_id: string;
  candidate_binding: JsonObject;
  candidate_digest: string;
  changed_files: JsonObject[];
  candidate_repo: string;
  fingerprint: string;
  created_at: string;
}

export interface AuthorizationCheck {
  state: "approved" | "pending" | "denied" | "expired" | "used" | "not_found" | "binding_mismatch";
  request?: JsonObject;
}

export interface SelfUpgradeOptions {
  candidateRoot?: string;
  targetRoot?: string;
  testRunner?: (candidateRepo: string, baselineManifest: string) => Promise<JsonObject>;
}

function now(): string { return new Date().toISOString(); }
function pythonDigest(value: JsonValue): string {
  return createHash("sha256").update(pythonCanonical(value), "utf8").digest("hex");
}
async function fileSha256(path: string): Promise<string> {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}
function isObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
function normalizedRelative(value: unknown): string {
  const raw = String(value ?? "").trim().replaceAll("\\", "/");
  if (!raw || raw.startsWith("/")) throw new Error("升级文件必须是仓库相对路径");
  const parts = raw.split("/").filter((item) => item && item !== ".");
  if (!parts.length || parts.includes("..")) throw new Error(`升级路径逃逸：${raw}`);
  const normalized = parts.join("/");
  if (FORBIDDEN_EXACT.has(normalized) || FORBIDDEN_PREFIXES.some((prefix) => normalized === prefix.slice(0, -1) || normalized.startsWith(prefix))) {
    throw new Error(`路径属于永久红线：${normalized}`);
  }
  return normalized;
}
function pathAllowed(path: string, allowed: JsonValue | undefined): boolean {
  if (!Array.isArray(allowed)) return false;
  return allowed.some((raw) => {
    const rule = String(raw ?? "").replaceAll("\\", "/").replace(/^\.\//, "");
    return rule.endsWith("/") ? path.startsWith(rule) : path === rule;
  });
}
function validateRepoPath(value: unknown, allowed: JsonValue | undefined): string {
  const normalized = normalizedRelative(value);
  if (!pathAllowed(normalized, allowed)) throw new Error(`路径超出主人批准范围：${normalized}`);
  const name = normalized.split("/").at(-1) ?? "";
  const dot = name.lastIndexOf(".");
  const suffix = dot >= 0 ? name.slice(dot).toLowerCase() : "";
  if (!ALLOWED_SUFFIXES.has(suffix) && !ALLOWED_BASENAMES.has(name)) throw new Error(`不支持的升级文件类型：${normalized}`);
  return normalized;
}
function within(root: string, path: string): boolean {
  const base = resolve(root);
  const candidate = resolve(path);
  return candidate === base || candidate.startsWith(`${base}${sep}`);
}
async function regularFile(path: string): Promise<boolean> {
  try { const info = await lstat(path); return info.isFile() && !info.isSymbolicLink(); }
  catch { return false; }
}
async function exists(path: string): Promise<boolean> {
  try { await lstat(path); return true; } catch { return false; }
}

async function treeManifest(root: string): Promise<JsonObject> {
  const result: JsonObject = {};
  async function visit(directory: string): Promise<void> {
    for (const entry of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      if (SKIP_DIRECTORIES.has(entry.name)) continue;
      const path = join(directory, entry.name);
      const rel = relative(root, path).split(sep).join("/");
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) await visit(path);
      else if (entry.isFile() && ![".pyc", ".pyo"].some((suffix) => entry.name.endsWith(suffix)) && entry.name !== ".agenelf-evolution-workspace.json") {
        result[rel] = await fileSha256(path);
      }
    }
  }
  await visit(root);
  return result;
}

async function runProcess(argv: string[], cwd: string, timeoutMs: number): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(argv[0], argv.slice(1), { cwd, shell: false, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString("utf8"); if (stdout.length > MAX_OUTPUT * 2) child.kill("SIGKILL"); });
    child.stderr.on("data", (chunk: Buffer) => { stderr += chunk.toString("utf8"); if (stderr.length > MAX_OUTPUT * 2) child.kill("SIGKILL"); });
    const timer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("close", (code, signal) => {
      clearTimeout(timer);
      resolvePromise({
        exitCode: typeof code === "number" ? code : signal ? 124 : 126,
        stdout: stdout.slice(-MAX_OUTPUT),
        stderr: stderr.slice(-MAX_OUTPUT)
      });
    });
  });
}

async function addedText(target: string, candidate: string): Promise<string> {
  if (!(await regularFile(target))) return readFile(candidate, "utf8");
  const result = await runProcess(["git", "diff", "--no-index", "--unified=0", "--", target, candidate], dirname(target), 30_000);
  if (![0, 1].includes(result.exitCode)) throw new Error(`无法计算候选差异：${result.stderr.slice(-2000)}`);
  return result.stdout.split(/\r?\n/).filter((line) => line.startsWith("+") && !line.startsWith("+++" )).map((line) => line.slice(1)).join("\n");
}

async function scanRedlines(relativePath: string, candidatePath: string, targetPath: string): Promise<void> {
  const body = await readFile(candidatePath, "utf8");
  const additions = await addedText(targetPath, candidatePath);
  for (const [label, pattern] of REDLINE_PATTERNS) {
    if (pattern.test(additions)) throw new Error(`候选 ${relativePath} 新增代码命中永久安全红线：${label}`);
  }
  const missing = (REQUIRED_TOKENS[relativePath] ?? []).filter((token) => !body.includes(token));
  if (missing.length) throw new Error(`候选 ${relativePath} 删除了可信升级根约束：${missing.join(", ")}`);
}

function parseDate(value: JsonValue | undefined): number {
  const parsed = Date.parse(String(value ?? ""));
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

export async function checkAuthorization(root: string, requestId: string, expectedBinding: JsonObject): Promise<AuthorizationCheck> {
  if (!AUTH_RE.test(requestId)) return { state: "not_found" };
  const authRequest = await readJson<JsonObject | null>(join(root, "data", "auth-requests", `${requestId}.json`), null);
  if (!authRequest) return { state: "not_found" };
  if (await regularFile(join(root, "data", "auth-consumed", `${requestId}.json`))) return { state: "used", request: authRequest };
  const decision = await readJson<JsonObject | null>(join(root, "data", "auth-decisions", `${requestId}.json`), null);
  if (!decision) return { state: parseDate(authRequest.expires_at) < Date.now() ? "expired" : "pending", request: authRequest };
  if (String(decision.decision ?? "") === "deny") return { state: "denied", request: authRequest };
  const required = Math.max(1, Number(authRequest.required_approvers ?? 1) || 1);
  if (String(decision.decision ?? "") !== "approve") return { state: required > 1 && parseDate(authRequest.expires_at) >= Date.now() ? "pending" : "not_found", request: authRequest };
  if (required > 1) {
    const approvers = new Set((Array.isArray(decision.approvals) ? decision.approvals : []).filter(isObject).map((item) => String(item.decided_by ?? "")).filter(Boolean));
    if (approvers.size < required) return { state: parseDate(authRequest.expires_at) < Date.now() ? "expired" : "pending", request: authRequest };
  }
  if (parseDate(decision.expires_at) < Date.now()) return { state: "expired", request: authRequest };
  const requestBinding = isObject(authRequest.binding) ? authRequest.binding : {};
  const actual = String(decision.fingerprint ?? "");
  if (actual !== pythonDigest(requestBinding) || actual !== pythonDigest(expectedBinding)) return { state: "binding_mismatch", request: authRequest };
  return { state: "approved", request: authRequest };
}

export async function consumeAuthorization(root: string, requestId: string, expectedBinding: JsonObject): Promise<boolean> {
  const checked = await checkAuthorization(root, requestId, expectedBinding);
  if (checked.state !== "approved") return false;
  try {
    await atomicWriteJson(join(root, "data", "auth-consumed", `${requestId}.json`), {
      id: requestId,
      consumed_at: now(),
      fingerprint: pythonDigest(expectedBinding)
    }, true);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EEXIST" ? false : Promise.reject(error);
  }
}

async function atomicCopy(source: string, destination: string): Promise<void> {
  await mkdir(dirname(destination), { recursive: true });
  const temp = join(dirname(destination), `.${destination.split(sep).at(-1)}-${randomUUID()}.upgrade`);
  const sourceInfo = await stat(source);
  const handle = await open(temp, "wx", 0o600);
  try {
    await handle.writeFile(await readFile(source));
    await handle.sync();
  } finally { await handle.close(); }
  await chmod(temp, sourceInfo.mode & 0o777).catch(() => undefined);
  await rename(temp, destination);
}

interface BackupManifest extends JsonObject {
  request_id: string;
  created_at: string;
  files: JsonObject[];
}

export class SelfUpgradeRunner {
  readonly root: string;
  readonly candidateRoot: string;
  readonly targetRoot: string;
  readonly testRunner: (candidateRepo: string, baselineManifest: string) => Promise<JsonObject>;
  readonly requests: string;
  readonly results: string;
  readonly locks: string;
  readonly backups: string;
  readonly sessions: string;
  readonly events: string;
  readonly auditPath: string;
  private initialized = false;

  constructor(root: string, options: SelfUpgradeOptions = {}) {
    this.root = resolve(root);
    this.candidateRoot = resolve(options.candidateRoot ?? join(this.root, "app-tmp", "repo"));
    this.targetRoot = resolve(options.targetRoot ?? process.env.AGENELF_UPGRADE_TARGET ?? join(this.root, "upgrade-target"));
    this.requests = join(this.root, "data", "self-upgrade-requests");
    this.results = join(this.root, "data", "self-upgrade-results");
    this.locks = join(this.root, "data", "self-upgrade-locks");
    this.backups = join(this.root, "data", "self-upgrade-backups");
    this.sessions = join(this.root, "data", "authorized-upgrades");
    this.events = join(this.root, "data", "self-upgrade-events");
    this.auditPath = join(this.root, "logs", "self-upgrade-runner.log");
    this.testRunner = options.testRunner ?? ((candidate, baseline) => this.runTrustedTests(candidate, baseline));
  }

  async initialize(): Promise<void> {
    for (const path of [this.requests, this.results, this.locks, this.backups, this.events, dirname(this.auditPath)]) await mkdir(path, { recursive: true });
    this.initialized = true;
  }

  protected async beforeLock(_request: SelfUpgradeRequest): Promise<void> { /* deterministic race hook */ }
  protected async beforeApplyFile(_path: string, _index: number): Promise<void> { /* fault-injection hook */ }

  private async event(requestId: string, type: string, payload: JsonObject = {}): Promise<void> {
    await appendLine(join(this.events, `${requestId}.jsonl`), JSON.stringify({
      schema_version: 1,
      id: randomId("uevt-", 20),
      upgrade_id: requestId,
      type,
      origin: "runner",
      ts: now(),
      payload: sanitizeObject(payload)
    }));
  }

  private async audit(eventName: string, detail: string): Promise<void> {
    await appendLine(this.auditPath, `[${now()}] [${eventName}] ${redactSensitiveText(detail).slice(-3000)}`);
  }

  private targetPath(relativePath: string): string {
    const path = resolve(this.targetRoot, relativePath);
    if (!within(this.targetRoot, path)) throw new Error(`target path escapes upgrade root: ${relativePath}`);
    return path;
  }

  private manifestPath(sessionId: string, raw: unknown, label: string): string {
    const path = resolve(String(raw ?? ""));
    const sessionRoot = resolve(this.sessions, sessionId);
    if (!within(sessionRoot, path)) throw new Error(`${label} 不在升级会话证据目录内`);
    return path;
  }

  private requestPayload(request: SelfUpgradeRequest): JsonObject {
    return {
      schema_version: request.schema_version,
      session_id: request.session_id,
      intent_auth_id: request.intent_auth_id,
      candidate_auth_id: request.candidate_auth_id,
      candidate_binding: request.candidate_binding,
      candidate_digest: request.candidate_digest,
      changed_files: request.changed_files as unknown as JsonValue,
      candidate_repo: request.candidate_repo
    };
  }

  private async loadValidated(request: SelfUpgradeRequest): Promise<{ session: JsonObject; allowedPaths: JsonValue; baselinePath: string; testReportPath: string }> {
    if (!REQUEST_RE.test(request.id) || request.schema_version !== 1) throw new Error("invalid self-upgrade request id or version");
    if (!SESSION_RE.test(request.session_id)) throw new Error("invalid upgrade session id");
    if (pythonDigest(this.requestPayload(request)) !== request.fingerprint) throw new Error("request fingerprint mismatch");
    if (resolve(request.candidate_repo) !== this.candidateRoot) throw new Error("candidate repository binding mismatch");
    const session = await readJson<JsonObject | null>(join(this.sessions, `${request.session_id}.json`), null);
    if (!session) throw new Error("upgrade session missing");
    if (!["apply_queued", "awaiting_candidate_approval"].includes(String(session.status ?? ""))) throw new Error(`session state cannot be applied: ${String(session.status ?? "")}`);
    if (session.intent_consumed !== true) throw new Error("intent authorization was not consumed by candidate generation");
    for (const field of ["intent_auth_id", "candidate_auth_id", "candidate_digest"] as const) {
      if (pythonCanonical(request[field] as JsonValue) !== pythonCanonical(session[field] as JsonValue)) throw new Error(`${field} mismatch`);
    }
    if (pythonCanonical(request.candidate_binding) !== pythonCanonical(session.candidate_binding as JsonValue)) throw new Error("candidate binding mismatch");
    if (pythonCanonical(request.changed_files as unknown as JsonValue) !== pythonCanonical(session.changed_file_records as JsonValue)) throw new Error("changed-file manifest mismatch");
    const plan = isObject(session.plan) ? session.plan : {};
    if (!Array.isArray(plan.allowed_paths)) throw new Error("session allowed_paths invalid");
    const baselinePath = this.manifestPath(request.session_id, session.baseline_manifest_path, "baseline manifest");
    const testReportPath = this.manifestPath(request.session_id, session.test_report_path, "test report");
    return { session, allowedPaths: plan.allowed_paths, baselinePath, testReportPath };
  }

  async verifyCandidate(request: SelfUpgradeRequest, loaded: { session: JsonObject; allowedPaths: JsonValue; baselinePath: string; testReportPath: string }): Promise<void> {
    const candidateInfo = await lstat(this.candidateRoot);
    if (!candidateInfo.isDirectory() || candidateInfo.isSymbolicLink()) throw new Error("candidate repository missing or invalid");
    const manifest = await treeManifest(this.candidateRoot);
    const digest = pythonDigest(manifest);
    if (digest !== request.candidate_digest) throw new Error("candidate tree changed after owner approval");
    const binding = request.candidate_binding;
    if (String(binding.candidate_tree_sha256 ?? "") !== digest) throw new Error("candidate binding digest mismatch");
    if (await fileSha256(loaded.baselinePath) !== String(binding.baseline_manifest_sha256 ?? "")) throw new Error("baseline manifest evidence changed");
    if (await fileSha256(loaded.testReportPath) !== String(binding.test_report_sha256 ?? "")) throw new Error("test report evidence changed");
    for (const raw of request.changed_files) {
      if (!isObject(raw)) throw new Error("changed-file record must be an object");
      const path = validateRepoPath(raw.path, loaded.allowedPaths);
      const candidate = resolve(this.candidateRoot, path);
      if (!within(this.candidateRoot, candidate) || !(await regularFile(candidate))) throw new Error(`candidate file missing or symlinked: ${path}`);
      const target = this.targetPath(path);
      await scanRedlines(path, candidate, target);
      if (await fileSha256(candidate) !== String(raw.after_sha256 ?? "")) throw new Error(`candidate file hash mismatch: ${path}`);
      const before = String(raw.before_sha256 ?? "");
      if (before) {
        if (!(await regularFile(target)) || await fileSha256(target) !== before) throw new Error(`target changed since candidate baseline: ${path}`);
      } else if (await exists(target)) throw new Error(`candidate expected a new file but target exists: ${path}`);
    }
  }

  async runTrustedTests(candidateRepo: string, baselineManifest: string): Promise<JsonObject> {
    const runner = join(this.root, "scripts", "run_authorized_upgrade_tests.py");
    if (!(await regularFile(runner)) || !(await regularFile(baselineManifest))) throw new Error("trusted test runner or baseline manifest missing");
    const result = await runProcess(["python", runner, "--candidate-repo", candidateRepo, "--baseline-manifest", baselineManifest, "--timeout", "600"], this.root, 900_000);
    if (result.exitCode !== 0) throw new Error(`candidate revalidation failed:\n${`${result.stdout}\n${result.stderr}`.slice(-8000)}`);
    try {
      const value = JSON.parse(result.stdout || "{}");
      return isObject(value) ? value : { status: "passed", output: result.stdout.slice(-8000) };
    } catch {
      return { status: "passed", output: `${result.stdout}\n${result.stderr}`.slice(-8000) };
    }
  }

  async backupTargets(request: SelfUpgradeRequest): Promise<{ directory: string; manifest: BackupManifest }> {
    await mkdir(this.backups, { recursive: true });
    const directory = join(this.backups, request.id);
    await mkdir(directory);
    const manifest: BackupManifest = { request_id: request.id, created_at: now(), files: [] };
    for (const record of request.changed_files) {
      const path = String(record.path ?? "");
      const source = this.targetPath(path);
      const existed = await regularFile(source);
      const entry: JsonObject = { path, existed };
      if (existed) {
        const destination = join(directory, "files", path);
        await atomicCopy(source, destination);
        entry.sha256 = await fileSha256(source);
      }
      manifest.files.push(entry);
    }
    await atomicWriteJson(join(directory, "manifest.json"), manifest);
    await this.event(request.id, "upgrade.backup.created", { backup_dir: directory, files: manifest.files.length });
    return { directory, manifest };
  }

  async rollback(directory: string, manifest: BackupManifest, requestId = ""): Promise<void> {
    if (requestId) await this.event(requestId, "upgrade.rollback.started", { backup_dir: directory });
    for (const entry of [...manifest.files].reverse()) {
      const path = String(entry.path ?? "");
      const target = this.targetPath(path);
      if (entry.existed === true) {
        const backup = join(directory, "files", path);
        if (await regularFile(backup)) await atomicCopy(backup, target);
      } else await unlink(target).catch((error) => { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; });
    }
    if (requestId) await this.event(requestId, "upgrade.rollback.completed", { backup_dir: directory });
  }

  private restartMetadata(paths: string[]): { restart_required: boolean; hot_reloadable_skills: string[] } {
    const hot = paths.filter((path) => path.startsWith("app/skills/") && path.endsWith(".py")).map((path) => path.split("/").at(-1)!.slice(0, -3));
    const restart = paths.some((path) => !(path.startsWith("app/skills/") || path.startsWith("app/tests/") || path.startsWith("docs/") || ["README.md", "Makefile"].includes(path)));
    return { restart_required: restart, hot_reloadable_skills: hot };
  }

  private async applyFiles(request: SelfUpgradeRequest): Promise<{ applied: string[]; backup_dir: string; restart_required: boolean; hot_reloadable_skills: string[] }> {
    const backup = await this.backupTargets(request);
    const applied: string[] = [];
    try {
      for (let index = 0; index < request.changed_files.length; index += 1) {
        const record = request.changed_files[index];
        const path = String(record.path ?? "");
        await this.beforeApplyFile(path, index);
        const source = resolve(this.candidateRoot, path);
        const destination = this.targetPath(path);
        await atomicCopy(source, destination);
        if (await fileSha256(destination) !== String(record.after_sha256 ?? "")) throw new Error(`post-write hash mismatch: ${path}`);
        applied.push(path);
        await this.event(request.id, "upgrade.file.applied", { path, index });
      }
    } catch (error) {
      await this.rollback(backup.directory, backup.manifest, request.id);
      throw error;
    }
    return { applied, backup_dir: backup.directory, ...this.restartMetadata(applied) };
  }

  private async persist(requestId: string, result: JsonObject, eventType: string): Promise<string> {
    try { await atomicWriteJson(join(this.results, `${requestId}.json`), result, true); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return "done";
      throw error;
    }
    await this.event(requestId, eventType, { status: result.status as JsonValue });
    return String(result.status ?? "failed");
  }

  async processRequest(path: string): Promise<string> {
    const initial = await readJson<SelfUpgradeRequest | null>(path, null);
    if (!initial) return "invalid";
    if (!REQUEST_RE.test(String(initial.id ?? ""))) return "invalid";
    if (await regularFile(join(this.results, `${initial.id}.json`))) return "done";
    let loaded;
    try { loaded = await this.loadValidated(initial); }
    catch (error) {
      return this.persist(initial.id, { schema_version: 1, id: initial.id, status: "failed", finished_at: now(), error: redactSensitiveText(error instanceof Error ? `${error.name}: ${error.message}` : String(error)) }, "upgrade.failed");
    }
    const preliminary = await checkAuthorization(this.root, initial.candidate_auth_id, initial.candidate_binding);
    if (preliminary.state === "pending") return "pending";
    await this.beforeLock(initial);
    const lockPath = join(this.locks, `${initial.id}.lock`);
    let lock;
    try { lock = await open(lockPath, "wx", 0o600); }
    catch (error) { return (error as NodeJS.ErrnoException).code === "EEXIST" ? "locked" : Promise.reject(error); }
    try {
      if (await regularFile(join(this.results, `${initial.id}.json`))) return "done";
      const request = await readJson<SelfUpgradeRequest | null>(path, null);
      if (!request || request.id !== initial.id) throw new Error("locked request missing or replaced");
      loaded = await this.loadValidated(request);
      await this.event(request.id, "upgrade.runner.claimed", { session_id: request.session_id, files: request.changed_files.length });
      const auth = await checkAuthorization(this.root, request.candidate_auth_id, request.candidate_binding);
      await this.event(request.id, "upgrade.authorization.checked", { state: auth.state, candidate_auth_id: request.candidate_auth_id });
      if (auth.state === "pending") return "pending";
      if (auth.state !== "approved") throw new Error(`candidate authorization is not approved: ${auth.state}`);
      await this.verifyCandidate(request, loaded);
      await this.event(request.id, "upgrade.candidate.verified", { candidate_digest: request.candidate_digest });
      await this.event(request.id, "upgrade.tests.started", { baseline_manifest: "verified-session-evidence" });
      const testReport = await this.testRunner(this.candidateRoot, loaded.baselinePath);
      await this.event(request.id, "upgrade.tests.completed", { status: testReport.status ?? "passed" });
      const finalAuth = await checkAuthorization(this.root, request.candidate_auth_id, request.candidate_binding);
      if (finalAuth.state !== "approved") throw new Error(`candidate authorization changed before consume: ${finalAuth.state}`);
      if (!(await consumeAuthorization(this.root, request.candidate_auth_id, request.candidate_binding))) throw new Error("candidate authorization could not be consumed");
      await this.event(request.id, "upgrade.authorization.consumed", { candidate_auth_id: request.candidate_auth_id });
      const applied = await this.applyFiles(request);
      const result: JsonObject = {
        schema_version: 1,
        id: request.id,
        status: "succeeded",
        session_id: request.session_id,
        finished_at: now(),
        changed_files: applied.applied,
        backup_dir: applied.backup_dir,
        restart_required: applied.restart_required,
        hot_reloadable_skills: applied.hot_reloadable_skills,
        test_report: testReport
      };
      const status = await this.persist(request.id, result, "upgrade.result.persisted");
      await this.audit(status, `${request.id} files=${applied.applied.join(",")}`);
      return status;
    } catch (error) {
      const reason = redactSensitiveText(error instanceof Error ? `${error.name}: ${error.message}` : String(error)).slice(-8000);
      const status = await this.persist(initial.id, { schema_version: 1, id: initial.id, status: "failed", finished_at: now(), error: reason }, "upgrade.failed");
      await this.audit("failed", `${initial.id} ${reason}`);
      return status;
    } finally {
      await lock.close();
      await rm(lockPath, { force: true });
    }
  }

  async processOnce(): Promise<Record<string, number>> {
    if (!this.initialized) await this.initialize();
    let names: string[] = [];
    try { names = await readdir(this.requests); } catch { return {}; }
    const counts: Record<string, number> = {};
    for (const name of names.filter((item) => /^self-upgrade-[0-9a-f]{16}\.json$/.test(item)).sort()) {
      const state = await this.processRequest(join(this.requests, name));
      counts[state] = (counts[state] ?? 0) + 1;
    }
    return counts;
  }
}
