import { appendLine } from "./fs-store.ts";
import type { ExecutionMode, Risk, ToolExecutionContract } from "./types.ts";

export interface PolicyDecision {
  allowed: boolean;
  reason: string;
  risk: Risk;
  executionMode: ExecutionMode;
  approval: "none" | "exact" | "host" | "impossible";
}

const AGENT_SUBJECTS = new Set(["agent", "api", "http", "web", "cli"]);

export class PolicyEngine {
  readonly auditPath: string;

  constructor(root: string) {
    this.auditPath = `${root}/logs/node-policy-audit.log`;
  }

  evaluate(contract: ToolExecutionContract | null, subject: string): PolicyDecision {
    if (!contract) {
      return { allowed: false, reason: "工具缺少 execution contract", risk: "forbidden", executionMode: "forbidden", approval: "impossible" };
    }
    const { risk, executionMode } = contract;
    if (risk === "forbidden" || executionMode === "forbidden") {
      return { allowed: false, reason: "安全红线禁止执行", risk, executionMode, approval: "impossible" };
    }
    if (executionMode === "pure") {
      return { allowed: true, reason: "纯计算或只读目录能力", risk, executionMode, approval: "none" };
    }
    if (executionMode === "local_state") {
      const allowed = AGENT_SUBJECTS.has(subject);
      return { allowed, reason: allowed ? "允许写入主人本地受控状态" : "未知调用主体", risk, executionMode, approval: "none" };
    }
    if (executionMode === "queued_runner") {
      return { allowed: AGENT_SUBJECTS.has(subject), reason: "仅允许提交不可变请求，由独立 Runner 执行", risk, executionMode, approval: risk === "read" ? "none" : "exact" };
    }
    if (executionMode === "controlled_sandbox") {
      const allowed = subject === "agent" || subject === "cli";
      return { allowed, reason: allowed ? "允许进入受控沙盒" : "该渠道不能启动代码沙盒", risk, executionMode, approval: "exact" };
    }
    if (executionMode === "host_controlled") {
      const allowed = subject === "cli" || subject === "host";
      return { allowed, reason: allowed ? "仅主人终端可调用" : "需要宿主机主人入口", risk, executionMode, approval: "host" };
    }
    return { allowed: false, reason: "未知 execution mode", risk: "forbidden", executionMode: "forbidden", approval: "impossible" };
  }

  async audit(tool: string, subject: string, contract: ToolExecutionContract | null, decision: PolicyDecision): Promise<void> {
    const line = JSON.stringify({
      at: new Date().toISOString(),
      tool,
      subject,
      capability: contract?.capability ?? "unclassified",
      operation: contract?.operation ?? "unclassified",
      risk: decision.risk,
      execution_mode: decision.executionMode,
      allowed: decision.allowed,
      reason: decision.reason
    });
    await appendLine(this.auditPath, line);
  }
}
