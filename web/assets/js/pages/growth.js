/**
 * growth.js — 自我成长：路线图 / 反思 / 意向 / 自优化（四个 Tab）
 */
import { get, post } from '../api.js';
import {
  h, toast, loadingBlock, errorBlock, emptyState, statusBadge,
  fmtTime, shortId, pick, kvList, renderValue, openDrawer, confirmDialog, withBtn,
} from '../ui.js';

const TABS = [
  ['roadmap', '路线图'],
  ['reflections', '反思'],
  ['intentions', '意向'],
  ['optimization', '自优化'],
];

export async function renderGrowth(root) {
  const tabBar = h('div.tabs', { role: 'tablist' });
  const panel = h('div', { style: 'min-height:200px' });

  root.append(
    h('div.page-head',
      h('h1', { text: '自我成长' }),
      h('p.page-desc', { text: 'AgenElf 的路线图、反思记录、成长意向与自优化覆盖。' })),
    tabBar, panel);

  let disposed = false;
  let activeTab = TABS[0][0];
  let cleanupTab = null;

  for (const [key, label] of TABS) {
    tabBar.append(h('button', {
      role: 'tab',
      class: key === activeTab ? 'is-active' : '',
      onclick: () => switchTab(key),
    }, label));
  }

  async function switchTab(key) {
    if (disposed) return;
    activeTab = key;
    tabBar.querySelectorAll('button').forEach((b, i) =>
      b.classList.toggle('is-active', TABS[i][0] === key));
    if (typeof cleanupTab === 'function') { try { cleanupTab(); } catch {} cleanupTab = null; }
    panel.innerHTML = '';
    panel.append(loadingBlock());
    try {
      if (key === 'roadmap') await tabRoadmap(panel);
      else if (key === 'reflections') await tabReflections(panel);
      else if (key === 'intentions') cleanupTab = await tabIntentions(panel);
      else if (key === 'optimization') await tabOptimization(panel);
    } catch (err) {
      panel.innerHTML = '';
      panel.append(errorBlock(err, () => switchTab(key)));
    }
  }

  await switchTab(activeTab);
  return () => { disposed = true; if (typeof cleanupTab === 'function') cleanupTab(); };
}

/* ================= 路线图 ================= */

async function tabRoadmap(panel) {
  const data = await get('/self/roadmap', { limit: 50 });
  panel.innerHTML = '';
  const items = Array.isArray(data) ? data
    : (pick(data || {}, 'roadmap', 'items', 'entries', 'milestones') ?? []);

  panel.append(h('div.card',
    h('div.card__title', h('h2', { text: '成长路线图' }),
      h('span.badge.badge--accent', { text: `${items.length} 项` })),
    items.length
      ? h('ul.list-clean', items.map(it => {
          const title = pick(it, 'title', 'name', 'goal', 'summary') ?? '(无标题)';
          const status = pick(it, 'status', 'state', 'phase');
          const desc = pick(it, 'description', 'detail', 'rationale', 'notes');
          const when = pick(it, 'created_at', 'updated_at', 'target_date', 'timestamp');
          return h('li.list-item',
            h('div.li-head',
              h('span.li-title', { text: String(title) }),
              h('span', { style: 'display:flex;gap:8px;align-items:center' },
                when ? h('span.small.muted.nowrap', { text: fmtTime(when) }) : null,
                status ? statusBadge(status) : null)),
            desc ? h('div.li-sub', { text: String(desc) }) : null);
        }))
      : emptyState('路线图暂无条目', { icon: '🌱', sub: '后端尚未生成成长路线图。' })));
}

/* ================= 反思 ================= */

