import { readdir, stat } from "node:fs/promises";
import { join } from "node:path";
import { atomicWriteJson, readJson } from "./fs-store.ts";
import { randomId, sha256 } from "./canonical.ts";
import { redactSensitiveText } from "./privacy.ts";
import type { JsonObject, JsonValue, Risk } from "./types.ts";

const DEFAULT_TTL: Record<string, number> = { read: 120, change: 1800, privileged: 900 };

export interface OperationRequest {
  schema_version: 1;
  id: string;
  capability: string;
  operation: string;
  target: string;
  parameters: JsonObject;
  risk: Risk;
  summary: string;
  fingerprint: string;
  created_at: string;
  expires_at: string;
  ttl_seconds: number;
  created_by: string;
  reused_existing?: boolean;
}

export class OperationQueue {
  readonly root: string;
  readonly requests: string;
  readonly results: string;
  readonly decisions: string;

  constructor(root: string) {
    this.root = root;
    this.requests = join(root, "data", "ops-requests");
    this.results = join(root, "data", "ops-results");
    this.decisions = join(root, "data", "auth-decisions");
  }

  private payload(input: Pick<OperationRequest, "capability" | "operation" | "target" | "parameters">): JsonObject {
    return {
      capability: input.capability.trim(),
      operation: input.operation.trim(),
      target: input.target.trim(),
      parameters: input.parameters
    };
  }

  private async existingRequests(): Promise<OperationRequest[]> {
    try {
      const names = (await readdir(this.requests)).filter((name) => /^op-[0-9a-f]{16}\.json$/.test(name));
      const rows = await Promise.all(names.map(async (name) => {
        const path = join(this.requests, name);
        return { row: await readJson<OperationRequest | null>(path, null), mtime: (await stat(path)).mtimeMs };
      }));
      return rows.filter((item): item is { row: OperationRequest; mtime: number } => Boolean(item.row)).sort((a, b) => b.mtime - a.mtime).map((item) => item.row);
    } catch { return []; }
  }

  async submit(input: {
    capability: string;
    operation: string;
    target: string;
    parameters?: JsonObject;
    risk: Risk;
    summary: string;
    ttlSeconds?: number;
  }): Promise<OperationRequest> {
    if (!input.operation.trim() || !input.target.trim()) throw new Error("operation 与 target 不能为空");
    if (input.risk === "forbidden" || input.risk === "irreversible") throw new Error("该风险级别不能提交到普通 Runner");
    const parameters = input.parameters ?? {};
    const payload = this.payload({ ...input, parameters });
    const fingerprint = sha256(payload as unknown as JsonValue);
    const now = Date.now();
    for (const request of await this.existingRequests()) {
      if (request.fingerprint !== fingerprint || request.risk !== input.risk) continue;
      if (await readJson(join(this.results, `${request.id}.json`), null) !== null) continue;
      if (Date.parse(request.expires_at) <= now) continue;
      return { ...request, reused_existing: true };
    }
    const ttl = Math.max(15, Math.min(input.ttlSeconds ?? DEFAULT_TTL[input.risk] ?? 1800, 86_400));
    const request: OperationRequest = {
      schema_version: 1,
      id: randomId("op-", 16),
      capability: input.capability.trim(),
      operation: input.operation.trim(),
      target: input.target.trim(),
      parameters,
      risk: input.risk,
      summary: redactSensitiveText(input.summary).trim().slice(0, 2000),
      fingerprint,
      created_at: new Date(now).toISOString(),
      expires_at: new Date(now + ttl * 1000).toISOString(),
      ttl_seconds: ttl,
      created_by: "agenelf-node-agent"
    };
    await atomicWriteJson(join(this.requests, `${request.id}.json`), request as unknown as JsonObject, true);
    return request;
  }

  async get(id: string): Promise<JsonObject> {
    if (!/^op-[0-9a-f]{16}$/.test(id)) throw new Error("非法 operation id");
    const request = await readJson<OperationRequest | null>(join(this.requests, `${id}.json`), null);
    if (!request) return { id, status: "not_found" };
    const result = await readJson<JsonObject | null>(join(this.results, `${id}.json`), null);
    if (result) return { id, status: String(result.status ?? "finished"), request: request as unknown as JsonObject, result };
    if (Date.parse(request.expires_at) <= Date.now()) return { id, status: "expired", request: request as unknown as JsonObject };
    const decision = await readJson<JsonObject | null>(join(this.decisions, `${id}.json`), null);
    const decisionValue = String(decision?.decision ?? "");
    const status = decisionValue === "approve" ? "approved" : decisionValue === "deny" ? "denied" : request.risk === "read" ? "queued" : "awaiting_approval";
    return { id, status, request: request as unknown as JsonObject, decision };
  }
}
