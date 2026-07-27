import { PolicyEngine } from "./policy.ts";
import type { JsonObject, JsonValue, SkillDescriptor, ToolContext, ToolDefinition } from "./types.ts";

export class SkillRegistry {
  readonly policy: PolicyEngine;
  private readonly skills = new Map<string, SkillDescriptor>();
  private readonly tools = new Map<string, ToolDefinition>();

  constructor(root: string) { this.policy = new PolicyEngine(root); }

  register(skill: SkillDescriptor): void {
    if (this.skills.has(skill.id)) throw new Error(`skill 重复：${skill.id}`);
    for (const tool of skill.tools) {
      if (this.tools.has(tool.name)) throw new Error(`tool 重复：${tool.name}`);
    }
    this.skills.set(skill.id, skill);
    for (const tool of skill.tools) this.tools.set(tool.name, tool);
  }

  getTool(name: string): ToolDefinition | undefined { return this.tools.get(name); }
  allTools(): ToolDefinition[] { return [...this.tools.values()]; }

  toolSchemas(): JsonObject[] {
    return this.allTools().map((tool) => ({
      type: "function",
      function: { name: tool.name, description: tool.description, parameters: tool.inputSchema }
    }));
  }

  catalog() {
    return [...this.skills.values()].map((skill) => ({
      id: skill.id,
      name: skill.name,
      description: skill.description,
      version: skill.version,
      domain: skill.domain,
      trust: skill.trust,
      tools: skill.tools.map((tool) => ({ name: tool.name, contract: tool.contract }))
    }));
  }

  async dispatch(name: string, args: JsonObject, context: ToolContext): Promise<JsonValue> {
    const tool = this.tools.get(name);
    if (!tool) return { error: `未知工具 ${name}` };
    const decision = this.policy.evaluate(tool.contract, context.subject);
    await this.policy.audit(name, context.subject, tool.contract, decision);
    if (!decision.allowed) return { error: `策略拒绝工具 ${name}：${decision.reason}`, policy: decision as unknown as JsonObject };
    try {
      return await tool.handler(args, context);
    } catch (error) {
      return { error: `${error instanceof Error ? error.name : "Error"}: ${error instanceof Error ? error.message : String(error)}` };
    }
  }
}
