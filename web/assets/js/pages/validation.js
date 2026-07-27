/**
 * validation.js — 验证与修复
 * 验证：catalog 表格 + 运行单个 check + 轮询结果
 * 修复：提交 code-repair 请求 + 状态查询
 */
import { get, post } from '../api.js';
import {
  h, toast, loadingBlock, errorBlock, emptyState, statusBadge,
  fmtTime, shortId, pick, kvList, renderValue, withBtn,
} from '../ui.js';

const POLL_INTERVAL = 2500;
const POLL_MAX = 60; // 最多轮询次数（≈150s）

export async function renderValidation(root) {
  root.append(h('div.page-head',
    h('h1', { text: '验证与修复' }),
    h('p.page-desc', { text: '运行系统验证检查并轮询结果；提交代码修复请求并跟踪其状态。' })));

  let disposed = false;
  const pollers = new Set();
  function addPoller(id) { pollers.add(id); return id; }
  function stopPollers() { pollers.forEach(clearInterval); pollers.clear(); }

  /* ================= 验证 catalog ================= */
  const catCard = h('div.card',
    h('div.card__title', h('h2', { text: '验证检查目录' }),
      h('button.btn.btn--sm', {}, '刷新')),
    loadingBlock());

  const runResult = h('div.card', { hidden: true });

  async function loadCatalog() {
    catCard.innerHTML = '';
    catCard.append(h('div.card__title', h('h2', { text: '验证检查目录' }),
      h('button.btn.btn--sm', { onclick: loadCatalog }, '刷新')), loadingBlock());
    try {
      const data = await get('/validation/catalog');
      const items = Array.isArray(data) ? data
        : (pick(data || {}, 'checks', 'catalog', 'items') ?? []);
      catCard.querySelector('.loading-block')?.remove();
      if (!items.length) {
        catCard.append(emptyState('验证目录为空', { icon: '🧪' }));
        return;
      }
      catCard.append(h('div.table-wrap',
        h('table.table',
          h('thead', h('tr',
            h('th', { text: '检查项' }),
            h('th', { text: '描述' }),
            h('th', { text: '分类' }),
            h('th', { text: '操作' }))),
          h('tbody', items.map(it => {
            const name = typeof it === 'string' ? it : String(pick(it, 'check', 'name', 'id', 'key') ?? '?');
            const desc = typeof it === 'object' && it ? pick(it, 'description', 'desc', 'summary') : '';
            const cat = typeof it === 'object' && it ? pick(it, 'category', 'group', 'kind') : '';
            const btn = h('button.btn.btn--sm', {}, '运行');
            btn.addEventListener('click', () => withBtn(btn, () => runCheck(name)));
            return h('tr',
              h('td.mono', { text: name }),
              h('td', { text: desc ? String(desc) : '—' }),
              h('td', { text: cat ? String(cat) : '—' }),
              h('td', btn));
          })))));
    } catch (err) {
      catCard.querySelector('.loading-block')?.remove();
      catCard.append(errorBlock(err, loadCatalog));
    }
  }

  /* ---- 运行 check + 轮询 ---- */
  async function runCheck(name) {
    runResult.hidden = false;
    runResult.innerHTML = '';
    runResult.append(h('div.card__title', h('h2', { text: `运行结果：${name}` })),
      loadingBlock('正在提交…'));
    try {
      const resp = await post(`/validation/checks/${encodeURIComponent(name)}`, {});
      const resultId = pick(resp || {}, 'id', 'result_id', 'run_id');
      // 若响应直接含结果则直接展示
      const inline = pick(resp || {}, 'result', 'status', 'output');
      if (!resultId) {
        runResult.innerHTML = '';
        runResult.append(h('div.card__title', h('h2', { text: `运行结果：${name}` })),
          inline !== undefined ? renderValue(resp) : h('p.muted', { text: '已提交，但响应中未返回结果 ID。' }));
        return;
      }
      pollResult(name, resultId);
    } catch (err) {
      runResult.innerHTML = '';
      runResult.append(h('div.card__title', h('h2', { text: `运行结果：${name}` })), errorBlock(err));
      throw err;
    }
  }

  function pollResult(name, resultId) {
    let count = 0;
    const paint = (note) => {
      runResult.innerHTML = '';
      runResult.append(h('div.card__title', h('h2', { text: `运行结果：${name}` })),
        h('p', {}, loadingInline(), ` ${note}`),
        h('p.small.muted.mono', { text: `result_id: ${resultId}` }));
    };
    const loadingInline = () => h('span.spinner');
    paint('检查运行中，正在轮询结果…');

    const timer = addPoller(setInterval(async () => {
      if (disposed) { clearInterval(timer); return; }
      count++;
      try {
        const d = await get(`/validation/results/${encodeURIComponent(resultId)}`);
        const st = String(pick(d || {}, 'status', 'state') ?? '').toLowerCase();
        const done = ['ok', 'success', 'succeeded', 'done', 'completed', 'complete',
          'failed', 'error', 'pass', 'passed'].includes(st)
          || pick(d || {}, 'finished', 'done', 'completed') === true;
        if (done || count >= POLL_MAX) {
          clearInterval(timer);
          pollers.delete(timer);
          runResult.innerHTML = '';
          runResult.append(h('div.card__title', h('h2', { text: `运行结果：${name}` })));
          if (st) runResult.append(h('p', {}, statusBadge(st || 'finished')));
          runResult.append(renderValue(d));
          if (count >= POLL_MAX && !done) {
            runResult.append(h('p.small.muted', { text: '轮询已达上限，可稍后手动查询该 result_id。' }));
          }
        } else {
          paint(`检查运行中，正在轮询结果…（第 ${count} 次）`);
        }
      } catch (err) {
        clearInterval(timer);
        pollers.delete(timer);
        runResult.innerHTML = '';
        runResult.append(h('div.card__title', h('h2', { text: `运行结果：${name}` })), errorBlock(err));
      }
    }, POLL_INTERVAL));
  }

  /* ================= 代码修复 ================= */
  const repo = h('input.input.mono', { placeholder: '仓库标识，例如 agenelf 或 git URL' });
  const diff = h('textarea.textarea.textarea--mono', {
    rows: 10,
    placeholder: '粘贴 unified diff 补丁内容…\n--- a/app/example.py\n+++ b/app/example.py\n@@ ...',
    spellcheck: 'false',
  });
  const submitBtn = h('button.btn.btn--primary', {}, '提交修复请求');
  const repairResult = h('div', { style: 'margin-top:12px' });

  submitBtn.addEventListener('click', () => withBtn(submitBtn, async () => {
    if (!repo.value.trim()) { toast.warn('请填写仓库标识'); return; }
    if (!diff.value.trim()) { toast.warn('请粘贴 unified diff'); return; }
    const resp = await post('/code-repair/requests', {
      repository: repo.value.trim(),
      unified_diff: diff.value,
    });
    const rid = pick(resp || {}, 'id', 'request_id');
    toast.ok('修复请求已提交' + (rid ? `（ID: ${shortId(rid, 10)}）` : ''));
    repairResult.innerHTML = '';
    repairResult.append(h('div.banner.banner--info',
      h('span.banner__ico', { text: '📨' }),
      h('div', {},
        h('p', {}, '请求已提交。', rid ? h('span', {}, '请求 ID：', h('code', { text: String(rid) })) : null),
        h('p.small.muted', { text: '可将 ID 粘贴到下方查询框跟踪状态。' }))));
    if (rid) queryInput.value = String(rid);
    diff.value = '';
  }));

  const queryInput = h('input.input.mono', { placeholder: '输入修复请求 ID 查询状态…' });
  const queryBtn = h('button.btn', {}, '查询');
  const queryResult = h('div', { style: 'margin-top:14px' });

  async function queryRepair() {
    const id = queryInput.value.trim();
    if (!id) { toast.warn('请输入请求 ID'); return; }
    queryResult.innerHTML = '';
    queryResult.append(loadingBlock('查询中…'));
    try {
      const d = await get(`/code-repair/requests/${encodeURIComponent(id)}`);
      queryResult.innerHTML = '';
      const card = h('div.card.card--flat', { style: 'margin-bottom:0' });
      const st = pick(d, 'status', 'state', 'phase');
      card.append(
        h('div', { style: 'display:flex;gap:10px;align-items:center;margin-bottom:10px' },
          h('strong', { text: '修复请求状态' }), st ? statusBadge(st) : null),
        kvList([
          ['ID', pick(d, 'id', 'request_id') ?? id],
          ['仓库', pick(d, 'repository', 'repo')],
          ['创建时间', fmtTime(pick(d, 'created_at', 'timestamp'))],
          ['结论', pick(d, 'verdict', 'outcome', 'summary', 'message')],
        ]),
        h('h3', { style: 'margin-top:12px', text: '完整数据' }),
        renderValue(d));
      queryResult.append(card);
    } catch (err) {
      queryResult.innerHTML = '';
      queryResult.append(errorBlock(err, queryRepair));
    }
  }

  queryBtn.addEventListener('click', queryRepair);
  queryInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) queryRepair();
  });

  root.append(
    catCard,
    runResult,
    h('h2', { style: 'margin:26px 0 12px', text: '代码修复' }),
    h('div.grid.grid--2',
      h('div.card',
        h('div.card__title', h('h2', { text: '提交修复请求' })),
        h('div.field', h('label', { text: '仓库 *' }), repo),
        h('div.field', h('label', { text: 'Unified Diff *' }), diff,
          h('div.hint', { text: '仅接受标准 unified diff 格式补丁。' })),
        h('div.btn-row', submitBtn),
        repairResult),
      h('div.card',
        h('div.card__title', h('h2', { text: '查询修复状态' })),
        h('div.btn-row', { style: 'flex-wrap:nowrap' }, queryInput, queryBtn),
        queryResult)));

  await loadCatalog();
  return () => { disposed = true; stopPollers(); };
}
