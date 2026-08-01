import { EventEmitter } from "node:events";
import { randomId } from "./canonical.ts";
import { sanitizeObject } from "./privacy.ts";
import { SessionLedgerStore } from "./session-ledger.ts";
import type { JsonObject } from "./types.ts";

export const AGENT_EVENT_TYPES = new Set([
  "run.started", "turn.started", "reasoning.started", "reasoning.delta", "reasoning.completed",
  "message.delta", "message.completed", "tool.preflight", "tool.started", "tool.delta", "tool.completed",
  "approval.required", "approval.resolved", "runner.started", "runner.completed", "run.checkpointed",
  "run.compacted", "run.settled", "run.failed", "run.cancelled"
]);
const TERMINAL_TYPES = new Set(["run.settled", "run.failed", "run.cancelled"]);
const EVENT_ORIGINS = new Set(["runtime", "agent_skill", "owner", "runner", "migration"]);
const TRANSIENT_TYPES = new Set(["reasoning.delta", "message.delta", "tool.delta"]);
const DIRECT_SECRET_ROUTES = new Set(["reveal", "apply", "diagnostic"]);

export interface AgentEvent {
  schema_version: 1;
  id: string;
  session_id: string;
  run_id: string;
  seq: number;
  type: string;
  origin: string;
  ts: string;
  transient: boolean;
  payload: JsonObject;
}

export interface AgentEventOptions {
  origin?: string;
  transient?: boolean;
  allowSensitivePayload?: boolean;
}

export class EventCursorExpired extends Error {}
export class RunAlreadyTerminal extends Error {}

function canExposeDirectSecretPayload(type: string, origin: string, payload: JsonObject): boolean {
  if (origin !== "runtime") return false;
  if (type !== "message.delta" && type !== "message.completed") return false;
  if (payload.sensitive !== true) return false;
  return DIRECT_SECRET_ROUTES.has(String(payload.direct_route ?? ""));
}

export class RunEventStream {
  readonly sessionId: string;
  readonly runId: string;
  private readonly ledger: SessionLedgerStore;
  private readonly emitter = new EventEmitter();
  private readonly buffer: AgentEvent[] = [];
  private readonly maxBuffer: number;
  private terminalType: string | null = null;
  private sequence = 0;
  private writeChain: Promise<void> = Promise.resolve();

  constructor(root: string, sessionId: string, runId = randomId("run-", 16), maxBuffer = 2000) {
    this.sessionId = sessionId;
    this.runId = runId;
    this.ledger = new SessionLedgerStore(root);
    this.maxBuffer = Math.max(1, Math.min(maxBuffer, 20_000));
    this.emitter.setMaxListeners(1000);
  }

  async emit(type: string, payload: JsonObject = {}, options: AgentEventOptions = {}) {
    if (!AGENT_EVENT_TYPES.has(type)) throw new Error(`未知 Agent event：${type}`);
    const origin = options.origin ?? "runtime";
    if (!EVENT_ORIGINS.has(origin)) throw new Error(`未知 Agent event origin：${origin}`);
    const sensitiveAllowed = canExposeDirectSecretPayload(type, origin, payload);
    if (options.allowSensitivePayload === true && !sensitiveAllowed) {
      throw new Error("敏感事件原文只允许确定性主人 Secret Chat 消息");
    }
    const exposeSensitive = options.allowSensitivePayload === true && sensitiveAllowed;
    const task = async () => {
      if (this.terminalType) throw new RunAlreadyTerminal(`run 已以 ${this.terminalType} 结束`);
      const event: AgentEvent = {
        schema_version: 1,
        id: randomId("aevt-", 20),
        session_id: this.sessionId,
        run_id: this.runId,
        seq: this.sequence + 1,
        type,
        origin,
        ts: new Date().toISOString(),
        transient: exposeSensitive ? true : options.transient ?? TRANSIENT_TYPES.has(type),
        payload: exposeSensitive ? payload : sanitizeObject(payload)
      };
      if (!event.transient) {
        await this.ledger.append({
          sessionId: this.sessionId,
          type: "custom",
          origin: event.origin,
          payload: { agent_event: event as unknown as JsonObject }
        });
      }
      this.sequence = event.seq;
      this.buffer.push(event);
      if (this.buffer.length > this.maxBuffer) this.buffer.shift();
      if (TERMINAL_TYPES.has(type)) this.terminalType = type;
      this.emitter.emit("event", event);
      return event;
    };
    let result: AgentEvent | undefined;
    const prior = this.writeChain;
    this.writeChain = prior.catch(() => undefined).then(async () => { result = await task(); });
    await this.writeChain;
    return result!;
  }

  eventsAfter(afterSeq = 0, limit = 200): AgentEvent[] {
    const oldest = this.buffer[0]?.seq;
    if (oldest && afterSeq < oldest - 1) throw new EventCursorExpired(`游标 ${afterSeq} 已过期`);
    return this.buffer.filter((event) => event.seq > afterSeq).slice(0, Math.max(1, Math.min(limit, 1000)));
  }

  async waitAfter(afterSeq = 0, timeoutMs = 15_000): Promise<AgentEvent[]> {
    const available = this.eventsAfter(afterSeq);
    if (available.length || this.terminalType) return available;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { cleanup(); resolve([]); }, Math.max(0, timeoutMs));
      const listener = () => {
        try {
          const values = this.eventsAfter(afterSeq);
          if (values.length || this.terminalType) { cleanup(); resolve(values); }
        } catch (error) { cleanup(); reject(error); }
      };
      const cleanup = () => { clearTimeout(timer); this.emitter.off("event", listener); };
      this.emitter.on("event", listener);
    });
  }

  snapshot() {
    return {
      session_id: this.sessionId,
      run_id: this.runId,
      last_seq: this.sequence,
      oldest_buffered_seq: this.buffer[0]?.seq ?? null,
      buffered_events: this.buffer.length,
      terminal_type: this.terminalType
    };
  }

  get isTerminal() { return Boolean(this.terminalType); }
}

export class AgentEventHub {
  private readonly runs = new Map<string, RunEventStream>();
  private readonly root: string;
  private readonly maxRuns: number;
  constructor(root: string, maxRuns = 128) { this.root = root; this.maxRuns = maxRuns; }

  create(sessionId: string): RunEventStream {
    if (this.runs.size >= this.maxRuns) {
      const terminal = [...this.runs.entries()].find(([, stream]) => stream.isTerminal);
      if (!terminal) throw new Error("活动 run 已达到上限");
      this.runs.delete(terminal[0]);
    }
    const stream = new RunEventStream(this.root, sessionId);
    this.runs.set(stream.runId, stream);
    return stream;
  }

  get(runId: string): RunEventStream {
    const stream = this.runs.get(runId);
    if (!stream) throw new Error(`run 不存在：${runId}`);
    return stream;
  }

  list() { return [...this.runs.values()].map((stream) => stream.snapshot()); }
}
