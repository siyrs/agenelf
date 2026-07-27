/**
 * memory.js — 记忆：写入（fact/preference）+ 搜索
 */
import { get, post } from '../api.js';
import {
  h, toast, emptyState, loadingBlock, errorBlock, fmtTime, pick, renderValue, withBtn,
} from '../ui.js';

export async function renderMemory(root) {
  root.append(h('div.page-head',
    h('h1', { text: '记忆' }),
    h('p.page-desc', { text: '向 AgenElf 的长期记忆写入事实或偏好，并检索已有记忆。' })));

  /* ---- 写入表单 ---- */
  const kind = h('select.select',
    h('option', { value: 'fact', text: 'fact（事实）' }),
    h('option', { value: 'preference', text: 'preference（偏好）' }));
  const content = h('textarea.textarea', {
    rows: 3,
    placeholder: '例如：「部署窗口为每周四 22:00 后」或「偏好简洁的中文回复」',
  });
  const saveBtn = h('button.btn.btn--primary', {}, '写入记忆');

  saveBtn.addEventListener('click', () => withBtn(saveBtn, async () => {
    const text = content.value.trim();
    if (!text) { toast.warn('请填写记忆内容'); return; }
    await post('/memory', { kind: kind.value, content: text });
    toast.ok('记忆已写入');
    content.value = '';
  }));

  /* ---- 搜索 ---- */
  const q = h('input.input', { placeholder: '输入关键词搜索记忆…' });
  const limit = h('select.select',
    [10, 20, 50].map(n => h('option', { value: String(n), text: `${n} 条` })));
  const searchBtn = h('button.btn', {}, '搜索');
  const resultBox = h('div', { style: 'margin-top:14px' },
    emptyState('输入关键词开始检索', { icon: '🧠' }));

  async function search() {
    const query = q.value.trim();
    if (!query) { toast.warn('请输入搜索关键词'); return; }
    resultBox.innerHTML = '';
    resultBox.append(loadingBlock('检索中…'));
    try {
      const data = await get('/memory/search', { q: query, limit: limit.value });
      const items = Array.isArray(data) ? data
        : (pick(data || {}, 'results', 'memories', 'items', 'matches') ?? []);
      resultBox.innerHTML = '';
      if (!items.length) {
        resultBox.append(emptyState(`没有找到与「${query}」相关的记忆`, { icon: '🔍' }));
        return;
      }
      resultBox.append(h('p.small.muted', { text: `共 ${items.length} 条结果` }),
        h('div.grid.grid--2', items.map(it => {
          const k = pick(it, 'kind', 'type', 'category') ?? 'fact';
          const text = pick(it, 'content', 'text', 'value', 'memory') ?? JSON.stringify(it);
          const when = pick(it, 'created_at', 'timestamp', 'time', 'updated_at');
          const score = pick(it, 'score', 'similarity', 'relevance');
          return h('div.card.card--flat', { style: 'margin-bottom:0' },
            h('div', { style: 'display:flex;gap:8px;align-items:center;margin-bottom:7px' },
              h('span.badge', { class: k === 'preference' ? 'badge--accent' : 'badge--info', text: String(k) }),
              score !== undefined ? h('span.small.muted', { text: `相关度 ${(Number(score) * 100).toFixed(0)}%` }) : null,
              h('span.small.muted', { style: 'margin-left:auto', text: fmtTime(when) })),
            h('div', { style: 'white-space:pre-wrap', text: String(text) }));
        })));
    } catch (err) {
      resultBox.innerHTML = '';
      resultBox.append(errorBlock(err, search));
    }
  }

  searchBtn.addEventListener('click', search);
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) search();
  });

  root.append(
    h('div.grid.grid--2',
      h('div.card',
        h('div.card__title', h('h2', { text: '写入记忆' })),
        h('div.field', h('label', { text: '类型' }), kind),
        h('div.field', h('label', { text: '内容 *' }), content),
        h('div.btn-row', saveBtn)),
      h('div.card',
        h('div.card__title', h('h2', { text: '检索记忆' })),
        h('div.field', h('label', { text: '关键词' }), q),
        h('div.form-row',
          h('div.field', h('label', { text: '返回条数' }), limit),
          h('div.field', h('label', { html: '&nbsp;' }), searchBtn)),
        resultBox)),
  );
}
