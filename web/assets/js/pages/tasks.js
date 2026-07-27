/**
 * tasks.js — 任务：合并展示双来源任务的只读视图
 * GET /tasks        → { tasks: [...] }（source: board=任务板 / engine=治理引擎）
 * GET /tasks/{id}   → { source, task }（engine 含 events/evidence 审计历史）
 */
import { get } from '../api.js';
import {
  h, loadingBlock, errorBlock, emptyState, statusBadge,
  fmtTime, shortId, pick, kvList, renderValue, openDrawer,
} from '../ui.js';

const SOURCE_LABEL = { board: '任务板', engine: '治理引擎' };

function sourceBadge(source) {
  const label = SOURCE_LABEL[source] || source || '未知来源';
  return h(`span.badge.badge--${source === 'engine' ? 'accent' : 'info'}`, { text: label });
}

export async function renderTasks(root) {
  root.append(h('div.page-head',
    h('h1', { text: '任务' }),
    h('p.page-desc', {
      text: '合并展示结构化任务板（workspace/tasks）与治理任务引擎（data/tasks）的任务，均为只读视图。',
    })));

  const statusSel = h('select.select', {}, h('option', { value: '', text: '全部状态' }));
  const refreshBtn = h('button.btn.btn--sm', { type: 'button' }, '刷新');
  const listBody = h('div', {}, loadingBlock());

  root.append(h('div.card',
    h('div.card__title', h('h2', { text: '任务列表' }),
      h('div.btn-row', { style: 'flex-wrap:nowrap' }, statusSel, refreshBtn)),
    listBody));

  statusSel.addEventListener('change', loadTasks);
  refreshBtn.addEventListener('click', loadTasks);

  let knownStatuses = '';

  /** 根据未过滤的全量结果动态补充状态下拉（两个来源状态机不同） */
  function syncStatusOptions(tasks) {
    const statuses = [...new Set(
      tasks.map(t => String(pick(t, 'status') ?? '')).filter(Boolean),
    )].sort();
    const key = statuses.join('|');
    if (key === knownStatuses) return;
    const current = statusSel.value;
    knownStatuses = key;
    statusSel.innerHTML = '';
    statusSel.append(h('option', { value: '', text: '全部状态' }));
    for (const s of statuses) statusSel.append(h('option', { value: s, text: s }));
    statusSel.value = statuses.includes(current) ? current : '';
  }

  async function loadTasks() {
    const status = statusSel.value;
    listBody.innerHTML = '';
    listBody.append(loadingBlock());
    try {
      const d = await get('/tasks', status ? { status } : null);
      const tasks = Array.isArray(d) ? d : (pick(d || {}, 'tasks', 'items') ?? []);
      if (!status) syncStatusOptions(tasks); // 仅全量时刷新状态选项，避免选项被过滤收窄
      listBody.innerHTML = '';
      if (!tasks.length) {
        listBody.append(emptyState(
          status ? `没有状态为「${status}」的任务` : '暂无任务',
          { icon: '🗂️', sub: '任务板与治理引擎都没有匹配的任务记录。' }));
        return;
      }
      listBody.append(h('ul.list-clean', tasks.map(t => {
        const id = pick(t, 'id', 'task_id');
        const title = pick(t, 'title', 'summary') ?? '(无标题)';
        const progress = pick(t, 'progress');
        const when = pick(t, 'updated_at', 'created_at');
        return h('li.list-item', { onclick: () => id !== undefined && id !== null && openTask(id) },
          h('div.li-head',
            h('span.li-title', { text: String(title) }),
            h('span', { style: 'display:flex;gap:8px;align-items:center;flex-shrink:0' },
              sourceBadge(t.source),
              statusBadge(pick(t, 'status') ?? ''))),
          h('div.li-sub',
            h('span.mono', { text: shortId(id ?? '?', 24) }),
            progress ? ` · 进度 ${progress}` : '',
            when ? ` · ${fmtTime(when)}` : ''));
      })));
    } catch (err) {
      listBody.innerHTML = '';
      listBody.append(errorBlock(err, loadTasks));
    }
  }

  async function openTask(id) {
    openDrawer('任务详情', loadingBlock());
    try {
      const d = await get(`/tasks/${encodeURIComponent(id)}`);
      const task = pick(d, 'task') ?? d ?? {};
      const body = h('div');

      body.append(h('p', { style: 'display:flex;gap:8px;align-items:center' },
        sourceBadge(pick(d, 'source') ?? task.source),
        statusBadge(pick(task, 'status') ?? '')));

      body.append(kvList([
        ['ID', pick(task, 'id', 'task_id')],
        ['标题', pick(task, 'title', 'summary')],
        ['优先级', pick(task, 'priority')],
        ['进度', pick(task, 'progress')],
        ['创建时间', fmtTime(pick(task, 'created_at'))],
        ['更新时间', fmtTime(pick(task, 'updated_at'))],
        ['完成时间', pick(task, 'done_at') ? fmtTime(task.done_at) : undefined],
        ['阻塞原因', pick(task, 'block_reason') || undefined],
        ['关联改进意向', pick(task, 'linked_intention') || undefined],
        ['来源渠道', pick(task, 'source_channel')],
        ['版本号', pick(task, 'revision')],
      ]));

      // 步骤列表（board: {text,status,note}；engine: {title,status,risk,...}）
      const steps = Array.isArray(task.steps) ? task.steps : [];
      if (steps.length) {
        body.append(h('h3', { style: 'margin-top:16px', text: `步骤（${steps.length}）` }),
          h('ul.list-clean', steps.map((s, i) => {
            const step = typeof s === 'string' ? { text: s } : (s || {});
            const label = pick(step, 'text', 'title') ?? `步骤 ${i + 1}`;
            const risk = pick(step, 'risk');
            return h('li.list-item', { style: 'cursor:default' },
              h('div.li-head',
                h('span.li-title', { text: `${i + 1}. ${label}` }),
                h('span', { style: 'display:flex;gap:8px;align-items:center;flex-shrink:0' },
                  risk ? h('span.badge.badge--info', { text: risk }) : null,
                  step.status ? statusBadge(step.status) : null)),
              step.note ? h('div.li-sub', { text: String(step.note) }) : null);
          })));
      }

      // 证据（board: 字符串列表；engine: {kind,reference,summary,trusted,...}）
      const evidence = Array.isArray(task.evidence) ? task.evidence : [];
      if (evidence.length) {
        body.append(h('h3', { style: 'margin-top:16px', text: `证据（${evidence.length}）` }),
          h('ul.list-clean', evidence.map(ev => {
            if (typeof ev === 'string') {
              return h('li.list-item', { style: 'cursor:default' },
                h('div.li-sub.mono', { text: ev }));
            }
            const item = ev || {};
            return h('li.list-item', { style: 'cursor:default' },
              h('div.li-head',
                h('span.li-title', { text: String(pick(item, 'reference') ?? '—') }),
                h('span', { style: 'display:flex;gap:8px;align-items:center;flex-shrink:0' },
                  item.kind ? h('span.badge.badge--info', { text: String(item.kind) }) : null,
                  item.trusted ? h('span.badge.badge--ok', { text: '可信' }) : null)),
              item.summary ? h('div.li-sub', { text: String(item.summary) }) : null);
          })));
      }

      // 事件历史（engine 审计字段）
      const events = Array.isArray(task.events) ? task.events : [];
      if (events.length) {
        body.append(h('h3', { style: 'margin-top:16px', text: `事件历史（${events.length}）` }),
          h('ul.list-clean', events.slice().reverse().map(ev => {
            const item = ev || {};
            return h('li.list-item', { style: 'cursor:default' },
              h('div.li-head',
                h('span.li-title', { text: String(pick(item, 'event') ?? 'event') }),
                h('span.small.muted', { text: fmtTime(pick(item, 'at')) })),
              item.detail ? h('div.li-sub', { text: String(item.detail) }) : null);
          })));
      }

      // 其余字段整体展示
      const known = new Set(['id', 'task_id', 'title', 'summary', 'status', 'priority', 'progress',
        'created_at', 'updated_at', 'done_at', 'block_reason', 'linked_intention', 'source_channel',
        'revision', 'steps', 'evidence', 'events', 'source']);
      const extra = Object.entries(task).filter(([k]) => !known.has(k));
      if (extra.length) {
        body.append(h('h3', { style: 'margin-top:16px', text: '其他字段' }),
          renderValue(Object.fromEntries(extra)));
      }
      openDrawer(`任务 · ${shortId(id, 16)}`, body);
    } catch (err) {
      openDrawer('任务详情', errorBlock(err));
    }
  }

  await loadTasks();
}
