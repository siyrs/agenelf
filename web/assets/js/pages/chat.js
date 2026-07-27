/**
 * chat.js — 对话页：多会话管理 + 消息流 + SSE 流式渲染（失败自动降级同步 /chat）+ markdown 定稿
 * 会话：localStorage 存会话列表（id + 显示名 + 创建时间），默认会话不发 session_id（走后端默认桶）。
 * 切换会话：GET /chat/history?session_id= 拉取后端历史渲染；之后消息走带 session_id 的 /chat/stream。
 * 清空会话：二次确认后 DELETE /chat/history?session_id= 并清空本地气泡。
 * 流式：POST /chat/stream（SSE：status → message(delta)×N → done/error）
 * 降级：POST /chat {message, channel:"web"} → {reply}
 */
import { get, post, del, streamChat } from '../api.js';
import {
  h, toast, emptyState, confirmDialog, openDrawer, closeDrawer, withBtn, loadingBlock,
} from '../ui.js';
import { mdElement } from '../markdown.js';
import { goSettings } from '../../app.js';

const CHAT_TIMEOUT = 120000; // 对话可能较慢，给 120s
const HISTORY_LIMIT = 50;

/* ---------------- 会话存储（localStorage） ---------------- */

const SESSIONS_KEY = 'agenelf_chat_sessions';
const DEFAULT_ID = 'default'; // 默认会话：不发送 session_id，走后端默认桶

function defaultStore() {
  return { current: DEFAULT_ID, sessions: [{ id: DEFAULT_ID, name: '默认会话', createdAt: 0 }] };
}

function loadStore() {
  try {
    const raw = JSON.parse(localStorage.getItem(SESSIONS_KEY) || 'null');
    if (!raw || !Array.isArray(raw.sessions)) return defaultStore();
    // 过滤非法项并确保默认会话存在且排在最前
    const sessions = raw.sessions.filter(s => s && typeof s.id === 'string' && s.id && s.id !== DEFAULT_ID);
    sessions.unshift({ id: DEFAULT_ID, name: '默认会话', createdAt: 0 });
    for (const s of sessions) {
      if (typeof s.name !== 'string' || !s.name) s.name = s.id;
      if (typeof s.createdAt !== 'number') s.createdAt = Date.now();
    }
    const current = sessions.some(s => s.id === raw.current) ? raw.current : DEFAULT_ID;
    return { current, sessions };
  } catch {
    return defaultStore();
  }
}

function saveStore(store) {
  try { localStorage.setItem(SESSIONS_KEY, JSON.stringify(store)); } catch { /* 存储满等场景忽略 */ }
}

/** 会话对应的后端 session_id；默认会话返回 ''（请求中省略该字段） */
function backendSessionId(session) {
  return session && session.id !== DEFAULT_ID ? session.id : '';
}

/* ---------------- 消息渲染 ---------------- */

function msgNode(m) {
  const isUser = m.role === 'user';
  const wrap = h(`div.msg.msg--${isUser ? 'user' : 'agent'}${m.role === 'error' ? '.is-error' : ''}`);
  wrap.append(h('div.msg__avatar', { text: isUser ? '我' : 'A' }));
  const bubble = h('div.msg__bubble');
  if (isUser || m.role === 'error') {
    bubble.textContent = m.text;
    bubble.style.whiteSpace = 'pre-wrap';
  } else {
    bubble.append(mdElement(m.text));
  }
  const box = h('div', { style: 'min-width:0' }, bubble,
    h('div.msg__meta', { text: m.meta || '' }));
  wrap.append(box);
  return wrap;
}

function typingNode() {
  return h('div.msg.msg--agent#typing-row',
    h('div.msg__avatar', { text: 'A' }),
    h('div.msg__bubble',
      h('span.muted.small', { text: '思考中 ' }),
      h('span.typing', h('i'), h('i'), h('i'))));
}

/* ---------------- 页面 ---------------- */

