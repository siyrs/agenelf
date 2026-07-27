import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { AgenelfAgent } from "../../../packages/core/src/agent.ts";

const COMMANDS = [
  ["/help", "显示命令"], ["/status", "Node Runtime 状态"], ["/skills", "能力与工具"],
  ["/resources", "按需资源目录"], ["/runs", "当前运行列表"], ["/quit", "退出"]
] as const;

export async function runCli(root = process.env.AGENELF_ROOT || process.cwd()): Promise<void> {
  const agent = new AgenelfAgent(resolve(root));
  await agent.initialize();
  const rl = createInterface({ input, output });
  console.log(`Agenelf Node Runtime ${String((await agent.status()).version)} · ${agent.model.config.model}`);
  console.log("输入 /help 查看命令。\n");
  try {
    while (true) {
      const line = (await rl.question("你 > ")).trim();
      if (!line) continue;
      if (line === "/quit" || line === "/exit") break;
      if (line === "/help" || line === "/") { console.table(COMMANDS.map(([command, description]) => ({ command, description }))); continue; }
      if (line === "/status") { console.log(JSON.stringify(await agent.status(), null, 2)); continue; }
      if (line === "/skills") { console.log(JSON.stringify(agent.registry.catalog(), null, 2)); continue; }
      if (line === "/resources") { console.log(JSON.stringify(agent.resources.catalog(), null, 2)); continue; }
      if (line === "/runs") { console.log(JSON.stringify(agent.events.list(), null, 2)); continue; }
      if (line.startsWith("/")) { console.log(`未知命令：${line}`); continue; }
      try { console.log(`\nAgenelf > ${await agent.chat(line, { sessionId: "cli", subject: "cli" })}\n`); }
      catch (error) { console.error(`任务失败：${error instanceof Error ? error.message : String(error)}`); }
    }
  } finally { rl.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) runCli().catch((error) => { console.error(error); process.exitCode = 1; });
