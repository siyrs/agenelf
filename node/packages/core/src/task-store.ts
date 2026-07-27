import { readdir } from "node:fs/promises";
import { join } from "node:path";
import { atomicWriteJson, readJson, withDirectoryLock } from "./fs-store.ts";
import { randomId, sha256 } from "./canonical.ts";
import { redactSensitiveText } from "./privacy.ts";
import type { JsonObject, JsonValue } from "./types.ts";

export type TaskStatus = "planned" | "running" | "waiting_approval" | "paused" | "verifying" | "completed" | "failed" | "cancelled";
export interface NodeTask {
  schema_version: 1;
  id: string;
  title: string;
  goal: string;
  status: TaskStatus;
  revision: number;
  created_at: string;
  updated_at: string;
  acceptance_criteria: string[];
  evidence_refs: string[];
  definition_hash: string;
}

export class TaskStore {
  readonly directory: string;
  constructor(root: string) { this.directory = join(root, "data", "node-tasks"); }
  private path(id: string) { if (!/^ntask-[0-9a-f]{16}$/.test(id)) throw new Error("非法 task id"); return join(this.directory, `${id}.json`); }

  async create(input: { title: string; goal: string; acceptanceCriteria?: string[] }): Promise<NodeTask> {
    const title = redactSensitiveText(input.title).trim().slice(0, 300);
    const goal = redactSensitiveText(input.goal).trim().slice(0, 4000);
    if (!title || !goal) throw new Error("title 与 goal 不能为空");
    const acceptance = (input.acceptanceCriteria ?? []).map((item) => redactSensitiveText(item).trim().slice(0, 1000)).filter(Boolean).slice(0, 20);
    const now = new Date().toISOString();
    const task: NodeTask = {
      schema_version: 1,
      id: randomId("ntask-", 16),
      title,
      goal,
      status: "planned",
      revision: 1,
      created_at: now,
      updated_at: now,
      acceptance_criteria: acceptance,
      evidence_refs: [],
      definition_hash: sha256({ title, goal, acceptance_criteria: acceptance } as unknown as JsonValue)
    };
    await atomicWriteJson(this.path(task.id), task as unknown as JsonObject, true);
    return task;
  }

  async get(id: string): Promise<NodeTask> {
    const task = await readJson<NodeTask | null>(this.path(id), null);
    if (!task) throw new Error(`task 不存在：${id}`);
    return task;
  }

  async list(limit = 100): Promise<NodeTask[]> {
    try {
      const files = (await readdir(this.directory)).filter((name) => /^ntask-[0-9a-f]{16}\.json$/.test(name));
      const tasks = await Promise.all(files.map((name) => readJson<NodeTask | null>(join(this.directory, name), null)));
      return tasks.filter((task): task is NodeTask => Boolean(task)).sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, Math.max(0, Math.min(limit, 500)));
    } catch { return []; }
  }

  async transition(id: string, status: TaskStatus, expectedRevision?: number): Promise<NodeTask> {
    const path = this.path(id);
    return withDirectoryLock(`${path}.lock`, async () => {
      const task = await this.get(id);
      if (expectedRevision !== undefined && task.revision !== expectedRevision) throw new Error(`revision 冲突：${task.revision}`);
      const transitions: Record<TaskStatus, TaskStatus[]> = {
        planned: ["running", "paused", "cancelled"],
        running: ["waiting_approval", "paused", "verifying", "failed", "cancelled"],
        waiting_approval: ["running", "failed", "cancelled"],
        paused: ["running", "cancelled"],
        verifying: ["completed", "running", "failed", "cancelled"],
        completed: [], failed: ["planned", "cancelled"], cancelled: []
      };
      if (!transitions[task.status].includes(status)) throw new Error(`非法状态迁移：${task.status} -> ${status}`);
      task.status = status;
      task.revision += 1;
      task.updated_at = new Date().toISOString();
      await atomicWriteJson(path, task as unknown as JsonObject);
      return task;
    });
  }
}
