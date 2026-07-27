/**
 * autonomy.js — 自治与进化：cycle 列表/详情/发起 + 进化状态面板
 */
import { get, post } from '../api.js';
import {
  h, toast, loadingBlock, errorBlock, emptyState, statusBadge,
  fmtTime, shortId, pick, kvList, renderValue, openDrawer, confirmDialog, withBtn,
} from '../ui.js';

export async function renderAutonomy(root) {
  root.append(h('div.page-head',
    h('h1', { text: '自治与进化' }),
    h('p.page-desc', { text: '自治循环（cycle）的执行历史与详情，以及进化会话与晋升请求概况。' })));

  const listCard = h('div.card',
    h('div.card__title', h('h2', { text: 'Cycle 历史' }),
      h('button.btn.btn--sm#cycle-refresh', {}, '刷新')),
    loadingBlock());
  const evoCard = h('div.card',
    h('div.card__title', h('h2', { text: '进化状态' })),
    loadingBlock());

  /* ---- 发起新 cycle ---- */
  const goal = h('textarea.textarea', { rows: 3, placeholder: '本轮自治目标，例如：「提升验证覆盖率并修复失败的检查」' });
  const applyChk = h('input', { type: 'checkbox' });
  const applyWarn = h('div.banner.banner--danger', { hidden: true },
    h('span.banner__ico', { text: '⛔' }),
    h('div', {},
      h('p', {}, h('strong', { text: 'apply_changes 已开启：' }), '本 cycle 产出的变更将被直接应用，而非仅作提案。'),
      h('p.small.muted', { text: '请确认目标描述清晰、范围可控。提交时仍需二次确认。' })));
  const runBtn = h('button.btn.btn--primary', {}, '发起 Cycle');

  applyChk.addEventListener('change', () => { applyWarn.hidden = !applyChk.checked; });

  runBtn.addEventListener('click', () => withBtn(runBtn, async () => {
    const g = goal.value.trim();
    if (!g) { toast.warn('请填写 cycle 目标'); return; }
    if (applyChk.checked) {
      const ok = await confirmDialog(
        `确认以「应用变更」模式发起 cycle？\n目标：${g}\n产生的变更将被直接应用。`,
        { title: '高危操作确认', okText: '确认发起' });
      if (!ok) return;
    }
    const resp = await post('/autonomy/cycles', { goal: g, apply_changes: applyChk.checked });
    toast.ok('Cycle 已发起' + (resp && (resp.id || resp.cycle_id) ? `（ID: ${shortId(resp.id ?? resp.cycle_id, 10)}）` : ''));
    goal.value = '';
    applyChk.checked = false;
    applyWarn.hidden = true;
    await loadCycles();
  }));

  /* ---- cycle 列表 ---- */
  async function loadCycles() {
    listCard.innerHTML = '';
    listCard.append(h('div.card__title', h('h2', { text: 'Cycle 历史' }),
      h('button.btn.btn--sm', { onclick: loadCycles }, '刷新')), loadingBlock());
    try {
      const data = await get('/autonomy/cycles');
      let items = Array.isArray(data) ? data
        : (pick(data || {}, 'cycles', 'items', 'results') ?? []);
      // 时间倒序
      items = [...items].sort((a, b) =>
        new Date(pick(b, 'created_at', 'started_at', 'timestamp', 'time') ?? 0) -
        new Date(pick(a, 'created_at', 'started_at', 'timestamp', 'time') ?? 0));
      listCard.querySelector('.loading-block')?.remove();
      if (!items.length) {
        listCard.append(emptyState('还没有执行过 cycle', { icon: '⚙️', sub: '可在上方发起第一个自治循环。' }));
        return;
      }
      listCard.append(h('ul.list-clean', items.map(it => {
        const id = pick(it, 'id', 'cycle_id', 'uuid');
        const goalText = pick(it, 'goal', 'title', 'summary') ?? '(无目标描述)';
        const status = pick(it, 'status', 'state', 'phase');
        const applied = pick(it, 'apply_changes', 'applied');
        const when = pick(it, 'created_at', 'started_at', 'timestamp', 'time');
        return h('li.list-item', { onclick: () => id !== undefined && openCycle(id) },
          h('div.li-head',
            h('span.li-title', { text: String(goalText) }),
            h('span', { style: 'display:flex;gap:8px;align-items:center' },
              applied ? h('span.badge.badge--warn', { text: '应用变更' }) : null,
              status ? statusBadge(status) : null)),
          h('div.li-sub',
            h('span.mono', { text: shortId(id ?? '?', 20) }),
            when ? ` · ${fmtTime(when)}` : ''));
      })));
    } catch (err) {
      listCard.querySelector('.loading-block')?.remove();
      listCard.append(errorBlock(err, loadCycles));
    }
  }

  async function openCycle(id) {
    openDrawer('Cycle 详情', loadingBlock());
    try {
      const d = await get(`/autonomy/cycles/${encodeURIComponent(id)}`);
      const body = h('div');
      const st = pick(d, 'status', 'state', 'phase');
      if (st) body.append(h('p', {}, statusBadge(st)));
      body.append(kvList([
        ['ID', pick(d, 'id', 'cycle_id', 'uuid')],
        ['目标', pick(d, 'goal', 'title')],
        ['应用变更', pick(d, 'apply_changes')],
        ['开始时间', fmtTime(pick(d, 'started_at', 'created_at', 'timestamp'))],
        ['结束时间', fmtTime(pick(d, 'ended_at', 'finished_at', 'completed_at'))],
        ['结果摘要', pick(d, 'summary', 'result_summary', 'outcome')],
      ]));
      // 步骤/产物等其余字段整体展示
      const known = new Set(['id', 'cycle_id', 'uuid', 'goal', 'title', 'apply_changes',
        'status', 'state', 'phase', 'started_at', 'created_at', 'timestamp',
        'ended_at', 'finished_at', 'completed_at', 'summary', 'result_summary', 'outcome']);
      const extra = Object.entries(d || {}).filter(([k]) => !known.has(k));
      if (extra.length) {
        body.append(h('h3', { style: 'margin-top:16px', text: '步骤与产物' }),
          renderValue(Object.fromEntries(extra)));
      }
      openDrawer(`Cycle · ${shortId(id, 10)}`, body);
    } catch (err) {
      openDrawer('Cycle 详情', errorBlock(err));
    }
  }

  /* ---- 进化状态面板 ---- */
  async function loadEvolution() {
    try {
      const d = await get('/evolution/status');
      evoCard.innerHTML = '';
      evoCard.append(h('div.card__title', h('h2', { text: '进化状态' }),
        h('button.btn.btn--sm', { onclick: loadEvolution }, '刷新')));
      const session = pick(d, 'session', 'current_session');
      const prs = pick(d, 'promotion_requests', 'promotions', 'requests') ?? [];

      const sec1 = h('div');
      sec1.append(h('h3', { text: '当前 Session' }));
      if (session && typeof session === 'object') {
        sec1.append(kvList(Object.entries(session).slice(0, 8).map(([k, v]) =>
          [k, /_at$|time/.test(k) && v ? fmtTime(v) : v])));
      } else {
        sec1.append(h('p', {}, renderValue(session ?? '—')));
      }

      const sec2 = h('div', { style: 'margin-top:16px' });
      sec2.append(h('h3', { text: `晋升请求（${Array.isArray(prs) ? prs.length : 0}）` }));
      if (Array.isArray(prs) && prs.length) {
        sec2.append(h('ul.list-clean', prs.map(pr => {
          const id = pick(pr, 'id', 'request_id', 'operation_id');
          const status = pick(pr, 'status', 'state') ?? 'pending';
          const title = pick(pr, 'title', 'summary', 'description', 'kind') ?? '晋升请求';
          return h('li.list-item', { style: 'cursor:default' },
            h('div.li-head',
              h('span.li-title', { text: String(title) }),
              statusBadge(status)),
            h('div.li-sub', h('span.mono', { text: shortId(id ?? '?', 24) })));
        })));
      } else {
        sec2.append(h('p.muted', { text: '暂无晋升请求。' }));
      }
      evoCard.append(sec1, sec2);
    } catch (err) {
      evoCard.innerHTML = '';
      evoCard.append(h('div.card__title', h('h2', { text: '进化状态' })), errorBlock(err, loadEvolution));
    }
  }

  root.append(
    h('div.card',
      h('div.card__title', h('h2', { text: '发起新 Cycle' })),
      h('div.field', h('label', { text: '目标 *' }), goal),
      h('div.field',
        h('label.checkbox-row', applyChk,
          h('span', {}, 'apply_changes（直接应用变更，默认关闭）'))),
      applyWarn,
      h('div.btn-row', runBtn)),
    listCard, evoCard);

  await Promise.allSettled([loadCycles(), loadEvolution()]);
}
