export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export type JsonObject = { [key: string]: JsonValue };

export type Risk = "read" | "change" | "privileged" | "irreversible" | "forbidden";
export type ExecutionMode =
  | "pure"
  | "local_state"
  | "queued_runner"
  | "controlled_sandbox"
  | "host_controlled"
  | "forbidden";

export interface ToolExecutionContract {
  capability: string;
  operation: string;
  risk: Risk;
  executionMode: ExecutionMode;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: JsonObject;
}

export interface ChatMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  name?: string;
  toolCallId?: string;
  toolCalls?: ToolCall[];
  reasoningContent?: string;
}

export interface ModelResponse {
  content: string | null;
  toolCalls: ToolCall[];
  reasoningContent?: string;
}

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: JsonObject;
  contract: ToolExecutionContract;
  handler: (args: JsonObject, context: ToolContext) => Promise<JsonValue>;
}

export interface ToolContext {
  root: string;
  subject: string;
  sessionId: string;
  runId: string;
}

export interface SkillDescriptor {
  id: string;
  name: string;
  description: string;
  version: string;
  domain: string;
  trust: "builtin" | "owner" | "third_party";
  tools: ToolDefinition[];
}
