import { createHash } from "node:crypto";
import { open, readdir, realpath, stat } from "node:fs/promises";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { atomicWriteJson, readJson, withDirectoryLock } from "./fs-store.ts";
import { randomId } from "./canonical.ts";
import { sanitizeObject } from "./privacy.ts";
import type { JsonObject } from "./types.ts";

export interface RunnerConfig {
  roots?: Record<string, string>;
  commands?: Record<string, string[]>;
}
export interface NodeRunnerRequest {
  schema_version: 1;
  id: string;
  operation: "runtime_info" | "file_digest" | "allowlisted_command";
  parameters: JsonObject;
  created_at: string;
  expires_at: string;
}

export class NodeRunner {
  readonly root: string;
  readonly requestsDir: string;
  readonly resultsDir: string;
  readonly locksDir: string;
  readonly configPath: string;

  constructor(root: string) {
    this.root = resolve(root);
    this.requestsDir = join(this.root, "data", "node-runner-requests");
    this.resultsDir = join(this.root, "data", "node-runner-results");
    this.locksDir = join(this.root, "data", "node-runner-locks");
    this.configPath = join(this.root, "local", "node-runner.json");
  }

  async submit(operation: NodeRunnerRequest["operation"], parameters: JsonObject = {}, ttlSeconds = 300): Promise<NodeRunnerRequest> {
    const now = Date.now();
    const request: NodeRunnerRequest = {
      schema_version: 1,
      id: randomId("nrun-", 16),
      operation,
      parameters: sanitizeObject(parameters),
      created_at: new Date(now).toISOString(),
      expires_at: new Date(now + Math.max(15, Math.min(ttlSeconds, 3600)) * 1000).toISOString()
    };
    await atomicWriteJson(join(this.requestsDir, `${request.id}.json`), request as unknown as JsonObject, true);
    return request;
  }

  async processOnce(): Promise<number> {
    let names: string[] = [];
    try { names = (await readdir(this.requestsDir)).filter((name) => /^nrun-[0-9a-f]{16}\.json$/.test(name)).sort(); } catch { return 0; }
    let processed = 0;
    for (const name of names) {
      const id = name.slice(0, -5);
      if (await readJson(join(this.resultsDir, name), null) !== null) continue;
      await withDirectoryLock(join(this.locksDir, `${id}.lock`), async () => {
        if (await readJson(join(this.resultsDir, name), null) !== null) return;
        const request = await readJson<NodeRunnerRequest | null>(join(this.requestsDir, name), null);
        if (!request || request.id !== id) return;
        const started = new Date().toISOString();
        let result: JsonObject;
        try {
          if (Date.parse(request.expires_at) <= Date.now()) throw new Error("request expired");
          result = { id, status: "succeeded", started_at: started, finished_at: new Date().toISOString(), output: await this.execute(request) };
        } catch (error) {
          result = { id, status: "failed", started_at: started, finished_at: new Date().toISOString(), error: error instanceof Error ? error.message : String(error) };
        }
        await atomicWriteJson(join(this.resultsDir, name), result, true);
        processed += 1;
      });
    }
    return processed;
  }

  private async execute(request: NodeRunnerRequest): Promise<JsonObject> {
    if (request.operation === "runtime_info") {
      return { node: process.version, platform: process.platform, arch: process.arch, pid: process.pid };
    }
    const config = await readJson<RunnerConfig>(this.configPath, {});
    if (request.operation === "file_digest") {
      const alias = String(request.parameters.root_alias ?? "");
      const relative = String(request.parameters.path ?? "");
      const configured = config.roots?.[alias];
      if (!configured) throw new Error("未知 root_alias");
      const base = await realpath(resolve(this.root, configured));
      const target = await realpath(resolve(base, relative));
      if (target !== base && !target.startsWith(`${base}/`)) throw new Error("path 越出 allowlisted root");
      const info = await stat(target);
      if (!info.isFile() || info.size > 64 * 1024 * 1024) throw new Error("只允许读取 64 MiB 内普通文件");
      const handle = await open(target, "r");
      const hash = createHash("sha256");
      try {
        for await (const chunk of handle.createReadStream()) hash.update(chunk);
      } finally { await handle.close(); }
      return { root_alias: alias, path: relative, sha256: hash.digest("hex"), size: info.size };
    }
    if (request.operation === "allowlisted_command") {
      const alias = String(request.parameters.alias ?? "");
      const command = config.commands?.[alias];
      if (!Array.isArray(command) || !command.length || command.some((part) => typeof part !== "string" || !part)) throw new Error("未知或非法 command alias");
      const timeoutMs = Math.max(100, Math.min(Number(request.parameters.timeout_ms ?? 30_000), 120_000));
      return this.runCommand(command, timeoutMs);
    }
    throw new Error("unsupported operation");
  }

  private runCommand(command: string[], timeoutMs: number): Promise<JsonObject> {
    return new Promise((resolvePromise, reject) => {
      const child = spawn(command[0], command.slice(1), { shell: false, cwd: this.root, env: { PATH: process.env.PATH || "" }, stdio: ["ignore", "pipe", "pipe"] });
      let stdout = ""; let stderr = ""; let killed = false;
      const timer = setTimeout(() => { killed = true; child.kill("SIGKILL"); }, timeoutMs);
      child.stdout.on("data", (chunk) => { stdout = (stdout + chunk).slice(-64_000); });
      child.stderr.on("data", (chunk) => { stderr = (stderr + chunk).slice(-64_000); });
      child.on("error", (error) => { clearTimeout(timer); reject(error); });
      child.on("close", (code, signal) => {
        clearTimeout(timer);
        resolvePromise({ exit_code: code, signal, timed_out: killed, stdout, stderr });
      });
    });
  }
}
