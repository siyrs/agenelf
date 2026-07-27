/**
 * ui.js — 通用 UI 工具：DOM 构造、Toast、抽屉、确认框、格式化、空状态
 */

/** HTML 转义 */
export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * 轻量 DOM 构造器：h('div.card#id', {attrs?}, ...children)
 * attrs 可省略：h('div', child1, child2) 同样合法（第二参数为
 * Node / 字符串 / 数字 / 数组 / null 时视为 child）。
 * children 支持 string（作为 textNode）、Node、数组。
 */
export function h(spec, attrs = {}, ...children) {
  // attrs 省略场景：第二参数实为子节点
  if (attrs instanceof Node || typeof attrs === 'string' || typeof attrs === 'number'
      || Array.isArray(attrs) || attrs === null || attrs === false || attrs === true) {
    children.unshift(attrs);
    attrs = {};
  }
  // 支持 'tag.cls1.cls2#id' 或 'tag#id.cls' 任意位置的 #id
  let id = '';
  const idMatch = spec.match(/#([^\s#.]+)/);
  if (idMatch) {
    id = idMatch[1];
    spec = spec.replace(idMatch[0], '');
  }
  const [tag, ...cls] = spec.split('.');
  const el = document.createElement(tag || 'div');
  if (id) el.id = id;
  if (cls.length) el.className = cls.join(' ');
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = (el.className ? el.className + ' ' : '') + v;
    else if (k === 'html') el.innerHTML = v;                 // 调用方保证已转义
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/* ---------------- Toast ---------------- */

const TOAST_ICONS = { ok: '✓', err: '✕', warn: '⚠', info: 'ℹ' };

export function toast(type, msg, ms = 3800) {
  const root = document.getElementById('toast-root');
  const el = h(`div.toast.toast--${type}`,
    h('span', { text: TOAST_ICONS[type] || TOAST_ICONS.info, style: 'font-weight:700' }),
    h('span', { text: msg, style: 'flex:1' }));
  root.append(el);
  setTimeout(() => {
    el.classList.add('is-leaving');
    setTimeout(() => el.remove(), 320);
  }, ms);
}
toast.ok = (m, ms) => toast('ok', m, ms);
toast.err = (m, ms) => toast('err', m, ms ?? 5200);
toast.warn = (m, ms) => toast('warn', m, ms);
toast.info = (m, ms) => toast('info', m, ms);

/* ---------------- 抽屉 ---------------- */

export function openDrawer(title, bodyNode) {
  const drawer = document.getElementById('drawer');
  const mask = document.getElementById('drawer-mask');
  document.getElementById('drawer-title').textContent = title;
  const body = document.getElementById('drawer-body');
  body.innerHTML = '';
  body.append(bodyNode);
  drawer.hidden = false;
  mask.hidden = false;
}

export function closeDrawer() {
  document.getElementById('drawer').hidden = true;
  document.getElementById('drawer-mask').hidden = true;
}

/* ---------------- 确认框 ---------------- */

/** 二次确认。dangerText 为空时使用默认文案。返回 Promise<boolean> */
export function confirmDialog(text, { title = '确认操作', okText = '确认执行' } = {}) {
  return new Promise(resolve => {
    const mask = document.getElementById('modal-mask');
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-text').textContent = text;
    const okBtn = document.getElementById('modal-ok');
    const cancelBtn = document.getElementById('modal-cancel');
    okBtn.textContent = okText;
    mask.hidden = false;
    const done = (val) => {
      mask.hidden = true;
      okBtn.onclick = cancelBtn.onclick = mask.onclick = null;
      resolve(val);
    };
    okBtn.onclick = () => done(true);
    cancelBtn.onclick = () => done(false);
    mask.onclick = (e) => { if (e.target === mask) done(false); };
  });
}

/* ---------------- 状态组件 ---------------- */

export function spinner(large = false) {
  return h(`span.spinner${large ? '.spinner--lg' : ''}`, { role: 'status' });
}

export function loadingBlock(text = '加载中…') {
  return h('div.loading-block', spinner(true), h('span', { text }));
}

export function emptyState(text, { icon = '○', sub = '' } = {}) {
  return h('div.empty-state',
    h('span.empty-ico', { text: icon }),
    h('p', { text }),
    sub ? h('p.small.muted', { text: sub }) : null);
}

export function errorBlock(err, onRetry) {
  const box = h('div.empty-state',
    h('span.empty-ico', { text: '⚠' }),
    h('p', { text: '加载失败' }),
    h('p.small.muted', { text: err?.message || String(err) }));
  if (onRetry) {
    box.append(h('div', { style: 'margin-top:12px' },
      h('button.btn.btn--sm', { onclick: onRetry }, '重试')));
  }
  return box;
}

/* ---------------- 格式化 ---------------- */

export function fmtTime(v) {
  if (!v) return '—';
  let d;
  if (typeof v === 'number') d = new Date(v > 1e12 ? v : v * 1000);
  else d = new Date(v);
  if (isNaN(d)) return String(v);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function shortId(id, len = 12) {
  const s = String(id ?? '');
  return s.length > len ? s.slice(0, len) + '…' : s;
}

/** 从对象中按候选名取第一个存在的字段 */
export function pick(obj, ...names) {
  if (!obj || typeof obj !== 'object') return undefined;
  for (const n of names) {
    if (obj[n] !== undefined && obj[n] !== null) return obj[n];
  }
  return undefined;
}

/** 状态字符串 → badge 样式名 */
export function statusBadge(status) {
  const s = String(status ?? '').toLowerCase();
  let kind = 'info';
  if (['ok', 'healthy', 'success', 'succeeded', 'done', 'completed', 'complete', 'active', 'approved', 'promoted', 'pass', 'passed', 'running'].includes(s)) kind = 'ok';
  else if (['pending', 'waiting', 'queued', 'in_progress', 'proposed', 'draft', 'open', 'partial', 'degraded'].includes(s)) kind = 'warn';
  else if (['failed', 'error', 'rejected', 'rolled_back', 'blocked', 'critical', 'down', 'cancelled', 'canceled'].includes(s)) kind = 'danger';
  return h(`span.badge.badge--${kind}`, { text: s || '未知' });
}

/** 把任意值渲染为可读节点（对象/数组 → 格式化 JSON） */
export function renderValue(v) {
  if (v === null || v === undefined) return h('span.muted', { text: '—' });
  if (typeof v === 'boolean') return h('span', { text: v ? '是' : '否' });
  if (typeof v === 'number' || typeof v === 'string') {
    const s = String(v);
    if (s.length > 120) return h('pre', { style: 'white-space:pre-wrap;max-height:220px;overflow:auto' }, h('code', { text: s }));
    return h('span', { text: s });
  }
  return h('pre', { style: 'white-space:pre-wrap;max-height:300px;overflow:auto' },
    h('code', { text: JSON.stringify(v, null, 2) }));
}

/** 键值定义列表 */
export function kvList(entries) {
  const dl = h('dl.kv');
  for (const [k, v] of entries) {
    if (v === undefined) continue;
    dl.append(h('dt', { text: k }), h('dd', {}, renderValue(v)));
  }
  return dl;
}

/** 带按钮 Loading 态的执行包装：禁用按钮、显示转圈、捕获错误 toast */
export async function withBtn(btn, fn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '处理中…';
  try {
    return await fn();
  } catch (err) {
    toast.err(err?.message || String(err));
    return undefined;
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}
