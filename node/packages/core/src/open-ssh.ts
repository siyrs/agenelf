import { chmod, lstat, mkdtemp, rm, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { redactSensitiveText } from "./privacy.ts";
import { ServerCatalog, type ManagedServer } from "./server-catalog.ts";

const MAX_OUTPUT = 100_000;
const PROXY_URI_RE = /\b(vmess|vless|trojan|ss|ssr|hysteria2?|tuic):\/\/[^\s"']+/gi;
const URL_SECRET_RE = /([?&](?:token|secret|password|passwd|api[_-]?key|key)=)[^&\s"']+/gi;

export interface RemoteCommandResult {
  command: string;
  exit_code: number;
  stdout: string;
  stderr: string;
}

export type RemoteExecutor = (server: ManagedServer, command: string, timeoutMs: number) => Promise<RemoteCommandResult>;

export function sanitizeRemoteText(value: unknown): string {
  return redactSensitiveText(value)
    .replace(PROXY_URI_RE, (_match, scheme) => `${scheme}://[REDACTED]`)
    .replace(URL_SECRET_RE, "$1[REDACTED]");
}

export function truncateRemoteText(value: string, limit = MAX_OUTPUT): string {
  return value.length <= limit ? value : `${value.slice(0, limit)}\n...（输出已截断）`;
}

export function quoteRemote(value: string): string {
  if (/\0|\r|\n/.test(value)) throw new Error("远程参数包含非法控制字符");
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

async function checkedSecret(path: string, label: string): Promise<void> {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error(`${label} 必须是普通文件`);
  if (info.size < 1 || info.size > 128 * 1024) throw new Error(`${label} 大小非法`);
}

async function askpass(value: string): Promise<{ path: string; cleanup(): Promise<void> }> {
  if (!value || /[\r\n\0]/.test(value)) throw new Error("SSH 密码或私钥口令非法");
  const directory = await mkdtemp(join(tmpdir(), "agenelf-ssh-askpass-"));
  const path = join(directory, "askpass.mjs");
  await writeFile(path, "process.stdout.write(process.env.AGENELF_SSH_ASKPASS_VALUE ?? '');\n", { mode: 0o700 });
  await chmod(path, 0o700);
  return { path, cleanup: () => rm(directory, { recursive: true, force: true }) };
}

interface PreparedConnection {
  args: string[];
  environment: NodeJS.ProcessEnv;
  cleanup(): Promise<void>;
}

export class OpenSshTransport {
  readonly catalog: ServerCatalog;

  constructor(catalog: ServerCatalog) {
    this.catalog = catalog;
  }

  private async prepare(server: ManagedServer): Promise<PreparedConnection> {
    const args = [
      "-p", String(server.port),
      "-o", `ConnectTimeout=${server.connectTimeout}`,
      "-o", "ConnectionAttempts=1",
      "-o", "ServerAliveInterval=10",
      "-o", "ServerAliveCountMax=1",
      "-o", "LogLevel=ERROR",
      "-o", "BatchMode=no"
    ];
    const environment: NodeJS.ProcessEnv = { ...process.env };
    let askpassFile: { path: string; cleanup(): Promise<void> } | null = null;
    const knownHosts = this.catalog.secretPath(server.knownHosts);
    if (server.allowUnknownHostKey) {
      args.push("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null");
    } else {
      await checkedSecret(knownHosts, "known_hosts");
      args.push("-o", "StrictHostKeyChecking=yes", "-o", `UserKnownHostsFile=${knownHosts}`);
    }
    if (server.auth.type === "private_key") {
      const identity = this.catalog.secretPath(server.auth.privateKey);
      await checkedSecret(identity, "SSH 私钥");
      args.push("-o", "IdentitiesOnly=yes", "-i", identity);
      if (server.auth.passphraseEnv) {
        const value = String(process.env[server.auth.passphraseEnv] ?? "");
        if (!value) throw new Error(`SSH 私钥口令环境变量未设置：${server.auth.passphraseEnv}`);
        askpassFile = await askpass(value);
        environment.AGENELF_SSH_ASKPASS_VALUE = value;
      }
    } else {
      const value = String(process.env[server.auth.passwordEnv] ?? "");
      if (!value) throw new Error(`SSH 密码环境变量未设置：${server.auth.passwordEnv}`);
      askpassFile = await askpass(value);
      environment.AGENELF_SSH_ASKPASS_VALUE = value;
      args.push("-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no");
    }
    if (askpassFile) {
      environment.SSH_ASKPASS = askpassFile.path;
      environment.SSH_ASKPASS_REQUIRE = "force";
      environment.DISPLAY = environment.DISPLAY || "agenelf:0";
    }
    args.push(`${server.username}@${server.host}`);
    return {
      args,
      environment,
      cleanup: async () => {
        delete environment.AGENELF_SSH_ASKPASS_VALUE;
        await askpassFile?.cleanup();
      }
    };
  }

  async run(server: ManagedServer, remoteCommand: string, timeoutMs: number, stdinText?: string): Promise<RemoteCommandResult> {
    const prepared = await this.prepare(server);
    const args = [...prepared.args, remoteCommand];
    try {
      return await new Promise<RemoteCommandResult>((resolvePromise, reject) => {
        const child = spawn("ssh", args, {
          env: prepared.environment,
          shell: false,
          stdio: [stdinText === undefined ? "ignore" : "pipe", "pipe", "pipe"]
        });
        let stdout = "";
        let stderr = "";
        let total = 0;
        let settled = false;
        const finish = (value: RemoteCommandResult) => {
          if (settled) return;
          settled = true;
          resolvePromise(value);
        };
        const collect = (kind: "stdout" | "stderr", chunk: Buffer) => {
          total += chunk.length;
          if (total > MAX_OUTPUT * 2) {
            child.kill("SIGKILL");
            return;
          }
          if (kind === "stdout") stdout += chunk.toString("utf8");
          else stderr += chunk.toString("utf8");
        };
        child.stdout.on("data", (chunk: Buffer) => collect("stdout", chunk));
        child.stderr.on("data", (chunk: Buffer) => collect("stderr", chunk));
        const timer = setTimeout(() => child.kill("SIGKILL"), Math.max(1_000, Math.min(timeoutMs, 20 * 60_000)));
        child.once("error", (error) => {
          clearTimeout(timer);
          if (!settled) {
            settled = true;
            reject(error);
          }
        });
        child.once("close", (code, signal) => {
          clearTimeout(timer);
          finish({
            command: remoteCommand,
            exit_code: typeof code === "number" ? code : signal ? 124 : 126,
            stdout: truncateRemoteText(sanitizeRemoteText(stdout)),
            stderr: truncateRemoteText(sanitizeRemoteText(stderr))
          });
        });
        if (stdinText !== undefined) child.stdin.end(stdinText, "utf8");
      });
    } finally {
      await prepared.cleanup();
    }
  }

  async writeText(server: ManagedServer, remotePath: string, content: string, timeoutMs = 60_000): Promise<RemoteCommandResult> {
    if (Buffer.byteLength(content, "utf8") > 1024 * 1024) throw new Error("远程文本超过 1 MiB 上限");
    return this.run(server, `umask 077; cat > ${quoteRemote(remotePath)}`, timeoutMs, content);
  }
}

export function createOpenSshExecutor(catalog: ServerCatalog): RemoteExecutor {
  const transport = new OpenSshTransport(catalog);
  return (server, command, timeoutMs) => transport.run(server, command, timeoutMs);
}
