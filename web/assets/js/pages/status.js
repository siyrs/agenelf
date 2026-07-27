/**
 * status.js — 状态总览：卡片网格 + 能力健康条形图，每 30s 自动刷新
 * 数据源：/health、/status、/self/capability-health、/autonomy/cycles、/evolution/status
 */
import { get } from '../api.js';
import {
  h, toast, loadingBlock, errorBlock, statusBadge, pick, fmtTime, kvList, renderValue,
} from '../ui.js';

function statCard(label, value, sub = '') {
  return h('div.card.stat-card',
    h('span.stat-label', { text: label }),
    h('div.stat-value', {}, typeof value === 'string' ? value : value),
    sub ? h('span.stat-sub', { text: sub }) : null);
}

/** 能力健康 → 条形图。兼容数组或 {name: score} 字典两种返回形态。 */
function healthBars(data) {
  const box = h('div');
  let entries = [];
  if (Array.isArray(data)) {
    entries = data.map(item => {
      if (item && typeof item === 'object') {
        return [
          pick(item, 'name', 'capability', 'id', 'key') ?? '未知',
          Number(pick(item, 'health', 'score', 'value', 'ratio') ?? 0),
          pick(item, 'status'),
        ];
      }
      return [String(item), 0, undefined];
    });
  } else if (data && typeof data === 'object') {
    // 可能是 {capabilities: [...]} 或 {name: score}
    const inner = pick(data, 'capabilities', 'items', 'health');
    if (Array.isArray(inner)) return healthBars(inner);
    entries = Object.entries(data)
      .filter(([, v]) => typeof v === 'number' || (v && typeof v === 'object'))
      .map(([k, v]) => {
        if (typeof v === 'number') return [k, v, undefined];
        return [k, Number(pick(v, 'health', 'score', 'value') ?? 0), pick(v, 'status')];
      });
  }

  if (!entries.length) {
    box.append(h('p.muted', { text: '暂无能力健康数据。' }));
    return box;
  }

  for (const [name, raw, status] of entries) {
    // 归一化到 0~100
    const pct = raw <= 1 ? raw * 100 : raw;
    const clamped = Math.max(0, Math.min(100, pct));
    const cls = clamped >= 70 ? 'is-ok' : clamped >= 40 ? 'is-warn' : 'is-bad';
    box.append(h('div.bar-row',
      h('span.bar-label', { title: name, text: name }),
      h('div.bar-track', h('div.bar-fill.' + cls, { style: `width:${clamped}%` })),
      h('span.bar-val', { text: `${Math.round(clamped)}%` })));
    if (status) box.lastChild.append(statusBadge(status));
  }
  return box;
}

