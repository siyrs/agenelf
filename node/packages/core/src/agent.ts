import { AgentEventHub, type RunEventStream } from "./agent-events.ts";
import { sha256 } from "./canonical.ts";
import { MemoryStore } from "./memory-store.ts";
import { ModelGateway } from "./model-gateway.ts";
import { sanitizeJson } from "./privacy.ts";
import { PromptTemplateLoader } from "./prompt-templates.ts";
import { ResourceLoader } from "./resource-loader.ts";
import { SessionLedgerStore } from "./session-ledger.ts";
import { SkillRegistry } from "./skill-registry.ts";
import { ValidationQueue } from "./validation.ts";
import type { ChatMessage, JsonObject, JsonValue, ToolCall } from "./types.ts";
import { builtinSkills } from "../../skills/src/builtin.ts";

export interface ChatRun {
  stream: RunEventStream;
  completion: Promise<string>;
}

export class AgenelfAgent {
  readonly root: string;
  readonly registry: SkillRegistry;
  readonly model: ModelGateway;
  readonly events: AgentEventHub;
  readonly ledger: SessionLedgerStore;
  readonly memory: MemoryStore;
  readonly resources: ResourceLoader;
  readonly prompts: PromptTemplateLoader;
  readonly validation: ValidationQueue;
  private readonly sessionChains = new Map<string, Promise<void>>();
  private initialized = false;

  constructor(root = process.env.AGENELF_ROOT || process.cwd()) {
    this.root = root;
    this.registry = new SkillRegistry(root);
    this.model = new ModelGateway();
    this.events = new AgentEventHub(root);
    this.ledger = new SessionLedgerStore(root);
    this.memory = new MemoryStore(root);
    this.resources = new ResourceLoader(root);
    this.prompts = new PromptTemplateLoader(root);
    this.validation = new ValidationQueue(root);
  }

  async initialize(): Promise<void> {
    if (this.initialized) return;
    await Promise.all([this.resources.discover(), this.prompts.discover(), this.validation.initialize()]);
    for (const skill of builtinSkills(this.root, () => this.status(), this.validation, this.prompts)) this.registry.register(skill);
    this.initialized = true;
  }

  async status(): Promise<JsonObject> {
    const validationCatalog = this.validation.catalog();
    return {
      status: "ok",
      runtime: "node-typescript",
      version: "0.10.0",
      node: process.version,
      model: this.model.config.model,
      model_ready: this.model.ready,
      skills: this.registry.catalog().length,
      tools: this.registry.allTools().length,
      resources: this.resources.catalog().length,
      prompts: this.prompts.catalog().length,
      validation_checks: Array.isArray(validationCatalog.checks) ? validationCatalog.checks.length : 0,
      validation_suites: Array.isArray(validationCatalog.suites) ? validationCatalog.suites.length : 0,
      runs: this.events.list().length,
      compatibility: { legacy_api: Boolean(process.env.AGENELF_LEGACY_API_URL) },
      security: {
        policy_default: "fail-closed",
        secrets_in_agent: false,
        validation_alias_only: true,
        prompt_code_execution: false,
        runner_protocol: "immutable-file-queue-compatible"
      }
    };
  }

  startChat(message: string, options: { sessionId?: string; subject?: string } = {}): ChatRun {
    const sessionId = options.sessionId?.trim() || "default";
    const subject = options.subject?.trim() || "api";
    const stream = this.events.create(sessionId);
    const prior = this.sessionChains.get(sessionId) ?? Promise.resolve();
    const completion = prior.catch(() => undefined).then(() => this.executeChat(stream, message, subject));
    this.sessionChains.set(sessionId, completion.then(() => undefined, () => undefined));
    return { stream, completion };
  }

  async chat(message: string, options: { sessionId?: string; subject?: string } = {}): Promise<string> {
    return this.startChat(message, options).completion;
  }

  private async history(sessionId: string, limit = 20): Promise<ChatMessage[]> {
    const entries = await this.ledger.entries(sessionId, { type: "message", limit });
    return entries.flatMap((entry) => {
      const role = String(entry.payload.role ?? "");
      const content = entry.payload.content;
      if (!(["user", "assistant"] as string[]).includes(role) || typeof content !== "string") return [];
      return [{ role: role as "user" | "assistant", content }];
    });
  }

