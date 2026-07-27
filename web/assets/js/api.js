/**
 * api.js — 统一 API 封装
 * - 自动携带 X-Agenelf-Token
 * - 401 → 提示并跳转设置页；503 → token 未配置提示
 * - 超时控制（AbortController）
 * - 相对路径请求（假定控制台由后端同源托管于 /ui/）
 */

const TOKEN_KEY = 'agenelf_token';
const DEFAULT_TIMEOUT = 30000;

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(token) {
  const t = (token || '').trim();
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}

/** 401 全局处理回调（由 app.js 注册：toast + 跳转设置页） */
let unauthorizedHandler = null;
export function onUnauthorized(fn) { unauthorizedHandler = fn; }

export class ApiError extends Error {
  constructor(message, { status = 0, data = null, timeout = false } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
    this.timeout = timeout;
  }
}

/** 从错误响应体中提取可读信息（截断过长/HTML 响应体） */
function extractDetail(data, fallback) {
  if (!data) return fallback;
  if (typeof data === 'string') {
    const s = data.trim();
    // 非 JSON 的错误页（如代理返回的 HTML）不直接展示
    if (s.startsWith('<') || s.length > 300) return fallback;
    return s;
  }
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail) && data.detail.length) {
    return data.detail.map(d => d.msg || JSON.stringify(d)).join('；');
  }
  if (typeof data.message === 'string') return data.message;
  return fallback;
}

/**
 * 发起 API 请求。
 * @param {string} path   如 '/chat'
 * @param {object} opts   { method, body, query, timeout, silent }
 * @returns {Promise<any>} 解析后的 JSON（无 body 时为 null）
 */
export async function api(path, opts = {}) {
  const { method = 'GET', body, query, timeout = DEFAULT_TIMEOUT, silent = false } = opts;

  let url = path;
  if (query && typeof query === 'object') {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
    }
    const s = qs.toString();
    if (s) url += (url.includes('?') ? '&' : '?') + s;
  }

  const headers = { 'Accept': 'application/json' };
  const token = getToken();
  if (token) headers['X-Agenelf-Token'] = token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);

  let resp;
  try {
    resp = await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err.name === 'AbortError') {
      throw new ApiError(`请求超时（${Math.round(timeout / 1000)}s）：${method} ${path}`, { timeout: true });
    }
    throw new ApiError(`网络错误，无法连接后端：${err.message}`, { status: 0 });
  }
  clearTimeout(timer);

  let data = null;
  const text = await resp.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }

  if (!resp.ok) {
    const detail = extractDetail(data, `HTTP ${resp.status}`);
    if (resp.status === 401 && !silent && unauthorizedHandler) {
      unauthorizedHandler(detail);
    }
    throw new ApiError(detail, { status: resp.status, data });
  }
  return data;
}

/** GET 便捷方法 */
export const get = (path, query, opts = {}) => api(path, { ...opts, method: 'GET', query });
/** POST 便捷方法 */
export const post = (path, body, opts = {}) => api(path, { ...opts, method: 'POST', body });
/** DELETE 便捷方法（参数走 query） */
export const del = (path, query, opts = {}) => api(path, { ...opts, method: 'DELETE', query });

/* ---------------- SSE 流式对话（POST /chat/stream） ---------------- */

/** 解析一个 SSE 事件块 → { event, data }（data 为 JSON 解析结果，失败为 null） */
function parseSseBlock(block) {
  let event = '';
  const dataLines = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
  }
  let data = null;
  if (dataLines.length) {
    try { data = JSON.parse(dataLines.join('\n')); } catch { data = null; }
  }
  return { event, data };
}

/**
 * 流式对话：fetch + ReadableStream 手写解析 SSE（EventSource 不支持 POST/自定义头）。
 * 事件序列：status(thinking) → message(delta)×N → done；异常时为 error 事件。
 * @param {string} message
 * @param {object} opts { channel, sessionId, timeout, onStatus(phase), onDelta(text) }
 *   sessionId 为空时省略 session_id 字段（落入后端默认会话桶）。
 * @returns {Promise<object>} done 事件 payload（{ok:true}）
 * @throws {ApiError} HTTP 层错误（401/400/网络/超时）或流内 error 事件
 */
export async function streamChat(message, { channel = 'web', sessionId = '', timeout = 120000, onStatus, onDelta } = {}) {
  const headers = { 'Accept': 'text/event-stream', 'Content-Type': 'application/json' };
  const token = getToken();
  if (token) headers['X-Agenelf-Token'] = token;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);

  let resp;
  try {
    resp = await fetch('/chat/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify(sessionId ? { message, channel, session_id: sessionId } : { message, channel }),
      signal: ctrl.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err.name === 'AbortError') {
      throw new ApiError(`请求超时（${Math.round(timeout / 1000)}s）：POST /chat/stream`, { timeout: true });
    }
    throw new ApiError(`网络错误，无法连接后端：${err.message}`, { status: 0 });
  }

  if (!resp.ok || !resp.body) {
    clearTimeout(timer);
    const text = await resp.text().catch(() => '');
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch { data = text; } }
    const detail = extractDetail(data, `HTTP ${resp.status}`);
    if (resp.status === 401 && unauthorizedHandler) unauthorizedHandler(detail);
    throw new ApiError(detail, { status: resp.status || 0, data });
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let donePayload = null;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const { event, data } = parseSseBlock(block);
        if (event === 'status') {
          onStatus?.(data?.phase || '');
        } else if (event === 'message') {
          if (typeof data?.delta === 'string' && data.delta) onDelta?.(data.delta);
        } else if (event === 'done') {
          donePayload = data || { ok: true };
        } else if (event === 'error') {
          throw new ApiError(data?.error || '流式对话失败', { status: resp.status, data });
        }
      }
    }
  } finally {
    clearTimeout(timer);
    try { reader.cancel(); } catch { /* 忽略 */ }
  }
  return donePayload || { ok: true };
}

/* ---------------- 连接状态探测 ---------------- */

export const ConnState = {
  UNKNOWN: 'unknown',   // 检测中
  OK: 'ok',             // /health 与 /status 均通
  NO_AUTH: 'no_auth',   // /health 通但 /status 401（缺 token 或 token 错）
  BACKEND_NO_TOKEN: 'backend_no_token', // 后端未配置 token（503）
  DOWN: 'down',         // /health 不通
};

const connListeners = new Set();
let lastConn = { state: ConnState.UNKNOWN, detail: '' };

export function onConnChange(fn) {
  connListeners.add(fn);
  fn(lastConn);
  return () => connListeners.delete(fn);
}

function setConn(state, detail = '') {
  lastConn = { state, detail };
  connListeners.forEach(fn => fn(lastConn));
}

/** 探测后端连接：先 /health（无鉴权），再 /status（鉴权） */
export async function probeConnection() {
  try {
    await get('/health', null, { timeout: 8000, silent: true });
  } catch (err) {
    setConn(ConnState.DOWN, err.timeout ? '健康检查超时' : '后端不可达');
    return lastConn;
  }
  try {
    await get('/status', null, { timeout: 8000, silent: true });
    setConn(ConnState.OK, '已连接');
  } catch (err) {
    if (err.status === 401) setConn(ConnState.NO_AUTH, '需要有效 Token');
    else if (err.status === 503) setConn(ConnState.BACKEND_NO_TOKEN, '后端未配置 Token');
    else setConn(ConnState.DOWN, err.message);
  }
  return lastConn;
}