export async function renderStatus(root) {
  root.append(
    h('div.page-head',
      h('h1', { text: '状态总览' }),
      h('p.page-desc', { text: '系统健康、模型、技能与自治运行概况。每 30 秒自动刷新。' })),
  );

  const grid = h('div.grid.grid--3');
  const healthCard = h('div.card',
    h('div.card__title', h('h2', { text: '能力健康度' })),
    loadingBlock());
  const cyclesCard = h('div.card',
    h('div.card__title', h('h2', { text: '自治 Cycle' })),
    loadingBlock());
  const evoCard = h('div.card',
    h('div.card__title', h('h2', { text: '进化状态' })),
    loadingBlock());

  const container = h('div');
  container.append(grid, healthCard, h('div.grid.grid--2', cyclesCard, evoCard));
  const refreshNote = h('p.small.muted', { style: 'margin-top:4px' });
  container.append(refreshNote);
  root.append(container);

  let disposed = false;

  async function load() {
    if (disposed) return;

    // 顶层卡片：/health + /status
    grid.innerHTML = '';
    grid.append(statCard('存活状态', loadingInline(), '/health'));
    grid.append(statCard('模型', loadingInline(), '/status'));
    grid.append(statCard('技能数', loadingInline(), '/status'));

    const [healthRes, statusRes, capRes, cyclesRes, evoRes] = await Promise.allSettled([
      get('/health', null, { silent: true }),
      get('/status'),
      get('/self/capability-health'),
      get('/autonomy/cycles'),
      get('/evolution/status'),
    ]);
    if (disposed) return;

    grid.innerHTML = '';

    // 健康
    if (healthRes.status === 'fulfilled') {
      const d = healthRes.value || {};
      const st = pick(d, 'status', 'state', 'health') ?? 'ok';
      grid.append(statCard('存活状态', statusBadge(st), `来源 /health · ${fmtTime(pick(d, 'time', 'timestamp'))}`));
    } else {
      grid.append(statCard('存活状态', statusBadge('down'), healthRes.reason?.message || '不可达'));
    }

    // /status 详细
    if (statusRes.status === 'fulfilled') {
      const d = statusRes.value || {};
      grid.append(statCard('模型', String(pick(d, 'model', 'model_name', 'llm') ?? '—'),
        pick(d, 'provider', 'model_provider') ? `提供方：${pick(d, 'provider', 'model_provider')}` : ''));
      const skills = pick(d, 'skills', 'skill_count', 'num_skills');
      grid.append(statCard('技能数',
        String(typeof skills === 'object' && skills !== null ? (skills.total ?? skills.count ?? '—') : (skills ?? '—')),
        d.version ? `版本 ${d.version}` : ''));
      const intents = pick(d, 'intention_count', 'intentions', 'active_intentions');
      grid.replaceChildren(
        ...grid.children,
        statCard('意向计数', String(typeof intents === 'object' && intents !== null ? (intents.total ?? intents.count ?? JSON.stringify(intents)) : (intents ?? '—')), '活跃意向'));
    } else {
      const err = statusRes.reason;
      grid.append(statCard('模型', statusBadge(err?.status === 401 ? '401' : '错误'),
        err?.status === 401 ? '未认证，请在设置页配置 Token' : (err?.message || '')));
      grid.append(statCard('技能数', '—', ''));
      grid.append(statCard('意向计数', '—', ''));
    }

    // 能力健康
    healthCard.innerHTML = '';
    healthCard.append(h('div.card__title', h('h2', { text: '能力健康度' })));
    if (capRes.status === 'fulfilled') healthCard.append(healthBars(capRes.value));
    else healthCard.append(errorBlock(capRes.reason));

    // cycles
    cyclesCard.innerHTML = '';
    cyclesCard.append(h('div.card__title', h('h2', { text: '自治 Cycle' })));
    if (cyclesRes.status === 'fulfilled') {
      const d = cyclesRes.value;
      const list = Array.isArray(d) ? d : (pick(d || {}, 'cycles', 'items', 'results') ?? []);
      cyclesCard.append(kvList([
        ['累计 Cycle', Array.isArray(list) ? list.length : '—'],
        ['最近执行', list[0] ? fmtTime(pick(list[0], 'created_at', 'started_at', 'timestamp', 'time')) : '—'],
      ]));
    } else cyclesCard.append(errorBlock(cyclesRes.reason));

    // 进化状态
    evoCard.innerHTML = '';
    evoCard.append(h('div.card__title', h('h2', { text: '进化状态' })));
    if (evoRes.status === 'fulfilled') {
      const d = evoRes.value || {};
      const session = pick(d, 'session', 'current_session') || {};
      const prs = pick(d, 'promotion_requests', 'promotions', 'requests') ?? [];
      evoCard.append(kvList([
        ['当前 Session', pick(session, 'id', 'session_id', 'name') ?? (typeof session === 'string' ? session : '—')],
        ['待决晋升请求', Array.isArray(prs) ? prs.length : '—'],
      ]));
    } else evoCard.append(errorBlock(evoRes.reason));

    refreshNote.textContent = `最后刷新：${fmtTime(new Date().toISOString())} · 每 30 秒自动刷新`;
  }

  function loadingInline() {
    const s = h('span.spinner');
    return s;
  }

  await load();
  const timer = setInterval(load, 30000);
  return () => { disposed = true; clearInterval(timer); };
}
