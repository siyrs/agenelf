/**
 * settings.js — 设置：API Token、主题、关于
 */
import { getToken, setToken, probeConnection, get } from '../api.js';
import { h, toast, withBtn, renderValue } from '../ui.js';
import { getTheme, setTheme } from '../../app.js';

export async function renderSettings(root) {
  root.append(h('div.page-head',
    h('h1', { text: '设置' }),
    h('p.page-desc', { text: '配置 API Token 与界面偏好。Token 仅保存在本浏览器 localStorage 中。' })));

  /* ---- Token ---- */
  const tokenInput = h('input.input.mono', {
    type: 'password',
    placeholder: '输入 API Token…',
    value: getToken(),
    autocomplete: 'off',
  });
  const showChk = h('input', { type: 'checkbox' });
  showChk.addEventListener('change', () => {
    tokenInput.type = showChk.checked ? 'text' : 'password';
  });
  const saveBtn = h('button.btn.btn--primary', {}, '保存并测试连接');
  const clearBtn = h('button.btn', {}, '清除 Token');
  const testResult = h('div', { style: 'margin-top:12px' });

  saveBtn.addEventListener('click', () => withBtn(saveBtn, async () => {
    setToken(tokenInput.value);
    toast.ok('Token 已保存，正在测试连接…');
    testResult.innerHTML = '';
    try {
      await get('/health', null, { silent: true, timeout: 8000 });
      testResult.append(h('p', {}, h('span.badge.badge--ok', { text: '/health 通过' })));
    } catch (err) {
      testResult.append(h('p', {}, h('span.badge.badge--danger', { text: `/health 失败：${err.message}` })));
    }
    try {
      const st = await get('/status', null, { silent: true, timeout: 8000 });
      testResult.append(
        h('p', {}, h('span.badge.badge--ok', { text: '/status 通过（认证成功）' })),
        h('details', {}, h('summary.small.muted', { text: '查看 /status 响应' }), renderValue(st)));
      toast.ok('连接测试通过');
    } catch (err) {
      const msg = err.status === 401
        ? '认证失败（401）：Token 无效或已过期'
        : err.status === 503
          ? '后端未配置 Token（503）：请先在服务端配置，或留空 Token 重试'
          : `/status 失败：${err.message}`;
      testResult.append(h('p', {}, h('span.badge.badge--warn', { text: msg })));
      toast.warn(msg);
    }
    probeConnection();
  }));

  clearBtn.addEventListener('click', () => {
    setToken('');
    tokenInput.value = '';
    toast.info('Token 已清除');
    probeConnection();
  });

  /* ---- 主题 ---- */
  const themeLight = h('input', { type: 'radio', name: 'theme', value: 'light', checked: getTheme() === 'light' ? '' : null });
  const themeDark = h('input', { type: 'radio', name: 'theme', value: 'dark', checked: getTheme() === 'dark' ? '' : null });
  themeLight.addEventListener('change', () => { setTheme('light'); toast.info('已切换为浅色主题'); });
  themeDark.addEventListener('change', () => { setTheme('dark'); toast.info('已切换为深色主题'); });

  root.append(
    h('div.card',
      h('div.card__title', h('h2', { text: 'API Token' })),
      h('div.field',
        h('label', { text: 'Token' }),
        tokenInput,
        h('div.hint', {}, '所有受保护端点通过 ', h('code', { text: 'X-Agenelf-Token' }), ' 请求头认证；Token 仅存于本浏览器。')),
      h('div.field',
        h('label.checkbox-row', showChk, h('span', { text: '显示 Token' }))),
      h('div.btn-row', saveBtn, clearBtn),
      testResult),

    h('div.card',
      h('div.card__title', h('h2', { text: '主题外观' })),
      h('div.field',
        h('label.checkbox-row', { style: 'margin-bottom:8px' }, themeLight, h('span', { text: '浅色（默认，米白暖调）' })),
        h('label.checkbox-row', themeDark, h('span', { text: '深色（暖灰调）' })))),

    h('div.card',
      h('div.card__title', h('h2', { text: '关于' })),
      h('p', {}, h('strong', { text: 'AgenElf Web 控制台' }), ' — 自进化代理的零构建单页控制台。'),
      h('ul', { style: 'margin:0;padding-left:1.4em;color:var(--text-soft)' },
        h('li', { text: '纯 HTML / CSS / 原生 ES 模块，无框架、无构建步骤、无外部 CDN。' }),
        h('li', {}, '假定由后端在同源 ', h('code', { text: '/ui/' }), ' 路径托管，API 请求使用相对路径。'),
        h('li', {}, '数据每 30 秒自动探测连接；状态页每 30 秒自动刷新。'))),
  );
}
