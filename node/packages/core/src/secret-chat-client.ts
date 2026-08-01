import type { JsonObject, JsonValue } from "./types.ts";

const MAX_RESPONSE = 1024 * 1024;

function enabledByEnvironment(): boolean {
  const value = String(process.env.AGENELF_CHAT_PLAINTEXT_SECRETS ?? "true").trim().toLowerCase();
  return !["0", "false", "off", "no"].includes(value);
}

export class SecretChatClient {
  readonly baseUrl: string;
  readonly token: string;
  readonly enabled: boolean;

  constructor(options: { baseUrl?: string; token?: string; enabled?: boolean } = {}) {
    this.baseUrl = String(options.baseUrl ?? process.env.AGENELF_SECRET_CHAT_URL ?? "http://secret-chat-broker:8097").replace(/\/+$/, "");
    this.token = String(options.token ?? process.env.AGENELF_API_TOKEN ?? "");
    this.enabled = options.enabled ?? enabledByEnvironment();
  }

  private async request(path: string, init: RequestInit = {}): Promise<JsonObject> {
    if (!this.enabled) throw new Error("主人聊天明文密钥模式未启用");
    if (!this.token) throw new Error("AGENELF_API_TOKEN 未配置，无法调用 Secret Chat Broker");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20 * 60_000);
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        redirect: "error",
        signal: controller.signal,
        headers: {
          accept: "application/json",
          "content-type": "application/json",
          "x-agenelf-token": this.token,
          ...(init.headers ?? {})
        }
      });
      const text = await response.text();
      if (Buffer.byteLength(text, "utf8") > MAX_RESPONSE) throw new Error("Secret Chat Broker 响应超过 1 MiB 上限");
      let value: JsonValue;
      try { value = JSON.parse(text) as JsonValue; }
      catch { throw new Error(`Secret Chat Broker 返回非 JSON 响应：HTTP ${response.status}`); }
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Secret Chat Broker 返回值必须是 object");
      const document = value as JsonObject;
      if (!response.ok) throw new Error(String(document.error ?? `Secret Chat Broker HTTP ${response.status}`));
      return document;
    } finally {
      clearTimeout(timer);
    }
  }

  async targets(): Promise<JsonObject> {
    return this.request("/v1/targets", { method: "GET" });
  }

  async snapshot(envTarget: string, seatId = ""): Promise<JsonObject> {
    return this.request("/v1/snapshot", {
      method: "POST",
      body: JSON.stringify({ env_target: envTarget, ...(seatId ? { seat_id: seatId } : {}) })
    });
  }

  async apply(envTarget: string, changes: JsonValue[], confirmTarget: string): Promise<JsonObject> {
    return this.request("/v1/apply", {
      method: "POST",
      body: JSON.stringify({ env_target: envTarget, changes, confirm_target: confirmTarget })
    });
  }
}
