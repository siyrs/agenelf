/**
 * approvals.js — 审批中心（只读）
 * 审批操作只能在宿主终端执行；本页仅提供状态查询与待决项总览。
 */
import { get } from '../api.js';
import {
  h, toast, loadingBlock, errorBlock, emptyState, statusBadge,
  fmtTime, shortId, pick, kvList, renderValue, withBtn,
} from '../ui.js';

export async function renderApprovals(root) {
  root.append(h('div.page-head',
    h('h1', { text: '审批中心' }),
    h('p.page-desc', { text: '查看待审批的晋升请求与操作状态。' })));

  /* ---- 只读横幅 ---- */
  root.append(h('div.banner.banner--danger',
    h('span.banner__ico', { text: '🛡️' }),
    h('div', {},
      h('p', {}, h('strong', { text: '本页面为只读。' }),
        '出于安全设计，审批操作仅可在宿主终端执行：'),
      h('p', {}, h('code', { text: 'scripts/approve.sh <operation_id>' }), ' 或 CLI 命令 ',
        h('code', { text: '/approve <operation_id>' })),
      h('p.small.muted', { text: 'Web 控制台不提供任何审批写入入口。' }))));

  /* ---- operation 查询 ---- */
  const opInput = h('input.input.mono', { placeholder: '输入 operation_id 查询状态…' });
  const opBtn = h('button.btn', {}, '查询');
  const opResult = h('div', { style: 'margin-top:14px' });

  async function queryOp() {
    const id = opInput.value.trim();
    if (!id) { toast.warn('请输入 operation_id'); return; }
    opResult.innerHTML = '';
    opResult.append(loadingBlock('查询中…'));
    try {
      const d = await get(`/operations/${encodeURIComponent(id)}`);
      opResult.innerHTML = '';
      const card = h('div.card.card--flat', { style: 'margin-bottom:0' });
      const st = pick(d, 'status', 'state', 'phase');
      card.append(
        h('div', { style: 'display:flex;gap:10px;align-items:center;margin-bottom:10px' },
          h('strong', { text: '操作状态' }),
          st ? statusBadge(st) : null),
        kvList([
          ['operation_id', pick(d, 'id', 'operation_id') ?? id],
          ['类型', pick(d, 'kind', 'type', 'operation')],
          ['摘要', pick(d, 'summary', 'title', 'description')],
          ['创建时间', fmtTime(pick(d, 'created_at', 'timestamp'))],
          ['更新时间', fmtTime(pick(d, 'updated_at', 'finished_at'))],
        ]));
      const known = new Set(['id', 'operation_id', 'status', 'state', 'phase', 'kind', 'type',
        'operation', 'summary', 'title', 'description', 'created_at', 'updated_at',
        'finished_at', 'timestamp']);
      const extra = Object.entries(d || {}).filter(([k]) => !known.has(k));
      if (extra.length) {
        card.append(h('h3', { style: 'margin-top:14px', text: '完整数据' }),
          renderValue(Object.fromEntries(extra)));
      }
      opResult.append(card);
    } catch (err) {
      opResult.innerHTML = '';
      opResult.append(errorBlock(err, queryOp));
    }
  }

  opBtn.addEventListener('click', queryOp);
  opInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) queryOp();
  });

  root.append(h('div.card',
    h('div.card__title', h('h2', { text: '按 operation_id 查询' })),
    h('div.btn-row', { style: 'flex-wrap:nowrap' }, opInput, opBtn),
    opResult));

  /* ---- 待决项列表（来自 /evolution/status） ---- */
  const pendingCard = h('div.card',
    h('div.card__title', h('h2', { text: '待决晋升请求' }),
      h('button.btn.btn--sm', {}, '刷新')),
    loadingBlock());
  root.append(pendingCard);

  async function loadPending() {
    pendingCard.innerHTML = '';
    pendingCard.append(h('div.card__title', h('h2', { text: '待决晋升请求' }),
      h('button.btn.btn--sm', { onclick: loadPending }, '刷新')), loadingBlock());
    try {
      const d = await get('/evolution/status');
      const prs = pick(d || {}, 'promotion_requests', 'promotions', 'requests') ?? [];
      const pending = (Array.isArray(prs) ? prs : []).filter(pr => {
        const s = String(pick(pr, 'status', 'state') ?? 'pending').toLowerCase();
        return ['pending', 'waiting', 'queued', 'proposed', 'open'].includes(s);
      });
      pendingCard.querySelector('.loading-block')?.remove();
      if (!pending.length) {
        pendingCard.append(emptyState('当前没有待决的晋升请求', { icon: '✅', sub: '所有请求均已处理。' }));
        return;
      }
      pendingCard.append(h('ul.list-clean', pending.map(pr => {
        const id = pick(pr, 'id', 'request_id', 'operation_id');
        const title = pick(pr, 'title', 'summary', 'description', 'kind') ?? '晋升请求';
        const when = pick(pr, 'created_at', 'timestamp', 'submitted_at');
        return h('li.list-item', { style: 'cursor:default' },
          h('div.li-head',
            h('span.li-title', { text: String(title) }),
            statusBadge(pick(pr, 'status', 'state') ?? 'pending')),
          h('div.li-sub',
            h('span.mono', { text: shortId(id ?? '?', 28) }),
            when ? ` · ${fmtTime(when)}` : ''),
          h('div.small.muted', { style: 'margin-top:6px' },
            '终端审批：', h('code', { text: `scripts/approve.sh ${id ?? '<id>'}` })));
      })));
    } catch (err) {
      pendingCard.querySelector('.loading-block')?.remove();
      pendingCard.append(errorBlock(err, loadPending));
    }
  }

  await loadPending();
}
