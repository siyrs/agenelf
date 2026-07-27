import type { ChatMessage, JsonObject, ModelResponse, ToolCall, ToolDefinition } from "./types.ts";

export interface ModelConfig {
  baseUrl: string;
  apiKey: string;
  model: string;
  temperature: number;
  timeoutMs: number;
}

export interface StreamCallbacks {
  onContentDelta?: (delta: string) => Promise<void> | void;
  onReasoningDelta?: (delta: string) => Promise<void> | void;
}

function openAiMessages(messages: ChatMessage[]) {
  return messages.map((message) => {
    const value: Record<string, unknown> = { role: message.role, content: message.content };
    if (message.name) value.name = message.name;
    if (message.toolCallId) value.tool_call_id = message.toolCallId;
    if (message.reasoningContent) value.reasoning_content = message.reasoningContent;
    if (message.toolCalls) {
      value.tool_calls = message.toolCalls.map((call) => ({
        id: call.id,
        type: "function",
        function: { name: call.name, arguments: JSON.stringify(call.arguments) }
      }));
    }
    return value;
  });
}

function toolSchemas(tools: ToolDefinition[]) {
  return tools.length ? tools.map((tool) => ({ type: "function", function: { name: tool.name, description: tool.description, parameters: tool.inputSchema } })) : undefined;
}

function parseToolCalls(raw: unknown): ToolCall[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((item, index) => {
    const record = item as Record<string, unknown>;
    const fn = (record.function ?? {}) as Record<string, unknown>;
    let args: JsonObject = {};
    try {
      const parsed = JSON.parse(String(fn.arguments ?? "{}"));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) args = parsed as JsonObject;
    } catch {}
    return { id: String(record.id ?? `call-${index}`), name: String(fn.name ?? ""), arguments: args };
  }).filter((call) => call.name);
}

function requestBody(config: ModelConfig, messages: ChatMessage[], tools: ToolDefinition[], stream: boolean) {
  return {
    model: config.model,
    temperature: config.temperature,
    messages: openAiMessages(messages),
    tools: toolSchemas(tools),
    tool_choice: tools.length ? "auto" : undefined,
    stream
  };
}

export class ModelGateway {
  readonly config: ModelConfig;
  constructor(config: Partial<ModelConfig> = {}) {
    this.config = {
      baseUrl: config.baseUrl ?? process.env.OPENAI_BASE_URL ?? "https://api.openai.com/v1",
      apiKey: config.apiKey ?? process.env.OPENAI_API_KEY ?? "",
      model: config.model ?? process.env.AGENELF_MODEL ?? "gpt-5-mini",
      temperature: config.temperature ?? 0.4,
      timeoutMs: config.timeoutMs ?? 120_000
    };
  }

  get ready() { return Boolean(this.config.apiKey); }