async function tabReflections(panel) {
  panel.innerHTML = '';

  // 提交表单
  const note = h('textarea.textarea', { placeholder: '记录一条反思：观察到了什么、下次如何改进…', rows: 3 });
  const deep = h('input', { type: 'checkbox' });
  const submitBtn = h('button.btn.btn--primary', {}, '提交反思');
  const listCard = h('div.card', h('div.card__title', h('h2', { text: '历史反思' })), loadingBlock());

  async function loadList() {
    listCard.innerHTML = '';
    listCard.append(h('div.card__title', h('h2', { text: '历史反思' })), loadingBlock());
    try {
      const data = await get('/self/reflections', { limit: 30 });
      const items = Array.isArray(data) ? data
        : (pick(data || {}, 'reflections', 'items', 'entries') ?? []);
      listCard.innerHTML = '';
      listCard.append(h('div.card__title',
        h('h2', { text: '历史反思' }),
        h('span.badge.badge--accent', { text: `${items.length} 条` })));
      if (!items.length) {
        listCard.append(emptyState('还没有反思记录', { icon: '🪞' }));
        return;
      }
      listCard.append(h('ul.list-clean', items.map(it => {
        const text = pick(it, 'note', 'content', 'text', 'reflection') ?? JSON.stringify(it);
        const isDeep = pick(it, 'deep', 'is_deep');
        const when = pick(it, 'created_at', 'timestamp', 'time');
        return h('li.list-item', { style: 'cursor:default' },
          h('div.li-head',
            h('span.small.muted', { text: fmtTime(when) }),
            isDeep ? h('span.badge.badge--accent', { text: '深度反思' }) : null),
          h('div', { style: 'margin-top:5px;white-space:pre-wrap', text: String(text) }));
      })));
    } catch (err) {
      listCard.innerHTML = '';
      listCard.append(errorBlock(err, loadList));
    }
  }

  submitBtn.addEventListener('click', () => withBtn(submitBtn, async () => {
    const text = note.value.trim();
    if (!text) { toast.warn('请先填写反思内容'); return; }
    await post('/self/reflections', { note: text, deep: deep.checked });
    toast.ok('反思已提交');
    note.value = '';
    deep.checked = false;
    await loadList();
  }));

  panel.append(
    h('div.card',
      h('div.card__title', h('h2', { text: '提交新反思' })),
      h('div.field', h('label', { text: '反思内容' }), note),
      h('div.field',
        h('label.checkbox-row', deep,
          h('span', {}, '深度反思（deep）',
            h('div.hint', { text: '深度反思可能触发更重的内省流程，耗时更长。' })))),
      h('div.btn-row', submitBtn)),
    listCard);
  await loadList();
}

/* ================= 意向 ================= */

const INTENTION_STATUSES = ['', 'proposed', 'active', 'in_progress', 'completed', 'blocked', 'cancelled'];

