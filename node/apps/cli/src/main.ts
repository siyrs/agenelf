import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { AgenelfAgent } from "../../../packages/core/src/agent.ts";

const BUILTIN_COMMANDS = [
  ["/help", "显示命令与 Prompt Templates"],
  ["/status", "Node Runtime 状态"],
  ["/skills", "能力与工具"],
  ["/resources", "按需资源目录"],
  ["/prompts", "Markdown Prompt Templates"],
  ["/validation", "验证检查与套件"],
  ["/runs", "当前运行列表"],
  ["/quit", "退出"]
] as const;

export async function runCli(root = process.env.AGENELF_ROOT || process.cwd()): Promise<void> {
  const agent = new AgenelfAgent(resolve(root));
  await agent.initialize();
  const commands = [...BUILTIN_COMMANDS.map(([command]) => command), ...agent.prompts.commands(), "/exit", "/commands"];
  const rl = createInterface({
    input,
    output,
    completer: (line: string) => {
      const hits = commands.filter((command) => command.startsWith(line));
      return [hits.length ? hits : commands, line];
    }
  });
  console.log(`Agenelf Node Runtime ${String((await agent.status()).version)} · ${agent.model.config.model}`);
  console.log("输入 / 或 /help 查看命令；输入 /plan、/review、/test 使用 Markdown Prompt Templates。\n");
  try {
    while (true) {
      const line = (await rl.question("你 > ")).trim();
      if (!line) continue;
      if (line === "/quit" || line === "/exit") break;
      if (line === "/help" || line === "/commands" || line === "/") {
        const promptRows = agent.prompts.catalog().map((item) => ({ command: String(item.command), description: String(item.description || "Prompt Template") }));
        console.table([...BUILTIN_COMMANDS.map(([command, description]) => ({ command, description })), ...promptRows]);
        continue;
      }
      if (line === "/status") { console.log(JSON.stringify(await agent.status(), null, 2)); continue; }
      if (line === "/skills") { console.log(JSON.stringify(agent.registry.catalog(), null, 2)); continue; }
      if (line === "/resources") { console.log(JSON.stringify(agent.resources.catalog(), null, 2)); continue; }
      if (line === "/prompts") { console.log(JSON.stringify(agent.prompts.catalog(), null, 2)); continue; }
      if (line === "/validation") {
        console.log(agent.isValidationReady() ? JSON.stringify(agent.validation.catalog(), null, 2) : `Validation unavailable: ${agent.validationFailure()}`);
        continue;
      }
      if (line === "/runs") { console.log(JSON.stringify(agent.events.list(), null, 2)); continue; }
      const expanded = agent.prompts.expandCommand(line);
      if (line.startsWith("/") && !expanded) { console.log(`未知命令：${line}`); continue; }
      const message = expanded ? String(expanded.prompt) : line;
      if (expanded) console.log(`已展开 Prompt Template：${String(expanded.name)}\n`);
      try { console.log(`\nAgenelf > ${await agent.chat(message, { sessionId: "cli", subject: "cli" })}\n`); }
      catch (error) { console.error(`任务失败：${error instanceof Error ? error.message : String(error)}`); }
    }
  } finally { rl.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) runCli().catch((error) => { console.error(error); process.exitCode = 1; });