export async function renderChat(root) {
  const store = loadStore();
  const messages = []; // 当前会话气泡 {role:'user'|'agent'|'error', text, meta}
  let loadToken = 0;   // 防止历史加载与快速切换产生竞态

  const log = h('div.chat-log');
  const input = h('textarea.textarea', {
    placeholder: '输入消息，Enter 发送，Shift+Enter 换行…',
    rows: 2,
  });
  const sendBtn = h('button.btn.btn--primary', { type: 'button' }, '发送');

  /* ---------- 会话管理条 ---------- */

  const sessionSelect = h('select.select', { title: '切换会话' });
  const sessionIdHint = h('span.small.muted#chat-session-id');
  const newBtn = h('button.btn.btn--sm', { type: 'button' }, '＋ 新建会话');
  const renameBtn = h('button.btn.btn--sm.btn--ghost', { type: 'button' }, '重命名');
  const clearBtn = h('button.btn.btn--sm.btn--danger', { type: 'button' }, '清空当前会话');

  const sessionBar = h('div.chat-session-bar',
    h('span.chat-session-label', { text: '当前会话' }),
    sessionSelect,
    sessionIdHint,
    h('div.chat-session-actions', newBtn, renameBtn, clearBtn));

  function currentSession() {
    return store.sessions.find(s => s.id === store.current) || store.sessions[0];
  }

  function refreshSessionBar() {
    sessionSelect.innerHTML = '';
    for (const s of store.sessions) {
      sessionSelect.append(h('option', { value: s.id, text: s.name, selected: s.id === store.current ? '' : null }));
    }
    sessionSelect.value = store.current;
    const cur = currentSession();
    sessionIdHint.textContent = cur.id === DEFAULT_ID ? '后端默认桶' : `id: ${cur.id}`;
    renameBtn.disabled = cur.id === DEFAULT_ID; // 默认会话名称固定
  }

  function setBarDisabled(disabled) {
    sessionSelect.disabled = disabled;
    newBtn.disabled = disabled;
    clearBtn.disabled = disabled;
    renameBtn.disabled = disabled || currentSession().id === DEFAULT_ID;
  }

  /* ---------- 消息绘制 ---------- */

  function scrollBottom() {
    log.scrollTop = log.scrollHeight;
  }

  function paint() {
    log.innerHTML = '';
    if (!messages.length) {
      log.append(emptyState(`「${currentSession().name}」还没有消息`, {
        icon: '💬',
        sub: '回复将以流式方式逐句呈现；不支持流式的后端会自动降级为同步请求。',
      }));
      return;
    }
    for (const m of messages) log.append(msgNode(m));
    scrollBottom();
  }

  /** 从后端拉取当前会话历史并渲染（无记录时为空状态） */
  async function loadHistory() {
    const token = ++loadToken;
    const sid = backendSessionId(currentSession());
    messages.length = 0;
    log.innerHTML = '';
    log.append(loadingBlock('加载会话历史…'));
    try {
      const data = await get('/chat/history', { session_id: sid || undefined, limit: HISTORY_LIMIT });
      if (token !== loadToken) return; // 期间又切换了会话
      messages.length = 0;
      for (const e of (data && data.history) || []) {
        const text = typeof e?.content === 'string' ? e.content : String(e?.content ?? '');
        if (!text) continue;
        if (e.role === 'user') messages.push({ role: 'user', text });
        else if (e.role === 'assistant') messages.push({ role: 'agent', text });
        // system/tool 等角色不在对话页展示
      }
      paint();
    } catch (err) {
      if (token !== loadToken) return;
      messages.length = 0;
      paint();
      if (err.status !== 401) toast.err(`加载会话历史失败：${err.message}`);
    }
  }

  /* ---------- 会话操作 ---------- */

  /** 抽屉式命名表单（新建 / 重命名共用） */
  function openNameForm(title, { initial = '', okText = '保存', onSubmit }) {
    const nameInput = h('input.input', { value: initial, maxlength: 40, placeholder: '会话名称' });
    const submit = h('button.btn.btn--primary', { type: 'button' }, okText);
    submit.addEventListener('click', () => withBtn(submit, async () => {
      const name = nameInput.value.trim();
      if (!name) { toast.warn('请填写会话名称'); return; }
      await onSubmit(name);
      closeDrawer();
    }));
    nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); submit.click(); }
    });
    openDrawer(title, h('div',
      h('div.field', h('label', { text: '会话名称 *' }), nameInput),
      h('div.btn-row', submit)));
    nameInput.focus();
    nameInput.select();
  }

  async function switchSession(id) {
    if (!store.sessions.some(s => s.id === id) || id === store.current) {
      refreshSessionBar();
      return;
    }
    store.current = id;
    saveStore(store);
    refreshSessionBar();
    await loadHistory();
    input.focus();
  }

  sessionSelect.addEventListener('change', () => switchSession(sessionSelect.value));

  newBtn.addEventListener('click', () => {
    openNameForm('新建会话', {
      initial: `会话 ${store.sessions.length}`,
      okText: '创建',
      onSubmit: async (name) => {
        const id = `web-${Date.now().toString(36)}`;
        store.sessions.push({ id, name, createdAt: Date.now() });
        store.current = id;
        saveStore(store);
        refreshSessionBar();
        await loadHistory();
        toast.ok(`已创建会话「${name}」`);
        input.focus();
      },
    });
  });

  renameBtn.addEventListener('click', () => {
    const cur = currentSession();
    if (cur.id === DEFAULT_ID) { toast.warn('默认会话名称固定，不可重命名'); return; }
    openNameForm('重命名会话', {
      initial: cur.name,
      okText: '保存',
      onSubmit: async (name) => {
        cur.name = name;
        saveStore(store);
        refreshSessionBar();
        toast.ok(`会话已重命名为「${name}」`);
      },
    });
  });

  clearBtn.addEventListener('click', async () => {
    const cur = currentSession();
    const ok = await confirmDialog(
      `确定清空会话「${cur.name}」吗？将删除后端保存的该会话全部历史，且不可恢复。`,
      { title: '清空当前会话', okText: '清空' });
    if (!ok) return;
    try {
      const sid = backendSessionId(cur);
      await del('/chat/history', { session_id: sid || undefined });
      loadToken++; // 使进行中的历史加载失效
      messages.length = 0;
      paint();
      toast.ok(`会话「${cur.name}」已清空`);
    } catch (err) {
      if (err.status !== 401) toast.err(`清空会话失败：${err.message}`);
    }
  });

  /* ---------- 发送（流式优先，失败降级） ---------- */

  /**
   * 优先走 SSE 流式：逐 delta 纯文本追加到实时气泡，done 后由 paint() 用 markdown 定稿。
   * 流式失败且尚未收到任何增量时，自动降级同步 /chat。
   */
  async function sendStreaming(text) {
    const sid = backendSessionId(currentSession());
    let streamed = '';
    let liveBubble = null;
    try {
      await streamChat(text, {
        channel: 'web',
        sessionId: sid,
        timeout: CHAT_TIMEOUT,
        onDelta: (delta) => {
          if (!liveBubble) {
            // 首个增量到达：移除「思考中」，换成实时气泡（过程中纯文本追加）
            log.querySelector('#typing-row')?.remove();
            liveBubble = h('div.msg__bubble', { style: 'white-space:pre-wrap' });
            log.append(h('div.msg.msg--agent#stream-row',
              h('div.msg__avatar', { text: 'A' }),
              h('div', { style: 'min-width:0' }, liveBubble,
                h('div.msg__meta', { text: '流式输出中…' }))));
          }
          streamed += delta;
          liveBubble.textContent = streamed;
          scrollBottom();
        },
      });
      return streamed || '(空回复)';
    } catch (err) {
      if (streamed) {
        // 已收到部分内容：保留部分结果并提示中断，不再降级重发
        toast.warn(`流式输出中断：${err.message}`);
        return streamed;
      }
      if (err.status === 401 || err.status === 503) throw err; // 鉴权问题降级无意义
      // 旧后端无 /chat/stream（404）或网络/超时等：降级同步 /chat
      const body = sid ? { message: text, channel: 'web', session_id: sid } : { message: text, channel: 'web' };
      const data = await post('/chat', body, { timeout: CHAT_TIMEOUT });
      return String((data && (data.reply ?? data.message)) ?? '(空回复)');
    }
  }

  async function send() {
    const text = input.value.trim();
    if (!text || sendBtn.disabled) return;
    messages.push({ role: 'user', text });
    input.value = '';
    autoGrow();
    paint();

    // 思考中占位；发送期间禁止切换/清空会话，避免气泡串会话
    sendBtn.disabled = true;
    setBarDisabled(true);
    log.append(typingNode());
    scrollBottom();

    try {
      const reply = await sendStreaming(text);
      messages.push({ role: 'agent', text: reply });
    } catch (err) {
      if (err.status === 401) {
        toast.err('认证失败（401），请先在设置页配置 Token');
        goSettings();
      } else if (err.status === 503) {
        toast.err('后端未配置 Token（503），请在设置页查看说明');
      }
      messages.push({
        role: 'error',
        text: `发送失败：${err.message}`,
        meta: err.timeout ? '请求超时，可稍后重试' : '',
      });
    } finally {
      sendBtn.disabled = false;
      setBarDisabled(false);
      paint();
      input.focus();
    }
  }

  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  }

  sendBtn.addEventListener('click', send);
  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });

  root.append(
    h('div.page-head',
      h('h1', { text: '对话' }),
      h('p.page-desc', { text: '与 AgenElf 直接交谈。支持多会话隔离：新建会话拥有独立上下文，可随时切换或清空。' })),
    h('div.chat-wrap', sessionBar, log, h('div.chat-input-bar', input, sendBtn)),
  );
  refreshSessionBar();
  await loadHistory();
  input.focus();
}
