import { get, post } from '../api/client.js';
import { intro } from '../components/common/frame.js';
import { table } from '../components/common/data.js';
import { fieldControl, formError } from '../components/common/forms.js';
import { CorrectionStatusLabel, Role } from '../constants/enums.js';
import { bindForm, notify } from '../hooks/actions.js';
import { formValues } from '../types/form.js';
import { esc, timeLabel } from '../utils/html.js';

const cellText = row => `损伤 ${row.from_severity} 级 · 延期 ${row.from_delay} 级`;
const targetText = row => `损伤 ${row.to_severity} 级 · 延期 ${row.to_delay} 级`;

export async function correctionPage(spec, user, route) {
  const sheet = (route.id || '').trim();
  const query = sheet ? `?limit=20&sheet=${encodeURIComponent(sheet)}` : '?limit=20';
  const [{total, items}, ledger] = await Promise.all([
    get(`/corrections${query}`),
    get('/corrections/ledger'),
  ]);
  const writable = user.role !== Role.VIEWER;
  return `${intro('台账受理更正', '把指定件数从原损伤延期格移到目标格；更正按每张交付单从一号起依次入账，缺号先收下等待。',
    `<span class="count">${total} <small>张更正单</small></span>`)}
    ${writable ? `<div class="tools"><details><summary>受理一张交付单更正</summary>
      <form id="correction-form"><p>每张交付单的更正从 1 号开始编号；乱序到达先收下，缺号补齐后自动入账。原格件数不足时整笔拒绝，不会只挪一部分。</p>
      <div class="form-grid">${spec.correction_fields.map(field => fieldControl(field)).join('')}</div>
      ${formError}<button class="primary">提交更正</button></form></details></div>` : ''}
    <section class="panel"><div class="panel-heading"><h2>当前台账格件数（含已入账更正）</h2></div>
    ${table([
      {key: 'process', label: '归类工序'},
      {key: 'severity', label: '损伤等级'},
      {key: 'delay', label: '延期等级'},
      {key: 'count', label: '完成件数'},
    ], ledger.items, '台账暂无记录。')}</section>
    <section class="panel"><div class="panel-heading"><h2>${esc(spec.correction_name)}</h2>
      <form id="correction-filter" class="inline-filter"><input name="sheet" type="search" placeholder="按交付单编号筛选" value="${esc(sheet)}" maxlength="96"><button type="submit">筛选</button>${sheet ? '<a class="text-link" href="#corrections">清除</a>' : ''}</form></div>
    ${table([
      {key: 'sheet', label: '交付单编号'},
      {key: 'seq', label: '更正序号'},
      {key: 'process', label: '归类工序'},
      {label: '原格', html: row => esc(cellText(row))},
      {label: '目标格', html: row => esc(targetText(row))},
      {key: 'quantity', label: '挪动件数'},
      {label: '状态', html: row => `<span class="status status-${esc(row.status)}">${esc(CorrectionStatusLabel[row.status] || row.status)}</span>`},
      {label: '入账时间', html: row => timeLabel(row.posted_at)},
      {label: '说明', html: row => esc(row.reason || '')},
    ], items, sheet ? '该交付单暂无更正记录。' : '暂无更正记录。')}</section>`;
}

export function wireCorrections(root, spec, refresh) {
  bindForm(root.querySelector('#correction-form'), async form => {
    const payload = formValues(form, spec.correction_fields);
    if (!Object.keys(payload).length) return;
    const result = await post('/corrections', payload);
    notify(result.status === 'applied' ? '更正已入账'
      : result.status === 'rejected' ? `更正已拒绝：${result.reason}`
      : '更正已收下，等待缺号补齐后入账');
    refresh();
  });
  bindForm(root.querySelector('#correction-filter'), async form => {
    const value = form.elements.sheet.value.trim();
    location.hash = value ? `#corrections/${encodeURIComponent(value)}` : '#corrections';
  });
}
