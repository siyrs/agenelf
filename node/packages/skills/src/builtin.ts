import { MemoryStore } from "../../core/src/memory-store.ts";
import { OperationQueue } from "../../core/src/operation-queue.ts";
import { SessionLedgerStore } from "../../core/src/session-ledger.ts";
import { TaskStore, type TaskStatus } from "../../core/src/task-store.ts";
import type { JsonObject, SkillDescriptor } from "../../core/src/types.ts";

export function builtinSkills(root: string, statusProvider: () => Promise<JsonObject>): SkillDescriptor[] {
  const memory = new MemoryStore(root);
  const tasks = new TaskStore(root);
  const ledger = new SessionLedgerStore(root);
  const operations = new OperationQueue(root);

  return [
    {
      id: "agent.runtime",
      name: "Node Runtime",
      description: "运行状态、能力目录与健康检查。",
      version: "0.9.0",
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
      version: "0.9.0",
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
      version: "0.9.0",
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
      version: "0.9.0",
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
      id: "server.operations",
      name: "Runner Queue Bridge",
      description: "按既有 Python Runner 协议提交不可变操作请求，迁移期间保持安全控制面兼容。",
      version: "0.9.0",
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
    }
  ];
}
