import { MemoryStore } from "../../core/src/memory-store.ts";
import { OperationQueue } from "../../core/src/operation-queue.ts";
import { PromptTemplateLoader } from "../../core/src/prompt-templates.ts";
import { SecretChatClient } from "../../core/src/secret-chat-client.ts";
import { SessionLedgerStore } from "../../core/src/session-ledger.ts";
import { TaskStore, type TaskStatus } from "../../core/src/task-store.ts";
import { ValidationQueue } from "../../core/src/validation.ts";
import type { JsonObject, JsonValue, SkillDescriptor } from "../../core/src/types.ts";

export function builtinSkills(
  root: string,
  statusProvider: () => Promise<JsonObject>,
  validation: ValidationQueue | undefined,
  prompts: PromptTemplateLoader,
  secretChat?: SecretChatClient
): SkillDescriptor[] {
  const memory = new MemoryStore(root);
  const tasks = new TaskStore(root);
  const ledger = new SessionLedgerStore(root);
  const operations = new OperationQueue(root);

  const skills: SkillDescriptor[] = [
    {
      id: "agent.runtime",
      name: "Node Runtime",
      description: "运行状态、能力目录与健康检查。",
      version: "0.10.0",
      domain: "runtime",
      trust: "builtin",
      tools: [
        {
          name: "runtime_status", description: "查看 Agenelf Node Runtime 状态。",
          inputSchema: { type: "object", properties: {}, additionalProperties: false },
          contract: { capability: "agent.runtime", operation: "status", risk: "read", executionMode: "pure" },
          handler: async () => statusProvider()
        }
      ]
    },
    {
      id: "owner.memory",
      name: "主人记忆",
      description: "脱敏保存和检索主人事实、偏好与任务经验。",
      version: "0.10.0",
      domain: "owner",
      trust: "builtin",
      tools: [
        {
          name: "remember_owner_context", description: "保存一条主人事实或偏好。",
          inputSchema: { type: "object", properties: { kind: { type: "string" }, content: { type: "string" } }, required: ["kind", "content"], additionalProperties: false },
          contract: { capability: "owner.context", operation: "remember", risk: "change", executionMode: "local_state" },
          handler: async (args) => memory.add(String(args.kind ?? "fact"), String(args.content ?? "")) as unknown as JsonObject
        },
        {
          name: "recall_owner_context", description: "按关键词检索主人记忆。",
          inputSchema: { type: "object", properties: { query: { type: "string" }, limit: { type: "integer" } }, required: ["query"], additionalProperties: false },
          contract: { capability: "owner.context", operation: "recall", risk: "read", executionMode: "pure" },
          handler: async (args) => ({ entries: await memory.recall(String(args.query ?? ""), Number(args.limit ?? 5)) }) as unknown as JsonObject
        }
      ]
    },
    {
      id: "agent.tasks",
      name: "Node Task Store",
      description: "带 revision 和状态机的 Node 迁移任务存储。",
      version: "0.10.0",
      domain: "workflow",
      trust: "builtin",
      tools: [
        {
          name: "node_task_create", description: "创建带验收标准的任务。",
          inputSchema: { type: "object", properties: { title: { type: "string" }, goal: { type: "string" }, acceptance_criteria: { type: "array", items: { type: "string" } } }, required: ["title", "goal"], additionalProperties: false },
          contract: { capability: "agent.workflow", operation: "create", risk: "change", executionMode: "local_state" },
          handler: async (args) => tasks.create({ title: String(args.title ?? ""), goal: String(args.goal ?? ""), acceptanceCriteria: Array.isArray(args.acceptance_criteria) ? args.acceptance_criteria.map(String) : [] }) as unknown as JsonObject
        },
        {
          name: "node_task_list", description: "列出 Node 任务。",
          inputSchema: { type: "object", properties: { limit: { type: "integer" } }, additionalProperties: false },
          contract: { capability: "agent.workflow", operation: "list", risk: "read", executionMode: "pure" },
          handler: async (args) => ({ tasks: await tasks.list(Number(args.limit ?? 100)) }) as unknown as JsonObject
        },
        {
          name: "node_task_transition", description: "按 revision 推进 Node 任务状态。",
          inputSchema: { type: "object", properties: { id: { type: "string" }, status: { type: "string" }, expected_revision: { type: "integer" } }, required: ["id", "status"], additionalProperties: false },
          contract: { capability: "agent.workflow", operation: "transition", risk: "change", executionMode: "local_state" },
          handler: async (args) => tasks.transition(String(args.id ?? ""), String(args.status ?? "") as TaskStatus, args.expected_revision === undefined ? undefined : Number(args.expected_revision)) as unknown as JsonObject
        }
      ]
    },
    {
      id: "agent.session-ledger",
      name: "Session Ledger",
      description: "查询 Pi 风格可分支、可回放、带哈希链的会话账本。",
      version: "0.10.0",
      domain: "session",
      trust: "builtin",
      tools: [
        {
          name: "node_session_ledger_status", description: "验证会话账本完整性。",
          inputSchema: { type: "object", properties: { session_id: { type: "string" } }, required: ["session_id"], additionalProperties: false },
          contract: { capability: "agent.session_ledger", operation: "verify", risk: "read", executionMode: "pure" },
          handler: async (args) => ledger.verify(String(args.session_id ?? "")) as unknown as JsonObject
        }
      ]
    },
    {
      id: "agent.prompt-templates",
      name: "Markdown Prompt Templates",
      description: "Pi 风格 Markdown 模板；主人 local/prompts 可同名覆盖。",
      version: "0.10.0",
      domain: "prompt",
      trust: "builtin",
      tools: [
        {
          name: "prompt_template_catalog", description: "列出可用 Prompt Templates 与斜杠命令。",
          inputSchema: { type: "object", properties: {}, additionalProperties: false },
          contract: { capability: "agent.prompt_templates", operation: "catalog", risk: "read", executionMode: "pure" },
          handler: async () => ({ prompts: prompts.catalog() }) as unknown as JsonObject
        },
        {
          name: "expand_prompt_template", description: "展开模板文本，不执行扩展代码或外部动作。",
          inputSchema: { type: "object", properties: { name: { type: "string" }, input: { type: "string" } }, required: ["name"], additionalProperties: false },
          contract: { capability: "agent.prompt_templates", operation: "expand", risk: "read", executionMode: "pure" },
          handler: async (args) => prompts.expand(String(args.name ?? ""), String(args.input ?? ""))
        }
      ]
    }
  ];

  if (secretChat?.enabled) {
    skills.push({
      id: "owner.secret-chat",
      name: "主人聊天明文密钥",
      description: "主人明确要求时，通过内网 Secret Chat Broker 查看或修改受管服务器 .env 明文；不得主动回显。",
      version: "0.11.0",
      domain: "owner-secrets",
      trust: "owner",
      tools: [
        {
          name: "secret_env_targets",
          description: "列出可在聊天中管理的 .env 密钥目标与稳定席位 ID，不返回明文。",
          inputSchema: { type: "object", properties: {}, additionalProperties: false },
          contract: { capability: "server.secrets", operation: "chat_targets", risk: "read", executionMode: "host_controlled" },
          handler: async () => secretChat.targets()
        },
        {
          name: "secret_env_read_plaintext",
          description: "仅当主人明确要求查看明文时调用。返回指定目标的完整密钥明文；seat_id 为空时返回该目标全部席位。允许在最终聊天回复中原样展示。",
          inputSchema: {
            type: "object",
            properties: { env_target: { type: "string" }, seat_id: { type: "string" } },
            required: ["env_target"],
            additionalProperties: false
          },
          contract: { capability: "server.secrets", operation: "chat_reveal_plaintext", risk: "read", executionMode: "host_controlled" },
          sensitive: true,
          allowSensitiveResult: true,
          handler: async (args) => secretChat.snapshot(String(args.env_target ?? ""), String(args.seat_id ?? ""))
        },
        {
          name: "secret_env_apply_plaintext",
          description: "按主人在当前聊天中的明确指令直接修改受管 .env。changes 可只列出要变更的席位，未列出的席位自动保持不动；set 必须使用主人提供的真实明文，不得编造。confirm_target 必须与 env_target 完全一致。Broker 会原子写入、重载、健康检查并在失败时回滚。",
          inputSchema: {
            type: "object",
            properties: {
              env_target: { type: "string" },
              confirm_target: { type: "string" },
              changes: {
                type: "array",
                minItems: 1,
                items: {
                  type: "object",
                  properties: {
                    seat_id: { type: "string" },
                    action: { type: "string", enum: ["keep", "delete", "set"] },
                    value: { type: "string" }
                  },
                  required: ["seat_id", "action"],
                  additionalProperties: false
                }
              }
            },
            required: ["env_target", "confirm_target", "changes"],
            additionalProperties: false
          },
          contract: { capability: "server.secrets", operation: "chat_apply_plaintext", risk: "change", executionMode: "host_controlled" },
          sensitive: true,
          handler: async (args) => secretChat.apply(
            String(args.env_target ?? ""),
            Array.isArray(args.changes) ? args.changes as JsonValue[] : [],
            String(args.confirm_target ?? "")
          )
        }
      ]
    });
  }

  if (validation) {
    skills.push({
      id: "software.validation",
      name: "Node Software Validation",
      description: "只允许主人配置的验证别名，通过独立 Node Runner 生成可信 HTTP/TCP 证据。",
      version: "0.10.0",
      domain: "validation",
      trust: "builtin",
      tools: [
        {
          name: "node_validation_catalog", description: "列出主人配置的验证检查与套件。",
          inputSchema: { type: "object", properties: {}, additionalProperties: false },
          contract: { capability: "software.validation", operation: "catalog", risk: "read", executionMode: "pure" },
          handler: async () => validation.catalog()
        },
        {
          name: "node_validation_run_check", description: "提交一个主人配置的验证检查别名。",
          inputSchema: { type: "object", properties: { check: { type: "string" }, summary: { type: "string" } }, required: ["check"], additionalProperties: false },
          contract: { capability: "software.validation", operation: "run_check", risk: "read", executionMode: "queued_runner" },
          handler: async (args) => validation.submit("run_check", String(args.check ?? ""), String(args.summary ?? "Node validation check"))
        },
        {
          name: "node_validation_run_suite", description: "提交一个主人配置的验证套件别名。",
          inputSchema: { type: "object", properties: { suite: { type: "string" }, summary: { type: "string" } }, required: ["suite"], additionalProperties: false },
          contract: { capability: "software.validation", operation: "run_suite", risk: "read", executionMode: "queued_runner" },
          handler: async (args) => validation.submit("run_suite", String(args.suite ?? ""), String(args.summary ?? "Node validation suite"))
        },
        {
          name: "node_validation_get", description: "查询验证请求与独立 Runner 的可信结果。",
          inputSchema: { type: "object", properties: { id: { type: "string" } }, required: ["id"], additionalProperties: false },
          contract: { capability: "software.validation", operation: "get_result", risk: "read", executionMode: "pure" },
          handler: async (args) => validation.get(String(args.id ?? ""))
        }
      ]
    });
  }

  skills.push({
    id: "server.operations",
    name: "Runner Queue Bridge",
    description: "按既有 Python Runner 协议提交不可变操作请求，迁移期间保持安全控制面兼容。",
    version: "0.10.0",
    domain: "operations",
    trust: "builtin",
    tools: [
      {
        name: "submit_managed_operation", description: "向独立 Runner 提交精确绑定、限时的操作请求。",
        inputSchema: { type: "object", properties: { capability: { type: "string" }, operation: { type: "string" }, target: { type: "string" }, parameters: { type: "object" }, risk: { type: "string" }, summary: { type: "string" } }, required: ["capability", "operation", "target", "risk", "summary"], additionalProperties: false },
        contract: { capability: "server.operations", operation: "submit", risk: "change", executionMode: "queued_runner" },
        handler: async (args) => operations.submit({ capability: String(args.capability ?? ""), operation: String(args.operation ?? ""), target: String(args.target ?? ""), parameters: (args.parameters && typeof args.parameters === "object" && !Array.isArray(args.parameters) ? args.parameters : {}) as JsonObject, risk: String(args.risk ?? "change") as "read" | "change" | "privileged", summary: String(args.summary ?? "") }) as unknown as JsonObject
      },
      {
        name: "get_managed_operation", description: "查询 Runner 请求、审批和可信结果。",
        inputSchema: { type: "object", properties: { id: { type: "string" } }, required: ["id"], additionalProperties: false },
        contract: { capability: "server.operations", operation: "get_result", risk: "read", executionMode: "pure" },
        handler: async (args) => operations.get(String(args.id ?? ""))
      }
    ]
  });

  return skills;
}