async function tabIntentions(panel) {
  panel.innerHTML = '';
  let disposed = false;

  // 筛选 + 列表
  const filter = h('select.select', { style: 'max-width:200px' },
    INTENTION_STATUSES.map(s => h('option', { value: s, text: s ? s : '全部状态' })));
  const newBtn = h('button.btn.btn--primary.btn--sm', {}, '＋ 新建意向');
  const listCard = h('div.card',
    h('div.card__title',
      h('h2', { text: '意向列表' }),
      h('span', { style: 'display:flex;gap:10px;align-items:center' }, filter, newBtn)),
    loadingBlock());

  async function loadList() {
    if (disposed) return;
    listCard.querySelector('.loading-block')?.remove();
    const body = loadingBlock();
    listCard.append(body);
    try {
      const data = await get('/self/intentions', { status: filter.value, limit: 50 });
      const items = Array.isArray(data) ? data
        : (pick(data || {}, 'intentions', 'items', 'entries') ?? []);
      body.remove();
      listCard.querySelectorAll('.list-clean,.empty-state').forEach(n => n.remove());
      if (!items.length) {
        listCard.append(emptyState('当前筛选下没有意向', { icon: '🎯' }));
        return;
      }
      listCard.append(h('ul.list-clean', items.map(it => {
        const id = pick(it, 'id', 'intention_id', 'uuid');
        const title = pick(it, 'title', 'name', 'goal') ?? '(无标题)';
        const status = pick(it, 'status', 'state');
        const priority = pick(it, 'priority');
        const when = pick(it, 'created_at', 'updated_at', 'timestamp');
        return h('li.list-item', {
          onclick: () => id !== undefined && openIntention(id),
        },
          h('div.li-head',
            h('span.li-title', { text: String(title) }),
            h('span', { style: 'display:flex;gap:8px;align-items:center' },
              priority !== undefined ? h('span.badge', { text: `P:${priority}` }) : null,
              status ? statusBadge(status) : null)),
          h('div.li-sub',
            h('span.mono', { text: shortId(id ?? '?', 18) }),
            when ? ` · ${fmtTime(when)}` : ''));
      })));
    } catch (err) {
      body.remove();
      listCard.append(errorBlock(err, loadList));
    }
  }

  async function openIntention(id) {
    openDrawer('意向详情', loadingBlock());
    try {
      const d = await get(`/self/intentions/${encodeURIComponent(id)}`);
      const body = h('div');
      body.append(kvList([
        ['ID', pick(d, 'id', 'intention_id', 'uuid')],
        ['标题', pick(d, 'title', 'name', 'goal')],
        ['状态', undefined], // 用 badge 单独渲染
        ['优先级', pick(d, 'priority')],
        ['理由', pick(d, 'rationale', 'reason', 'motivation')],
        ['验收标准', fmtCriteria(pick(d, 'acceptance_criteria', 'criteria'))],
        ['创建时间', fmtTime(pick(d, 'created_at', 'timestamp'))],
        ['更新时间', fmtTime(pick(d, 'updated_at'))],
      ].filter(([, v]) => v !== undefined)));
      const st = pick(d, 'status', 'state');
      if (st) body.insertBefore(h('p', {}, statusBadge(st)), body.firstChild);
      // 其余未识别字段也展示出来，避免信息丢失
      const known = new Set(['id', 'intention_id', 'uuid', 'title', 'name', 'goal', 'status',
        'state', 'priority', 'rationale', 'reason', 'motivation', 'acceptance_criteria',
        'criteria', 'created_at', 'updated_at', 'timestamp']);
      const extra = Object.entries(d || {}).filter(([k]) => !known.has(k));
      if (extra.length) {
        body.append(h('h3', { style: 'margin-top:16px', text: '其他字段' }),
          renderValue(Object.fromEntries(extra)));
      }
      openDrawer(`意向 · ${shortId(id, 10)}`, body);
    } catch (err) {
      openDrawer('意向详情', errorBlock(err));
    }
  }

  function fmtCriteria(c) {
    if (!c) return undefined;
    if (Array.isArray(c)) return c.map(x => `• ${typeof x === 'string' ? x : JSON.stringify(x)}`).join('\n');
    return typeof c === 'string' ? c : JSON.stringify(c, null, 2);
  }

  function openCreateForm() {
    const title = h('input.input', { placeholder: '意向标题（要达成什么）' });
    const rationale = h('textarea.textarea', { rows: 3, placeholder: '为什么值得做（rationale）' });
    const priority = h('select.select',
      ['low', 'medium', 'high', 'critical'].map(p => h('option', { value: p, text: p, selected: p === 'medium' ? '' : null })));
    const criteria = h('textarea.textarea', { rows: 3, placeholder: '验收标准，每行一条' });
    const submit = h('button.btn.btn--primary', {}, '创建意向');

    submit.addEventListener('click', () => withBtn(submit, async () => {
      if (!title.value.trim()) { toast.warn('请填写标题'); return; }
      const body = {
        title: title.value.trim(),
        rationale: rationale.value.trim(),
        priority: priority.value,
        acceptance_criteria: criteria.value.split('\n').map(s => s.trim()).filter(Boolean),
      };
      await post('/self/intentions', body);
      toast.ok('意向已创建');
      await loadList();
      // 重置表单
      title.value = rationale.value = criteria.value = '';
      priority.value = 'medium';
    }));

    panel.querySelector('#intention-form-card')?.remove();
    panel.prepend(h('div.card#intention-form-card',
      h('div.card__title', h('h2', { text: '新建意向' })),
      h('div.field', h('label', { text: '标题 *' }), title),
      h('div.field', h('label', { text: '理由' }), rationale),
      h('div.form-row',
        h('div.field', h('label', { text: '优先级' }), priority),
        h('div.field', h('label', { text: '验收标准' }), criteria)),
      h('div.btn-row', submit)));
    title.focus();
  }

  filter.addEventListener('change', loadList);
  newBtn.addEventListener('click', openCreateForm);

  panel.append(listCard);
  await loadList();
  return () => { disposed = true; };
}

/* ================= 自优化 ================= */

