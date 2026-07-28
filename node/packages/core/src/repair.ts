import { lstat, mkdir, open, readdir, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { join, resolve } from "node:path";
import { performance } from "node:perf_hooks";
import { appendLine, atomicWriteJson, readJson } from "./fs-store.ts";
import { canonicalize, randomId, sha256 } from "./canonical.ts";
import { redactSensitiveText, sanitizeObject } from "./privacy.ts";
import { parseSimpleYaml } from "./simple-yaml.ts";
import type { JsonObject, JsonValue } from "./types.ts";

const REPAIR_ID_RE = /^repair-[0-9a-f]{16}$/;
const ALIAS_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const SAFE_PATH_RE = /^[A-Za-z0-9_./+@=-]+$/;
const BASE_RE = /^[0-9a-fA-F]{7,64}$/;
const MAX_OUTPUT = 16_000;
const MAX_COMMANDS = 8;
const ALLOWED_EXECUTABLES = new Set([
  "python", "python3", "pytest", "mvn", "./mvnw", "gradle", "./gradlew",
  "npm", "pnpm", "yarn", "go", "cargo", "dotnet", "bash", "sh"
]);
const GLOBAL_PROTECTED = [".git/", ".github/workflows/", "local/", "secrets/", ".env", ".ops-runner.env", "policy/"];

export interface RepairRequest {
  schema_version: 1;
  id: string;
  capability: "code.repair";
  operation: "apply_patch_and_test";
  target: string;
  parameters: {
    test_profile: string;
    patch_sha256: string;
    patch_bytes: number;
    expected_base: string;
  };
  risk: "read";
  summary: string;
  patch: string;
  fingerprint: string;
  created_at: string;
  created_by: string;
}

interface TestProfile { commands: string[][]; timeoutSeconds: number }
interface RepositoryProfile {
  sourceDir: string;
  allowedTestProfiles: string[];
  defaultTestProfile: string;
  protectedPaths: string[];
  maxPatchFiles: number;
  maxPatchBytes: number;
}
interface CommandEvidence {
  phase: string;
  argv: string[];
  exit_code: number | null;
  duration_ms: number;
  stdout_tail: string;
  stderr_tail: string;
  timed_out: boolean;
}

function now(): string { return new Date().toISOString(); }
function safeOutput(value: unknown, limit = MAX_OUTPUT): string {
  const text = redactSensitiveText(value);
  return text.length <= limit ? text : `…${text.slice(-limit)}`;
}
function object(value: JsonValue | undefined, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} 必须是 object`);
  return value;
}
function safeRelative(value: unknown, label: string): string {
  const text = String(value ?? "").trim().replaceAll("\\", "/");
  if (!text || text.startsWith("/") || text.split("/").includes("..") || !SAFE_PATH_RE.test(text)) throw new Error(`${label} 必须是安全相对路径`);
  return text.replace(/^\.\//, "");
}
function under(root: string, relative: string, label: string): string {
  const candidate = resolve(root, safeRelative(relative, label));
  if (!candidate.startsWith(`${resolve(root)}/`)) throw new Error(`${label} 逃逸出允许根目录`);
  return candidate;
}
function boundedInt(value: unknown, fallback: number, min: number, max: number, label: string): number {
  const parsed = Number(value ?? fallback);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) throw new Error(`${label} 必须在 ${min}-${max}`);
  return parsed;
}
function minimalEnv(home: string): NodeJS.ProcessEnv {
  const path = process.env.PATH || "/usr/local/bin:/usr/bin:/bin";
  return {
    PATH: path,
    HOME: home,
    TMPDIR: join(home, "tmp"),
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    PYTHONIOENCODING: "utf-8",
    PYTHONDONTWRITEBYTECODE: "1",
    CI: "1",
    NO_PROXY: "*",
    no_proxy: "*",
    HTTP_PROXY: "",
    HTTPS_PROXY: "",
    ALL_PROXY: "",
    npm_config_ignore_scripts: "true",
    npm_config_audit: "false",
    npm_config_fund: "false"
  };
}
function patchDigest(patch: string): string { return sha256(patch as unknown as JsonValue); }
function canonicalPayload(request: RepairRequest): JsonObject {
  return {
    capability: "code.repair",
    operation: "apply_patch_and_test",
    target: request.target.trim(),
    parameters: {
      test_profile: request.parameters.test_profile.trim(),
      patch_sha256: request.parameters.patch_sha256,
      patch_bytes: request.parameters.patch_bytes,
      expected_base: request.parameters.expected_base.trim().toLowerCase()
    }
  };
}

function changedPaths(patch: string): string[] {
  if (patch.includes("GIT binary patch") || patch.includes("Binary files ")) throw new Error("暂不支持二进制补丁");
  const result: string[] = [];
  for (const line of patch.split(/\r?\n/)) {
    if (!line.startsWith("diff --git ")) continue;
    const match = /^diff --git a\/([^\s]+) b\/([^\s]+)$/.exec(line);
    if (!match) throw new Error("补丁路径必须是不含空格的标准 git 路径");
    if (match[1] !== match[2]) throw new Error("当前版本不支持重命名补丁");
    const path = safeRelative(match[2], "补丁路径");
    if (!result.includes(path)) result.push(path);
  }
  if (!result.length) throw new Error("补丁未包含任何标准 diff --git 文件");
  return result;
}
function protectedPath(path: string, configured: string[]): boolean {
  const normalized = path.replace(/^\.\//, "");
  return [...GLOBAL_PROTECTED, ...configured].some((raw) => {
    const prefix = String(raw ?? "").trim().replaceAll("\\", "/").replace(/^\.\//, "");
    if (!prefix) return false;
    return normalized === prefix.replace(/\/$/, "") || normalized.startsWith(`${prefix.replace(/\/$/, "")}/`);
  });
}
function validateCommand(value: JsonValue, profile: string): string[] {
  if (!Array.isArray(value) || !value.length) throw new Error(`测试配置 ${profile} command 必须是非空 argv`);
  const argv = value.map((item) => String(item));
  if (argv.length > 64 || argv.some((item) => !item || item.length > 2_000 || /[\0\r\n]/.test(item))) throw new Error(`测试配置 ${profile} argv 非法或过长`);
  if (!ALLOWED_EXECUTABLES.has(argv[0])) throw new Error(`测试配置 ${profile} 不允许执行：${argv[0]}`);
  if ((argv[0] === "bash" || argv[0] === "sh" || argv[0] === "python" || argv[0] === "python3") && argv.slice(1).includes("-c")) throw new Error(`测试配置 ${profile} 禁止 ${argv[0]} -c`);
  return argv;
}

export class RepairCatalog {
  readonly root: string;
  readonly configFile: string;
  readonly sourceRoot: string;
  readonly repairRoot: string;
  repositories = new Map<string, RepositoryProfile>();
  testProfiles = new Map<string, TestProfile>();

  constructor(root: string) {
    this.root = resolve(root);
    this.configFile = resolve(process.env.AGENELF_REPOSITORIES_FILE || join(root, "local", "repositories.yaml"));
    this.sourceRoot = resolve(process.env.AGENELF_CODE_WORKSPACES || join(root, "code-workspaces"));
    this.repairRoot = resolve(process.env.AGENELF_REPAIR_SPACE || join(root, "repair-space"));
  }

  async initialize(): Promise<void> {
    const info = await lstat(this.configFile);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error("repositories.yaml 必须是普通文件");
    const config = parseSimpleYaml(await readFile(this.configFile, "utf8"));
    if (Number(config.schema_version ?? 1) !== 1) throw new Error("repositories.yaml schema_version 必须为 1");
    const rawProfiles = object(config.test_profiles, "test_profiles");
    const profiles = new Map<string, TestProfile>();
    for (const [name, raw] of Object.entries(rawProfiles)) {
      if (!ALIAS_RE.test(name)) throw new Error(`非法测试配置：${name}`);
      const value = object(raw, `test_profiles.${name}`);
      if (!Array.isArray(value.commands) || value.commands.length < 1 || value.commands.length > MAX_COMMANDS) throw new Error(`测试配置 ${name} commands 必须有 1-${MAX_COMMANDS} 项`);
      profiles.set(name, {
        commands: value.commands.map((item) => validateCommand(item, name)),
        timeoutSeconds: boundedInt(value.timeout_seconds, 900, 1, 1_800, `测试配置 ${name} timeout_seconds`)
      });
    }
    const rawRepositories = object(config.repositories, "repositories");
    const repositories = new Map<string, RepositoryProfile>();
    for (const [alias, raw] of Object.entries(rawRepositories)) {
      if (!ALIAS_RE.test(alias)) throw new Error(`非法仓库配置：${alias}`);
      const value = object(raw, `repositories.${alias}`);
      if (!Array.isArray(value.allowed_test_profiles) || !value.allowed_test_profiles.length) throw new Error(`仓库 ${alias} 必须配置 allowed_test_profiles`);
      const allowed = value.allowed_test_profiles.map((item) => String(item));
      if (allowed.some((name) => !profiles.has(name))) throw new Error(`仓库 ${alias} 引用了未知测试配置`);
      const defaultProfile = String(value.default_test_profile ?? allowed[0]);
      if (!allowed.includes(defaultProfile)) throw new Error(`仓库 ${alias} 默认测试配置不在允许清单`);
      const protectedPaths = Array.isArray(value.protected_paths) ? value.protected_paths.slice(0, 100).map(String) : [];
      repositories.set(alias, {
        sourceDir: safeRelative(value.source_dir ?? alias, "source_dir"),
        allowedTestProfiles: allowed,
        defaultTestProfile: defaultProfile,
        protectedPaths,
        maxPatchFiles: boundedInt(value.max_patch_files, 20, 1, 100, `仓库 ${alias} max_patch_files`),
        maxPatchBytes: boundedInt(value.max_patch_bytes, 262_144, 1_024, 262_144, `仓库 ${alias} max_patch_bytes`)
      });
    }
    this.repositories = repositories;
    this.testProfiles = profiles;
  }
}

async function runCommand(argv: string[], cwd: string, timeoutSeconds: number, env: NodeJS.ProcessEnv): Promise<Omit<CommandEvidence, "phase">> {
  const started = performance.now();
  return new Promise((resolvePromise) => {
    const child = spawn(argv[0], argv.slice(1), { cwd, env, shell: false, stdio: ["ignore", "pipe", "pipe"], detached: true });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString("utf8"); });
    child.stderr.on("data", (chunk: Buffer) => { stderr += chunk.toString("utf8"); });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try { process.kill(-child.pid!, "SIGKILL"); } catch { child.kill("SIGKILL"); }
    }, Math.max(1, timeoutSeconds) * 1_000);
    child.once("error", (error) => {
      clearTimeout(timer);
      resolvePromise({ argv, exit_code: null, duration_ms: Math.round((performance.now() - started) * 100) / 100, stdout_tail: "", stderr_tail: safeOutput(`${error.name}: ${error.message}`), timed_out: false });
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      resolvePromise({ argv, exit_code: typeof code === "number" ? code : null, duration_ms: Math.round((performance.now() - started) * 100) / 100, stdout_tail: safeOutput(stdout), stderr_tail: safeOutput(stderr), timed_out: timedOut });
    });
  });
}

async function scanSymlinks(root: string, directory = root): Promise<void> {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isSymbolicLink()) {
      const target = await realpath(path);
      if (target !== root && !target.startsWith(`${root}/`)) throw new Error(`工作区包含逃逸符号链接：${path.slice(root.length + 1)}`);
    } else if (entry.isDirectory()) await scanSymlinks(root, path);
  }
}

export class NodeRepairRunner {
  readonly root: string;
  readonly catalog: RepairCatalog;
  readonly requests: string;
  readonly results: string;
  readonly locks: string;
  readonly events: string;
  readonly auditPath: string;

  constructor(root: string) {
    this.root = resolve(root);
    this.catalog = new RepairCatalog(this.root);
    this.requests = join(this.root, "data", "repair-requests");
    this.results = join(this.root, "data", "repair-results");
    this.locks = join(this.root, "data", "repair-locks");
    this.events = join(this.root, "data", "repair-events");
    this.auditPath = join(this.root, "logs", "repair.log");
  }

  async initialize(): Promise<void> { await this.catalog.initialize(); }

  private async event(id: string, type: string, payload: JsonObject = {}): Promise<void> {
    await appendLine(join(this.events, `${id}.jsonl`), JSON.stringify({ schema_version: 1, id: randomId("revt-", 20), repair_id: id, type, origin: "runner", ts: now(), payload: sanitizeObject(payload) }));
  }
  private async audit(event: string, detail: string): Promise<void> { await appendLine(this.auditPath, `[${now()}] [${event}] ${safeOutput(detail, 2_000)}`); }

  private validate(request: RepairRequest): { repository: RepositoryProfile; profile: TestProfile; changed: string[]; payload: JsonObject } {
    if (request.schema_version !== 1 || !REPAIR_ID_RE.test(request.id)) throw new Error("不支持的代码修复请求版本或 ID");
    if (request.capability !== "code.repair" || request.operation !== "apply_patch_and_test" || request.risk !== "read") throw new Error("请求能力、操作或风险不受支持");
    if (!ALIAS_RE.test(request.target) || !ALIAS_RE.test(request.parameters?.test_profile ?? "")) throw new Error("仓库或测试配置别名非法");
    const patch = String(request.patch ?? "");
    if (!patch || patch.includes("\0") || !patch.includes("diff --git ")) throw new Error("补丁必须是无 NUL 的 git unified diff");
    if (redactSensitiveText(patch) !== patch) throw new Error("补丁包含疑似凭据");
    const bytes = Buffer.byteLength(patch, "utf8");
    const digest = patchDigest(patch);
    if (digest !== request.parameters.patch_sha256 || bytes !== Number(request.parameters.patch_bytes)) throw new Error("补丁摘要或大小校验失败");
    const expectedBase = String(request.parameters.expected_base ?? "").trim().toLowerCase();
    if (expectedBase && !BASE_RE.test(expectedBase)) throw new Error("expected_base 必须是 7-64 位 Git SHA");
    const payload = canonicalPayload(request);
    if (sha256(payload as unknown as JsonValue) !== request.fingerprint) throw new Error("请求指纹校验失败");
    const repository = this.catalog.repositories.get(request.target);
    if (!repository) throw new Error(`未知仓库别名：${request.target}`);
    if (!repository.allowedTestProfiles.includes(request.parameters.test_profile)) throw new Error(`仓库未允许测试配置 ${request.parameters.test_profile}`);
    if (bytes > repository.maxPatchBytes) throw new Error("补丁超过仓库配置上限");
    const changed = changedPaths(patch);
    if (changed.length > repository.maxPatchFiles) throw new Error("补丁文件数超过仓库配置上限");
    const blocked = changed.filter((path) => protectedPath(path, repository.protectedPaths));
    if (blocked.length) throw new Error(`补丁触碰受保护路径：${blocked.join(", ")}`);
    return { repository, profile: this.catalog.testProfiles.get(request.parameters.test_profile)!, changed, payload };
  }

  private result(request: RepairRequest, status: string, commands: CommandEvidence[], options: { summary: string; baseCommit?: string; changed?: string[]; artifactDir?: string }): JsonObject {
    return {
      schema_version: 1,
      id: request.id,
      capability: "code.repair",
      operation: "apply_patch_and_test",
      repository: request.target,
      test_profile: request.parameters.test_profile,
      status,
      summary: safeOutput(options.summary),
      base_commit: options.baseCommit ?? "",
      patch_sha256: request.parameters.patch_sha256,
      changed_files: options.changed ?? [],
      artifact_dir: options.artifactDir ?? "",
      commands: commands as unknown as JsonValue,
      finished_at: now(),
      source_repository_modified: false,
      committed: false,
      pushed: false,
      merged: false
    };
  }

  private async execute(request: RepairRequest, repository: RepositoryProfile, profile: TestProfile, changed: string[]): Promise<JsonObject> {
    const source = under(this.catalog.sourceRoot, repository.sourceDir, "source_dir");
    const sourceInfo = await lstat(source);
    if (!sourceInfo.isDirectory() || sourceInfo.isSymbolicLink()) throw new Error("只读源码目录不存在或不是普通目录");
    const gitInfo = await lstat(join(source, ".git"));
    if (!gitInfo.isDirectory() && !gitInfo.isFile()) throw new Error("只读源码不是 Git 仓库");

    const runDir = join(this.catalog.repairRoot, request.id);
    await rm(runDir, { recursive: true, force: true });
    await mkdir(join(runDir, "home", "tmp"), { recursive: true, mode: 0o700 });
    const worktree = join(runDir, "worktree");
    const patchPath = join(runDir, "candidate.patch");
    await writeFile(patchPath, request.patch, { encoding: "utf8", mode: 0o600 });
    const env = minimalEnv(join(runDir, "home"));
    const commands: CommandEvidence[] = [];
    const run = async (phase: string, argv: string[], cwd: string, timeout: number) => {
      const result = await runCommand(argv, cwd, timeout, env);
      const evidence = { phase, ...result };
      commands.push(evidence);
      await this.event(request.id, "repair.command.completed", { phase, exit_code: result.exit_code, timed_out: result.timed_out });
      return result;
    };

    await this.event(request.id, "repair.clone.started", { repository: request.target });
    const clone = await run("clone", ["git", "clone", "--quiet", "--no-hardlinks", "--local", source, worktree], runDir, 300);
    if (clone.exit_code !== 0) return this.result(request, "failed", commands, { summary: "无法复制只读源码仓库" });
    await scanSymlinks(worktree);
    const base = await run("base", ["git", "rev-parse", "HEAD"], worktree, 30);
    const baseCommit = base.exit_code === 0 ? base.stdout_tail.trim().split(/\r?\n/).at(-1) ?? "" : "";
    const expected = request.parameters.expected_base.trim().toLowerCase();
    if (expected && !baseCommit.toLowerCase().startsWith(expected)) return this.result(request, "blocked", commands, { summary: `源码基线 ${baseCommit.slice(0, 12)} 与 expected_base ${expected} 不一致`, baseCommit });

    const check = await run("patch_check", ["git", "apply", "--check", "--whitespace=error-all", patchPath], worktree, 60);
    if (check.exit_code !== 0) return this.result(request, "failed", commands, { summary: "补丁无法应用", baseCommit });
    const apply = await run("patch_apply", ["git", "apply", "--whitespace=fix", patchPath], worktree, 60);
    if (apply.exit_code !== 0) return this.result(request, "failed", commands, { summary: "补丁应用失败", baseCommit });
    let testsOk = true;
    for (const argv of profile.commands) {
      const test = await run("test", argv, worktree, profile.timeoutSeconds);
      if (test.exit_code !== 0 || test.timed_out) { testsOk = false; break; }
    }
    await run("diff_stat", ["git", "diff", "--no-ext-diff", "--stat"], worktree, 30);
    return this.result(request, testsOk ? "succeeded" : "failed", commands, {
      summary: testsOk ? "补丁已在隔离副本应用且全部测试通过" : "补丁已应用，但测试未通过",
      baseCommit,
      changed,
      artifactDir: `repair-space/${request.id}`
    });
  }

  async processRequest(path: string): Promise<string> {
    const request = await readJson<RepairRequest | null>(path, null);
    if (!request) return "invalid";
    const id = String(request.id ?? "");
    if (!REPAIR_ID_RE.test(id)) return "invalid";
    const resultPath = join(this.results, `${id}.json`);
    if (await readJson<JsonObject | null>(resultPath, null)) return "done";
    let lock;
    try { lock = await open(join(this.locks, `${id}.lock`), "wx", 0o600); }
    catch (error) { return (error as NodeJS.ErrnoException).code === "EEXIST" ? "locked" : Promise.reject(error); }
    try {
      await this.event(id, "repair.runner.claimed", { repository: request.target });
      const validated = this.validate(request);
      const result = await this.execute(request, validated.repository, validated.profile, validated.changed);
      await atomicWriteJson(resultPath, result, true);
      await this.event(id, "repair.result.persisted", { status: result.status as JsonValue });
      await this.audit("repair_finished", `${id} status=${String(result.status)} repository=${request.target}`);
      return String(result.status ?? "failed");
    } catch (error) {
      const summary = safeOutput(`${error instanceof Error ? error.name : "Error"}: ${error instanceof Error ? error.message : String(error)}`, 2_000);
      const result = this.result(request, "blocked", [], { summary });
      try { await atomicWriteJson(resultPath, result, true); } catch { /* another trusted writer won */ }
      try { await this.event(id, "repair.failed", { summary }); } catch { /* preserve primary result */ }
      await this.audit("repair_finished", `${id} status=blocked repository=${request.target}`);
      return "blocked";
    } finally {
      await lock.close();
      await rm(join(this.locks, `${id}.lock`), { force: true });
    }
  }

  async processOnce(): Promise<Record<string, number>> {
    try { await this.catalog.initialize(); } catch (error) { await this.audit("config_reload_failed", error instanceof Error ? error.message : String(error)); }
    let names: string[] = [];
    try { names = await readdir(this.requests); } catch { return {}; }
    const counts: Record<string, number> = {};
    for (const name of names.filter((item) => /^repair-[0-9a-f]{16}\.json$/.test(item)).sort()) {
      const state = await this.processRequest(join(this.requests, name));
      counts[state] = (counts[state] ?? 0) + 1;
    }
    return counts;
  }
}
