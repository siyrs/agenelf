import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { AgenelfAgent } from "../../../packages/core/src/agent.ts";
import { loadApprovalKey, OwnerApprovalStore } from "../../../packages/core/src/owner-approval.ts";

const BUILTIN_COMMANDS = [
  ["/help", "显示命令与 Prompt Templates"],
  ["/status", "Node Runtime 状态"],
  ["/skills", "能力与工具"],
  ["/resources", "按需资源目录"],
  ["/prompts", "Markdown Prompt Templates"],
  ["/validation", "验证检查与套件"],
  ["/approvals", "列出待审批请求"],
  ["/approve", "签名批准：/approve [op-id] [reason]"],
  ["/deny", "签名拒绝：/deny [op-id] [reason]"],
  ["/runs", "当前运行列表"],
  ["/quit", "退出"]
] as const;

function approvalInput(line: string): { action: "approve" | "deny"; requestId: string; reason: string } | null {
  const match = line.match(/^\/(approve|deny)(?:\s+((?:op|auth)-[0-9a-f]{16}))?(?:\s+([\s\S]*))?$/);
  if (!match) return null;
  return { action: match[1] as "approve" | "deny", requestId: match[2] || "", reason: (match[3] || "").trim() };
}

export async function runCli(root = process.env.AGENELF_ROOT || process.cwd()): Promise<void> {
  const resolvedRoot = resolve(root);
  const agent = new AgenelfAgent(resolvedRoot);
  const approvals = new OwnerApprovalStore(resolvedRoot);
  await Promise.all([agent.initialize(), approvals.initialize()]);
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
  console.log("输入 / 或 /help 查看命令；审批必须通过主人 CLI 的 /approve 或 /deny 显式签名。\n");
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
      if (line === "/approvals") {
        const pending = await approvals.listPending();
        console.log(pending.length ? JSON.stringify(pending, null, 2) : "当前没有待审批请求");
        continue;
      }
      const approval = approvalInput(line);
      if (approval) {
        try {
          const resolved = await approvals.resolvePending(approval.requestId);
          const key = await loadApprovalKey();
          const command = await approvals.submitCommand(String(resolved.selected.id), key, {
            action: approval.action,
            reason: approval.reason,
            decidedBy: "owner-cli",
            duplicates: resolved.duplicates
          });
          console.log(JSON.stringify(await approvals.waitForCommandResult(String(command.id), 15), null, 2));
        } catch (error) {
          console.error(`审批失败：${error instanceof Error ? error.message : String(error)}`);
        }
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