async function tabOptimization(panel) {
  const data = await get('/self/optimization');
  panel.innerHTML = '';

  panel.append(h('div.banner',
    h('span.banner__ico', { text: '⚠️' }),
    h('div', {},
      h('p', {}, h('strong', { text: '高危区域：' }), '「应用覆盖」与「回滚」会直接改变代理运行时行为。'),
      h('p.small.muted', { text: '执行前请确认参数无误；所有操作均要求二次确认。' }))));

  // 当前覆盖值表格
  const overrides = pick(data || {}, 'overrides', 'active_overrides', 'values', 'current') ?? data;
  const card = h('div.card',
    h('div.card__title', h('h2', { text: '当前优化覆盖' })));
  const rows = normalizeOverrides(overrides);
  if (rows.length) {
    card.append(h('div.table-wrap',
      h('table.table',
        h('thead', h('tr', h('th', { text: '参数' }), h('th', { text: '当前值' }), h('th', { text: '说明/来源' }))),
        h('tbody', rows.map(([k, v, note]) => h('tr',
          h('td.mono', { text: k }),
          h('td', {}, renderValue(v)),
          h('td.small.muted', { text: note || '—' })))))));
  } else {
    card.append(emptyState('当前没有生效的优化覆盖', { icon: '🧩', sub: '可通过下方表单应用新的覆盖参数。' }));
  }
  panel.append(card);

  // apply / rollback 表单
  const key = h('input.input', { placeholder: '参数名，例如 planner.temperature' });
  const value = h('input.input.mono', { placeholder: '参数值（数字 / true|false / 字符串）' });
  const reason = h('input.input', { placeholder: '变更理由（可选）' });
  const applyBtn = h('button.btn.btn--danger', {}, '应用覆盖（高危）');
  const rbTarget = h('input.input', { placeholder: '回滚目标：参数名或快照 ID（留空回滚全部）' });
  const rollbackBtn = h('button.btn.btn--danger', {}, '执行回滚（高危）');

  applyBtn.addEventListener('click', () => withBtn(applyBtn, async () => {
    const k = key.value.trim();
    if (!k) { toast.warn('请填写参数名'); return; }
    if (value.value.trim() === '') { toast.warn('请填写参数值'); return; }
    const ok = await confirmDialog(
      `确认应用优化覆盖？\n参数：${k}\n值：${value.value.trim()}\n此操作会立即影响代理行为。`,
      { title: '高危操作确认', okText: '确认应用' });
    if (!ok) return;
    await post('/self/optimization/apply', {
      key: k,
      value: parseValue(value.value.trim()),
      reason: reason.value.trim() || undefined,
    });
    toast.ok('覆盖已应用');
    await tabOptimizationRefresh(panel);
  }));

  rollbackBtn.addEventListener('click', () => withBtn(rollbackBtn, async () => {
    const t = rbTarget.value.trim();
    const ok = await confirmDialog(
      t ? `确认回滚「${t}」的优化覆盖？` : '未指定目标，将回滚全部优化覆盖。确认继续？',
      { title: '高危操作确认', okText: '确认回滚' });
    if (!ok) return;
    await post('/self/optimization/rollback', t ? { key: t } : {});
    toast.ok('回滚已执行');
    await tabOptimizationRefresh(panel);
  }));

  panel.append(h('div.grid.grid--2',
    h('div.card',
      h('div.card__title', h('h2', { text: '应用新覆盖' })),
      h('div.field', h('label', { text: '参数名 *' }), key),
      h('div.field', h('label', { text: '参数值 *' }), value,
        h('div.hint', { text: '自动识别数字与布尔值，其余按字符串提交。' })),
      h('div.field', h('label', { text: '理由' }), reason),
      h('div.btn-row', applyBtn)),
    h('div.card',
      h('div.card__title', h('h2', { text: '回滚' })),
      h('div.field', h('label', { text: '回滚目标' }), rbTarget,
        h('div.hint', { text: '留空表示回滚全部覆盖。' })),
      h('div.btn-row', rollbackBtn))));

  async function tabOptimizationRefresh(p) {
    p.innerHTML = '';
    p.append(loadingBlock());
    try { await tabOptimization(p); }
    catch (err) { p.innerHTML = ''; p.append(errorBlock(err)); }
  }
}

/** 把覆盖数据归一化为 [key, value, note][] 行 */
function normalizeOverrides(overrides) {
  if (!overrides) return [];
  if (Array.isArray(overrides)) {
    return overrides.map(it => {
      if (it && typeof it === 'object') {
        return [
          String(pick(it, 'key', 'name', 'param', 'parameter') ?? '?'),
          pick(it, 'value', 'current', 'override'),
          pick(it, 'reason', 'note', 'source', 'applied_at'),
        ];
      }
      return [String(it), '', ''];
    });
  }
  if (typeof overrides === 'object') {
    return Object.entries(overrides).map(([k, v]) => {
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        return [k, pick(v, 'value', 'current') ?? v, pick(v, 'reason', 'note', 'source')];
      }
      return [k, v, ''];
    });
  }
  return [];
}

function parseValue(s) {
  if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
  if (s === 'true') return true;
  if (s === 'false') return false;
  if (s === 'null') return null;
  return s;
}