  private async request(body: unknown): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.config.timeoutMs);
    try {
      const response = await fetch(`${this.config.baseUrl.replace(/\/$/, "")}/chat/completions`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${this.config.apiKey}` },
        signal: controller.signal,
        body: JSON.stringify(body)
      });
      if (!response.ok) throw new Error(`模型请求失败 HTTP ${response.status}: ${(await response.text()).slice(0, 1000)}`);
      return response;
    } finally {
      clearTimeout(timer);
    }
  }

  async chat(messages: ChatMessage[], tools: ToolDefinition[]): Promise<ModelResponse> {
    if (!this.ready) return this.mock(messages, tools);
    const response = await this.request(requestBody(this.config, messages, tools, false));
    const body = await response.json() as Record<string, unknown>;
    const choices = body.choices as Array<Record<string, unknown>>;
    const message = (choices?.[0]?.message ?? {}) as Record<string, unknown>;
    return {
      content: message.content == null ? null : String(message.content),
      reasoningContent: message.reasoning_content == null ? undefined : String(message.reasoning_content),
      toolCalls: parseToolCalls(message.tool_calls)
    };
  }

  async streamChat(messages: ChatMessage[], tools: ToolDefinition[], callbacks: StreamCallbacks = {}): Promise<ModelResponse> {
    if (!this.ready) {
      const mocked = this.mock(messages, tools);
      if (mocked.reasoningContent) await callbacks.onReasoningDelta?.(mocked.reasoningContent);
      if (mocked.content) await callbacks.onContentDelta?.(mocked.content);
      return mocked;
    }
    const body = requestBody(this.config, messages, tools, true);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.config.timeoutMs);
    try {
      const response = await fetch(`${this.config.baseUrl.replace(/\/$/, "")}/chat/completions`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${this.config.apiKey}` },
        signal: controller.signal,
        body: JSON.stringify(body)
      });
      if (!response.ok) throw new Error(`模型流请求失败 HTTP ${response.status}: ${(await response.text()).slice(0, 1000)}`);
      if (!response.body) throw new Error("模型流没有 response body");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      let content = "";
      let reasoning = "";
      const fragments = new Map<number, { id: string; name: string; arguments: string }>();
      while (true) {
        const { done, value } = await reader.read();
        pending += decoder.decode(value ?? new Uint8Array(), { stream: !done });
        const lines = pending.split(/\r?\n/);
        pending = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (!data || data === "[DONE]") continue;
          const payload = JSON.parse(data) as Record<string, unknown>;
          const choices = payload.choices as Array<Record<string, unknown>>;
          const delta = (choices?.[0]?.delta ?? {}) as Record<string, unknown>;
          const contentDelta = typeof delta.content === "string" ? delta.content : "";
          const reasoningDelta = typeof delta.reasoning_content === "string" ? delta.reasoning_content : typeof delta.reasoning === "string" ? delta.reasoning : "";
          if (contentDelta) { content += contentDelta; await callbacks.onContentDelta?.(contentDelta); }
          if (reasoningDelta) { reasoning += reasoningDelta; await callbacks.onReasoningDelta?.(reasoningDelta); }
          if (Array.isArray(delta.tool_calls)) {
            for (const raw of delta.tool_calls as Array<Record<string, unknown>>) {
              const index = Number(raw.index ?? 0);
              const current = fragments.get(index) ?? { id: "", name: "", arguments: "" };
              const fn = (raw.function ?? {}) as Record<string, unknown>;
              if (raw.id) current.id = String(raw.id);
              if (fn.name) current.name += String(fn.name);
              if (fn.arguments) current.arguments += String(fn.arguments);
              fragments.set(index, current);
            }
          }
        }
        if (done) break;
      }
      const toolCalls: ToolCall[] = [...fragments.entries()].sort(([a], [b]) => a - b).map(([index, fragment]) => {
        let args: JsonObject = {};
        try {
          const parsed = JSON.parse(fragment.arguments || "{}");
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) args = parsed as JsonObject;
        } catch {}
        return { id: fragment.id || `call-${index}`, name: fragment.name, arguments: args };
      }).filter((call) => call.name);
      return { content: content || null, reasoningContent: reasoning || undefined, toolCalls };
    } finally { clearTimeout(timer); }
  }

  private mock(messages: ChatMessage[], tools: ToolDefinition[]): ModelResponse {
    const latest = [...messages].reverse().find((message) => message.role === "user")?.content ?? "";
    const latestMessage = messages.at(-1);
    if (latestMessage?.role === "tool") return { content: `已完成工具调用：${latestMessage.content ?? ""}`, toolCalls: [] };
    if (/状态|status|体检|doctor/i.test(latest) && tools.some((tool) => tool.name === "runtime_status")) {
      return { content: null, toolCalls: [{ id: "call-mock-status", name: "runtime_status", arguments: {} }] };
    }
    if (/记住|remember/i.test(latest) && tools.some((tool) => tool.name === "remember_owner_context")) {
      return { content: null, toolCalls: [{ id: "call-mock-memory", name: "remember_owner_context", arguments: { kind: "fact", content: latest } }] };
    }
    return { content: `Agenelf Node Runtime 已接收：${latest}`, toolCalls: [] };
  }
}
