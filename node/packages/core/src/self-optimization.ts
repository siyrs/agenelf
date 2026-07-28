import { readdir, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { appendLine, atomicWriteJson, readJson, withDirectoryLock } from "./fs-store.ts";
import { redactSensitiveText, sanitizeObject } from "./privacy.ts";
import type { JsonObject, JsonValue } from "./types.ts";

export const TUNABLE_PARAMETERS: Record<string, { kind: "int" | "float"; default: number; min: number; max: number; description: string }> = {
  "agent.memory_prompt_limit": { kind: "int", default: 50, min: 10, max: 100, description: "注入系统提示的长期记忆条数上限" },
  "agent.memory_prompt_max_chars": { kind: "int", default: 8000, min: 2000, max: 20000, description: "注入系统提示的记忆块字符数上限" },
  "llm.temperature": { kind: "float", default: 0.6, min: 0, max: 1, description: "模型请求采样温度" }
};

interface OptimizationState {
  schema_version: 1;
  updated_at: string;
  active: Record<string, JsonObject>;
  history: JsonObject[];
  cooldowns: Record<string, string>;
  consciousness_claim: false;
}

function now(): string { return new Date().toISOString(); }
function safeText(value: unknown, limit = 1000): string { return redactSensitiveText(value).trim().slice(0, limit); }
function strings(value: unknown, limit = 10): string[] {
  return Array.isArray(value) ? value.map((item) => safeText(item, 1000)).filter(Boolean).slice(0, limit) : [];
}
function emptyState(): OptimizationState {
  return { schema_version: 1, updated_at: now(), active: {}, history: [], cooldowns: {}, consciousness_claim: false };
}
function parseTime(value: unknown): number {
  const parsed = Date.parse(String(value ?? ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

export class SelfOptimizationStore {
  readonly root: string;
  readonly path: string;
  readonly lockPath: string;
  readonly auditPath: string;
  readonly maxHistory: number;
  readonly cooldownSeconds: number;

  constructor(root: string, options: { maxHistory?: number; cooldownSeconds?: number } = {}) {
    this.root = resolve(root);
    this.path = join(this.root, "local", "self", "optimizations.json");
    this.lockPath = `${this.path}.lock`;
    this.auditPath = join(this.root, "logs", "audit.log");
    this.maxHistory = Math.max(10, Math.min(options.maxHistory ?? 100, 1000));
    this.cooldownSeconds = Math.max(0, Math.min(options.cooldownSeconds ?? Number(process.env.AGENELF_OPTIMIZATION_COOLDOWN_SECONDS ?? 3600), 86_400));
  }

  private normalize(raw: unknown): OptimizationState {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return emptyState();
    const value = raw as Record<string, unknown>;
    const state = emptyState();
    if (value.active && typeof value.active === "object" && !Array.isArray(value.active)) {
      for (const [key, item] of Object.entries(value.active as Record<string, unknown>)) {
        if (!(key in TUNABLE_PARAMETERS) || !item || typeof item !== "object" || Array.isArray(item)) continue;
        try {
          const normalized = this.validateValue(key, (item as Record<string, unknown>).value);
          state.active[key] = sanitizeObject({
            value: normalized,
            reason: safeText((item as Record<string, unknown>).reason),
            applied_at: safeText((item as Record<string, unknown>).applied_at, 100),
            evidence: strings((item as Record<string, unknown>).evidence),
            health_at_apply: (item as Record<string, unknown>).health_at_apply && typeof (item as Record<string, unknown>).health_at_apply === "object" ? (item as Record<string, unknown>).health_at_apply : {}
          });
        } catch { /* invalid owner-local override is ignored */ }
      }
    }
    if (Array.isArray(value.history)) state.history = value.history.flatMap((item) => item && typeof item === "object" && !Array.isArray(item) ? [sanitizeObject(item)] : []).slice(-this.maxHistory);
    if (value.cooldowns && typeof value.cooldowns === "object" && !Array.isArray(value.cooldowns)) {
      for (const [key, at] of Object.entries(value.cooldowns as Record<string, unknown>)) if (key in TUNABLE_PARAMETERS && parseTime(at)) state.cooldowns[key] = String(at);
    }
    state.updated_at = safeText(value.updated_at, 100) || now();
    return state;
  }

  private async load(): Promise<OptimizationState> {
    return this.normalize(await readJson<unknown>(this.path, {}));
  }
  private async save(state: OptimizationState): Promise<void> {
    state.updated_at = now();
    state.history = state.history.slice(-this.maxHistory);
    await atomicWriteJson(this.path, state as unknown as JsonObject);
  }
  private async audit(event: string, detail: string): Promise<void> {
    await appendLine(this.auditPath, `[${now()}] [${event}] ${safeText(detail, 2000)}`);
  }

  private validateValue(key: string, value: unknown): number {
    const spec = TUNABLE_PARAMETERS[key];
    if (!spec) throw new Error(`参数不在白名单：${key}`);
    if (typeof value === "boolean") throw new Error(`${key} 不能是布尔值`);
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) throw new Error(`${key} 必须是数字`);
    if (spec.kind === "int" && !Number.isInteger(parsed)) throw new Error(`${key} 必须是整数`);
    if (parsed < spec.min || parsed > spec.max) throw new Error(`${key} 允许范围是 [${spec.min}, ${spec.max}]`);
    return parsed;
  }
  private remaining(state: OptimizationState, key: string): number {
    const last = parseTime(state.cooldowns[key]);
    return last ? Math.max(0, this.cooldownSeconds * 1000 - (Date.now() - last)) : 0;
  }

  async effective(key: string, fallback?: number): Promise<number> {
    const spec = TUNABLE_PARAMETERS[key];
    if (!spec) throw new Error(`参数不在白名单：${key}`);
    const state = await this.load();
    const item = state.active[key];
    return item && typeof item.value === "number" ? item.value : fallback ?? spec.default;
  }

  async status(): Promise<JsonObject> {
    const state = await this.load();
    const parameters = Object.fromEntries(Object.entries(TUNABLE_PARAMETERS).map(([key, spec]) => [key, {
      ...spec,
      effective: state.active[key]?.value ?? spec.default,
      active: Boolean(state.active[key]),
      cooldown_remaining_seconds: Math.ceil(this.remaining(state, key) / 1000)
    }]));
    return {
      schema_version: 1,
      updated_at: state.updated_at,
      parameters,
      active: state.active,
      history: state.history.slice(-20).reverse(),
      cooldown_seconds: this.cooldownSeconds,
      consciousness_claim: false
    };
  }

  async apply(key: string, value: unknown, reason: string, evidence: unknown = []): Promise<{ applied: true; message: string; entry: JsonObject }> {
    const normalizedKey = String(key ?? "").trim();
    const normalizedValue = this.validateValue(normalizedKey, value);
    return withDirectoryLock(this.lockPath, async () => {
      const state = await this.load();
      const remaining = this.remaining(state, normalizedKey);
      if (remaining > 0) throw new Error(`${normalizedKey} 处于冷却期，还需 ${Math.ceil(remaining / 1000)} 秒`);
      const spec = TUNABLE_PARAMETERS[normalizedKey];
      const previous = state.active[normalizedKey]?.value ?? spec.default;
      const at = now();
      const entry = sanitizeObject({
        value: normalizedValue,
        reason: safeText(reason) || "未提供理由",
        applied_at: at,
        evidence: strings(evidence),
        health_at_apply: await this.healthSnapshot()
      });
      state.active[normalizedKey] = entry;
      state.cooldowns[normalizedKey] = at;
      state.history.push({ action: "apply", key: normalizedKey, previous, value: normalizedValue, at, reason: entry.reason, evidence: entry.evidence });
      await this.save(state);
      await this.audit("self_optimization_apply", `${normalizedKey} ${previous} -> ${normalizedValue}`);
      return { applied: true, message: `${normalizedKey} 已从 ${previous} 调整为 ${normalizedValue}`, entry };
    });
  }

  async rollback(key: string): Promise<{ rolled_back: true; message: string }> {
    const normalizedKey = String(key ?? "").trim();
    if (!(normalizedKey in TUNABLE_PARAMETERS)) throw new Error(`参数不在白名单：${normalizedKey}`);
    return withDirectoryLock(this.lockPath, async () => {
      const state = await this.load();
      const existing = state.active[normalizedKey];
      if (!existing) throw new Error(`${normalizedKey} 当前没有活动覆盖`);
      const previous = existing.value;
      delete state.active[normalizedKey];
      const at = now();
      state.cooldowns[normalizedKey] = at;
      state.history.push({ action: "rollback", key: normalizedKey, previous, value: TUNABLE_PARAMETERS[normalizedKey].default, at });
      await this.save(state);
      await this.audit("self_optimization_rollback", `${normalizedKey} -> default`);
      return { rolled_back: true, message: `${normalizedKey} 已回滚到默认值 ${TUNABLE_PARAMETERS[normalizedKey].default}` };
    });
  }

  private async healthSnapshot(): Promise<JsonObject> {
    const directories = ["validation-results", "repair-results", "ops-results", "self-upgrade-results"];
    let succeeded = 0;
    let failed = 0;
    let consecutiveFailures = 0;
    const samples: Array<{ at: string; status: string; text: string }> = [];
    for (const directory of directories) {
      try {
        for (const name of (await readdir(join(this.root, "data", directory))).filter((item) => item.endsWith(".json")).slice(-50)) {
          try {
            const raw = await readFile(join(this.root, "data", directory, name), "utf8");
            const value = JSON.parse(raw) as Record<string, unknown>;
            const status = String(value.status ?? "");
            const at = String(value.finished_at ?? value.updated_at ?? value.created_at ?? "");
            samples.push({ at, status, text: raw.slice(0, 20_000) });
          } catch { /* ignore malformed evidence */ }
        }
      } catch { /* missing directory */ }
    }
    samples.sort((a, b) => a.at.localeCompare(b.at));
    for (const item of samples) {
      if (item.status === "succeeded" || item.status === "passed" || item.status === "ok") { succeeded += 1; consecutiveFailures = 0; }
      else if (item.status === "failed" || item.status === "blocked") { failed += 1; consecutiveFailures += 1; }
    }
    const total = succeeded + failed;
    return { succeeded, failed, success_rate: total ? succeeded / total : 1, consecutive_failures: consecutiveFailures, sample_count: samples.length };
  }

  async autoTune(): Promise<JsonObject> {
    const state = await this.load();
    const health = await this.healthSnapshot();
    const evidenceText = JSON.stringify(health).toLowerCase();
    const candidates: Array<{ key: string; value: number; reason: string }> = [];
    if (Number(health.consecutive_failures ?? 0) >= 2) {
      const current = state.active["llm.temperature"]?.value as number | undefined ?? TUNABLE_PARAMETERS["llm.temperature"].default;
      if (current > 0.2) candidates.push({ key: "llm.temperature", value: Math.max(0.2, Math.round((current - 0.1) * 10) / 10), reason: "可信结果显示连续失败，保守降低采样温度" });
    }
    if (/memory|prompt|context/.test(evidenceText)) {
      const current = state.active["agent.memory_prompt_limit"]?.value as number | undefined ?? TUNABLE_PARAMETERS["agent.memory_prompt_limit"].default;
      candidates.push({ key: "agent.memory_prompt_limit", value: Math.max(10, Math.floor(current * 0.8)), reason: "上下文相关证据触发有界记忆收缩" });
    }
    for (const candidate of candidates) {
      try {
        const result = await this.apply(candidate.key, candidate.value, candidate.reason, [`health=${JSON.stringify(health)}`]);
        return { changed: true, candidate, result, health };
      } catch { /* try next bounded candidate */ }
    }
    return { changed: false, message: "当前没有满足阈值且不处于冷却期的自动调整", health, evaluated_candidates: candidates };
  }
}
