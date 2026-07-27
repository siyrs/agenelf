/**
 * app.js — 入口：路由、导航、主题、全局连接状态
 */
import { onConnChange, probeConnection, ConnState, onUnauthorized } from './js/api.js';
import { closeDrawer, toast } from './js/ui.js';

import { renderChat } from './js/pages/chat.js';
import { renderStatus } from './js/pages/status.js';
import { renderGrowth } from './js/pages/growth.js';
import { renderMemory } from './js/pages/memory.js';
import { renderTasks } from './js/pages/tasks.js';
import { renderAutonomy } from './js/pages/autonomy.js';
import { renderApprovals } from './js/pages/approvals.js';
import { renderValidation } from './js/pages/validation.js';
import { renderSettings } from './js/pages/settings.js';

/* ---------------- 主题 ---------------- */

const THEME_KEY = 'agenelf_theme';

export function getTheme() {
  return localStorage.getItem(THEME_KEY) || 'light';
}

export function setTheme(theme) {
  const t = theme === 'dark' ? 'dark' : 'light';
  localStorage.setItem(THEME_KEY, t);
  document.documentElement.dataset.theme = t;
}

setTheme(getTheme());

/* ---------------- 路由 ---------------- */

const routes = {
  chat:       { title: '对话',       render: renderChat },
  status:     { title: '状态总览',   render: renderStatus },
  growth:     { title: '自我成长',   render: renderGrowth },
  memory:     { title: '记忆',       render: renderMemory },
  tasks:      { title: '任务',       render: renderTasks },
  autonomy:   { title: '自治与进化', render: renderAutonomy },
  approvals:  { title: '审批中心',   render: renderApprovals },
  validation: { title: '验证与修复', render: renderValidation },
  settings:   { title: '设置',       render: renderSettings },
};

let currentCleanup = null;

function currentRoute() {
  const hash = location.hash.replace(/^#\/?/, '').split('?')[0];
  return routes[hash] ? hash : 'chat';
}

async function navigate() {
  const name = currentRoute();
  const route = routes[name];

  // 清理上一页（定时器等）
  if (typeof currentCleanup === 'function') {
    try { currentCleanup(); } catch { /* 忽略 */ }
    currentCleanup = null;
  }
  closeDrawer();

  // 导航高亮
  document.querySelectorAll('#nav-list a').forEach(a => {
    a.classList.toggle('is-active', a.dataset.route === name);
  });
  document.title = `${route.title} · AgenElf 控制台`;

  const content = document.getElementById('content');
  content.innerHTML = '';
  closeSidenav();

  const cleanup = await route.render(content);
  if (typeof cleanup === 'function') currentCleanup = cleanup;
  content.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

window.addEventListener('hashchange', navigate);

/** 供各页面跳转到设置页（如 401） */
export function goSettings() {
  location.hash = '#/settings';
}

/* 全局 401 处理：toast + 跳转设置页（5s 内合并多次触发） */
let last401 = 0;
onUnauthorized((detail) => {
  const now = Date.now();
  if (now - last401 < 5000) { goSettings(); return; }
  last401 = now;
  toast.err(`认证失败（401）：${detail || 'Token 无效'}，请在设置页更新`);
  goSettings();
  probeConnection(); // 立即刷新连接指示
});

/* ---------------- 侧边导航（窄屏折叠） ---------------- */

const sidenav = document.getElementById('sidenav');
const sidenavMask = document.getElementById('sidenav-mask');
const menuBtn = document.getElementById('menu-toggle');

function closeSidenav() {
  sidenav.classList.remove('is-open');
  sidenavMask.classList.remove('is-open');
  menuBtn.setAttribute('aria-expanded', 'false');
}

menuBtn.addEventListener('click', () => {
  const open = sidenav.classList.toggle('is-open');
  sidenavMask.classList.toggle('is-open', open);
  menuBtn.setAttribute('aria-expanded', String(open));
});
sidenavMask.addEventListener('click', closeSidenav);
document.getElementById('drawer-close').addEventListener('click', closeDrawer);
document.getElementById('drawer-mask').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeDrawer(); closeSidenav(); }
});

/* ---------------- 全局连接状态 ---------------- */

const CONN_UI = {
  [ConnState.UNKNOWN]:           { cls: '',        text: '检测中…' },
  [ConnState.OK]:                { cls: 'is-ok',   text: '已连接' },
  [ConnState.NO_AUTH]:           { cls: 'is-warn', text: '需 Token' },
  [ConnState.BACKEND_NO_TOKEN]:  { cls: 'is-warn', text: '后端未配 Token' },
  [ConnState.DOWN]:              { cls: 'is-bad',  text: '连接失败' },
};

onConnChange(({ state, detail }) => {
  const ui = CONN_UI[state] || CONN_UI[ConnState.UNKNOWN];
  for (const dot of [document.getElementById('conn-dot'), document.getElementById('conn-dot-top')]) {
    dot.className = 'conn-dot' + (ui.cls ? ' ' + ui.cls : '');
    dot.title = detail || ui.text;
  }
  document.getElementById('conn-text').textContent = ui.text;
});

// 立即探测一次，之后每 30s 探测
probeConnection();
setInterval(probeConnection, 30000);

// 供设置页保存 token 后手动触发
export { probeConnection };

/* ---------------- 启动 ---------------- */

if (!location.hash) location.hash = '#/chat';
navigate();