  private async systemPrompt(): Promise<string> {
    const memory = await this.memory.promptBlock(30);
    return [
      "你是 Agenelf Node Runtime，一个证据驱动、可审计、可持续改进的个人智能体。",
      "安全规则：不得直接读取主人 secrets，不得绕过 Policy、审批、Runner 或证据链。",
      "执行规则：需要外部副作用时只能提交精确、限时、不可变请求给独立 Runner。",
      "验证规则：只能选择主人 validation.yaml 中的别名；URL、Host、断言和网络执行属于独立 Validation Runner。",
      "Prompt Templates 只展开 Markdown 文本，不执行脚本、扩展代码或外部动作。",
      "完成声明必须基于工具结果、Runner 结果、验证或晋升证据。",
      `当前能力目录：${JSON.stringify(this.registry.catalog())}`,
      `按需资源目录（只含元数据）：${JSON.stringify(this.resources.catalog())}`,
      `Prompt Templates（只含元数据）：${JSON.stringify(this.prompts.catalog())}`,
      memory
    ].filter(Boolean).join("\n\n");
  }

  private safeToolResult(value: JsonValue): JsonValue {
    return sanitizeJson(value, "tool_result", 8, []).value;
  }

  private async executeChat(stream: RunEventStream, userInput: string, subject: string): Promise<string> {
    const text = String(userInput ?? "").trim();
    if (!text) throw new Error("message 不能为空");
    await this.initialize();
    try {
      await stream.emit("run.started", { subject, model: this.model.config.model });
      const historical = await this.history(stream.sessionId, 20);
      await this.ledger.append({ sessionId: stream.sessionId, type: "message", origin: "owner", payload: { role: "user", content: text, run_id: stream.runId } });
      const messages: ChatMessage[] = [
        { role: "system", content: await this.systemPrompt() },
        ...historical,
        { role: "user", content: text }
      ];
      const tools = this.registry.allTools();
      const maxRounds = Math.max(1, Math.min(Number(process.env.AGENELF_NODE_MAX_TOOL_ROUNDS ?? 16), 64));

      for (let round = 1; round <= maxRounds; round += 1) {
        await stream.emit("turn.started", { round });
        await stream.emit("reasoning.started", { round, model: this.model.config.model });
        let content = "";
        let reasoning = "";
        const response = await this.model.streamChat(messages, tools, {
          onContentDelta: async (delta) => { content += delta; await stream.emit("message.delta", { round, delta }); },
          onReasoningDelta: async (delta) => { reasoning += delta; await stream.emit("reasoning.delta", { round, delta }); }
        });
        await stream.emit("reasoning.completed", { round, available: Boolean(response.reasoningContent || reasoning), chars: (response.reasoningContent || reasoning).length });
        const finalContent = response.content ?? content;
        if (!response.toolCalls.length) {
          const reply = finalContent || "（未获得有效回复）";
          await stream.emit("message.completed", { round, text: reply });
          await this.ledger.append({ sessionId: stream.sessionId, type: "message", origin: "runtime", payload: { role: "assistant", content: reply, run_id: stream.runId } });
          await this.memory.add("episode", `用户：${text.slice(0, 500)} | 助手：${reply.slice(0, 1000)}`);
          await stream.emit("run.settled", { reason: "completed", rounds: round });
          return reply;
        }

        messages.push({ role: "assistant", content: finalContent || null, reasoningContent: response.reasoningContent, toolCalls: response.toolCalls });
        for (const call of response.toolCalls) await this.executeTool(stream, call, messages, subject, round);
      }

      const checkpoint = `达到 Node Runtime 最大工具轮次，已保存 run ${stream.runId} 的事件账本。`;
      await stream.emit("run.checkpointed", { reason: "tool_budget_exhausted", max_rounds: maxRounds });
      await stream.emit("message.completed", { text: checkpoint });
      await stream.emit("run.settled", { reason: "checkpointed" });
      return checkpoint;
    } catch (error) {
      const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      if (!stream.isTerminal) await stream.emit("run.failed", { error: message });
      throw error;
    }
  }

  private async executeTool(stream: RunEventStream, call: ToolCall, messages: ChatMessage[], subject: string, round: number): Promise<void> {
    const tool = this.registry.getTool(call.name);
    const contract = tool?.contract ?? null;
    await stream.emit("tool.preflight", {
      round, call_id: call.id, tool: call.name,
      capability: contract?.capability ?? "unclassified",
      operation: contract?.operation ?? "unclassified",
      risk: contract?.risk ?? "forbidden",
      execution_mode: contract?.executionMode ?? "forbidden",
      argument_hash: sha256(call.arguments as unknown as JsonValue)
    });
    await stream.emit("tool.started", { round, call_id: call.id, tool: call.name });
    const rawResult = await this.registry.dispatch(call.name, call.arguments, { root: this.root, subject, sessionId: stream.sessionId, runId: stream.runId });
    const result = this.safeToolResult(rawResult);
    const serialized = JSON.stringify(result);
    await stream.emit("tool.completed", { round, call_id: call.id, tool: call.name, result_preview: serialized.slice(0, 4000) });
    messages.push({ role: "tool", name: call.name, toolCallId: call.id, content: serialized });
  }
}
