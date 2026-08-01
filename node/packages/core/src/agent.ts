import { AgentEventHub, type RunEventStream } from "./agent-events.ts";
import { sha256 } from "./canonical.ts";
import { MemoryStore } from "./memory-store.ts";
import { ModelGateway } from "./model-gateway.ts";
import { sanitizeJson } from "./privacy.ts";
import { PromptTemplateLoader } from "./prompt-templates.ts";
import { ResourceLoader } from "./resource-loader.ts";
import { SecretChatClient } from "./secret-chat-client.ts";
import { routeOwnerSecretChat } from "./secret-chat-direct.ts";
import { SessionLedgerStore } from "./session-ledger.ts";
import { SelfOptimizationStore } from "./self-optimization.ts";
import { SkillRegistry } from "./skill-registry.ts";
import { ValidationQueue } from "./validation.ts";
import type { ChatMessage, JsonObject, JsonValue, ToolCall } from "./types.ts";
import { builtinSkills } from "../../skills/src/builtin.ts";

export interface ChatRun {
  stream: RunEventStream;
  completion: Promise<string>;
}

function likelySensitiveOwnerMessage(text: string): boolean {
  if (/(?:明文|密钥|密码|口令|token|api[ _-]?key|secret|credential)/i.test(text)) return true;
  if (/\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,})\b/.test(text)) return true;
  return /(?:修改|替换|更新|设置|删除|新增).{0,80}[A-Za-z0-9_./:+@%=-]{24,}/i.test(text);
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
  readonly optimization: SelfOptimizationStore;
  readonly secretChat: SecretChatClient;
  private readonly sessionChains = new Map<string, Promise<void>>();
  private initialized = false;
  private validationReady = false;
  private validationError = "";

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
    this.optimization = new SelfOptimizationStore(root);
    this.secretChat = new SecretChatClient();
  }

  async initialize(): Promise<void> {
    if (this.initialized) return;
    try {
      await this.validation.initialize();
      this.validationReady = true;
      this.validationError = "";
    } catch (error) {
      this.validationReady = false;
      this.validationError = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    }
    await Promise.all([this.resources.discover(), this.prompts.discover()]);
    for (const skill of builtinSkills(
      this.root,
      () => this.status(),
      this.validationReady ? this.validation : undefined,
      this.prompts,
      this.secretChat.enabled ? this.secretChat : undefined
    )) this.registry.register(skill);
    this.initialized = true;
  }

  isValidationReady(): boolean { return this.validationReady; }

  validationFailure(): string { return this.validationError; }

  async status(): Promise<JsonObject> {
    return {
      status: "ok",
      runtime: "node-typescript",
      version: "0.13.1",
      node: process.version,
      model: this.model.config.model,
      model_ready: this.model.ready,
      skills: this.registry.catalog().length,
      tools: this.registry.allTools().length,
      resources: this.resources.catalog().length,
      prompts: this.prompts.catalog().length,
      runs: this.events.list().length,
      validation: {
        ready: this.validationReady,
        error: this.validationReady ? "" : this.validationError.slice(0, 1_000)
      },
      secret_chat: {
        enabled: this.secretChat.enabled,
        broker: this.secretChat.baseUrl,
        routing: "deterministic-before-model",
        plaintext_in_model_context: false,
        ssh_credentials_mounted_in_agent: false
      },
      optimization: await this.optimization.status(),
      compatibility: { legacy_api: Boolean(process.env.AGENELF_LEGACY_API_URL) },
      security: {
        policy_default: "fail-closed",
        secrets_in_agent: this.secretChat.enabled ? "owner-explicit-via-internal-broker" : false,
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
    const entries = await this.ledger.entries(sessionId, { type: "message", limit: limit * 2 });
    return entries.flatMap((entry) => {
      if (entry.payload.sensitive === true) return [];
      const role = String(entry.payload.role ?? "");
      const content = entry.payload.content;
      if (!("user" === role || "assistant" === role) || typeof content !== "string") return [];
      return [{ role: role as "user" | "assistant", content }];
    }).slice(-limit);
  }

  private async systemPrompt(): Promise<string> {
    const memoryLimit = await this.optimization.effective("agent.memory_prompt_limit", 30);
    const memoryMaxChars = await this.optimization.effective("agent.memory_prompt_max_chars", 8000);
    this.model.config.temperature = await this.optimization.effective("llm.temperature", this.model.config.temperature);
    const memory = await this.memory.promptBlock(memoryLimit, memoryMaxChars);
    const resources = this.resources.catalog();
    const prompts = this.prompts.catalog();
    return [
      "你是 Agenelf Node Runtime，一个证据驱动、可审计、可持续改进的个人智能体。",
      this.secretChat.enabled
        ? "主人已启用聊天明文密钥模式。明确的查看和修改请求会在进入模型前由确定性 Secret Chat 路由处理；模型不得声称 Agenelf 绝对不能展示主人自己的受管密钥。"
        : "安全规则：不得直接读取主人 secrets，不得绕过 Policy、审批、Runner 或证据链。",
      "Secret Chat Broker 只允许固定受管目标和席位；不得编造密钥、扩大到未配置文件或输出 SSH 凭据。",
      "执行规则：除已启用的主人 Secret Chat Broker 外，需要外部副作用时只能提交精确、限时、不可变请求给独立 Runner。",
      "Prompt Templates 只展开 Markdown 文本，不执行脚本、扩展代码或外部动作。",
      "完成声明必须基于工具结果、Runner 结果、验证或晋升证据。",
      `当前能力目录：${JSON.stringify(this.registry.catalog())}`,
      `按需资源目录（只含元数据）：${JSON.stringify(resources)}`,
      `Prompt Templates（只含元数据）：${JSON.stringify(prompts)}`,
      memory
    ].filter(Boolean).join("\n\n");
  }

  private safeToolResult(value: JsonValue, allowSensitiveResult = false): JsonValue {
    return allowSensitiveResult ? value : sanitizeJson(value, "tool_result", 8, []).value;
  }

  private async executeChat(stream: RunEventStream, userInput: string, subject: string): Promise<string> {
    const text = String(userInput ?? "").trim();
    if (!text) throw new Error("message 不能为空");
    await this.initialize();
    const inputSensitive = likelySensitiveOwnerMessage(text);
    let sensitiveRun = inputSensitive;
    try {
      await stream.emit("run.started", { subject, model: this.model.config.model, sensitive: inputSensitive });
      const historical = await this.history(stream.sessionId, 20);
      await this.ledger.append({
        sessionId: stream.sessionId,
        type: "message",
        origin: "owner",
        payload: { role: "user", content: text, run_id: stream.runId, sensitive: inputSensitive }
      });

      const direct = await routeOwnerSecretChat(text, this.secretChat);
      if (direct.handled) {
        sensitiveRun = inputSensitive || direct.sensitive;
        const reply = direct.reply || "Secret Chat 路由已处理，但没有返回内容。";
        await stream.emit("message.completed", {
          round: 0,
          text: reply,
          sensitive: sensitiveRun,
          direct_route: direct.route ?? "diagnostic"
        });
        await this.ledger.append({
          sessionId: stream.sessionId,
          type: "message",
          origin: "runtime",
          payload: {
            role: "assistant",
            content: reply,
            run_id: stream.runId,
            sensitive: sensitiveRun,
            direct_route: direct.route ?? "diagnostic"
          }
        });
        await stream.emit("run.settled", {
          reason: "direct_secret_route",
          rounds: 0,
          sensitive: sensitiveRun,
          route: direct.route ?? "diagnostic"
        });
        return reply;
      }

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
          await stream.emit("message.completed", { round, text: reply, sensitive: sensitiveRun });
          await this.ledger.append({
            sessionId: stream.sessionId,
            type: "message",
            origin: "runtime",
            payload: { role: "assistant", content: reply, run_id: stream.runId, sensitive: sensitiveRun }
          });
          if (!sensitiveRun) await this.memory.add("episode", `用户：${text.slice(0, 500)} | 助手：${reply.slice(0, 1000)}`);
          await stream.emit("run.settled", { reason: "completed", rounds: round, sensitive: sensitiveRun });
          return reply;
        }

        messages.push({ role: "assistant", content: finalContent || null, reasoningContent: response.reasoningContent, toolCalls: response.toolCalls });
        for (const call of response.toolCalls) {
          if (await this.executeTool(stream, call, messages, subject, round)) sensitiveRun = true;
        }
      }

      const checkpoint = `达到 Node Runtime 最大工具轮次，已保存 run ${stream.runId} 的事件账本。`;
      await stream.emit("run.checkpointed", { reason: "tool_budget_exhausted", max_rounds: maxRounds, sensitive: sensitiveRun });
      await stream.emit("message.completed", { text: checkpoint, sensitive: sensitiveRun });
      await stream.emit("run.settled", { reason: "checkpointed", sensitive: sensitiveRun });
      return checkpoint;
    } catch (error) {
      const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      if (!stream.isTerminal) await stream.emit("run.failed", { error: message, sensitive: sensitiveRun });
      throw error;
    }
  }

  private async executeTool(stream: RunEventStream, call: ToolCall, messages: ChatMessage[], subject: string, round: number): Promise<boolean> {
    const tool = this.registry.getTool(call.name);
    const contract = tool?.contract ?? null;
    const sensitive = tool?.sensitive === true || tool?.allowSensitiveResult === true;
    await stream.emit("tool.preflight", {
      round,
      call_id: call.id,
      tool: call.name,
      capability: contract?.capability ?? "unclassified",
      operation: contract?.operation ?? "unclassified",
      risk: contract?.risk ?? "forbidden",
      execution_mode: contract?.executionMode ?? "forbidden",
      argument_hash: sha256(call.arguments as unknown as JsonValue),
      sensitive
    });
    await stream.emit("tool.started", { round, call_id: call.id, tool: call.name, sensitive });
    const rawResult = await this.registry.dispatch(call.name, call.arguments, { root: this.root, subject, sessionId: stream.sessionId, runId: stream.runId });
    const result = this.safeToolResult(rawResult, tool?.allowSensitiveResult === true);
    const serialized = JSON.stringify(result);
    await stream.emit("tool.completed", {
      round,
      call_id: call.id,
      tool: call.name,
      sensitive,
      result_preview: sensitive ? "[SENSITIVE TOOL RESULT OMITTED]" : serialized.slice(0, 4000)
    });
    messages.push({ role: "tool", name: call.name, toolCallId: call.id, content: serialized });
    return sensitive;
  }
}
