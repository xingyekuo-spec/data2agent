(function () {
  'use strict';
  var summaries = [];
  var summaryPage = 1;
  var detailPage = 1;
  var knownSources = {};
  var knownTables = {};

  function byId(id) { return document.getElementById(id); }
  function pushStatus(status) {
    if (status === 'ok') return badge('ok', '成功');
    if (status === 'completed') return badge('completed', '已推送完成');
    if (status === 'failed') return badge('failed', '最终失败');
    if (status === 'retrying') return badge('warning', '正在退避重试');
    return badge('running', '推送中');
  }
  function retryText(row) {
    if (row.status === 'retrying') return '正在退避，第 ' + fmtNumber((row.retry_count || 0) + 1) + ' 次尝试';
    if (row.status === 'failed') return row.retryable ? '可重试失败' : '不可重试失败';
    return row.retry_count ? '重试 ' + fmtNumber(row.retry_count) + ' 次后成功' : '首次成功';
  }
  function categorySuggestion(category) {
    var map = {
      auth: '核对平台签发的 source Token 与源登记状态',
      network: '检查中间机至平台的网络、DNS 与防火墙',
      platform_unavailable: '检查 ingest 服务及平台负载后重试',
      rate_limit: '等待退避完成，必要时降低抽取速率',
      generation_conflict: '确认没有第二台 connector 同时推送同一 source',
      stale_generation_rejected: '旧 generation 已被平台拒绝；重新启动一轮同步',
      generation_barrier_incomplete: '检查缺失表/失败批次后重跑本 generation',
      generation_heartbeat_failed: '检查长任务期间网络稳定性并查看 generation 事件',
      generation_heartbeat_rejected: '平台已关闭该 generation；不要继续发送旧批次',
      request_validation: '核对协议版本、表结构与请求字段'
    };
    return map[category] || '查看推送详情和日志后处理';
  }
  function populateSelect(id, values, label) {
    var select = byId(id); var current = select.value;
    select.innerHTML = '<option value="">' + label + '</option>' + Object.keys(values).sort().map(function (value) {
      return '<option value="' + esc(value) + '"' + (value === current ? ' selected' : '') + '>' + esc(value) + '</option>';
    }).join('');
  }
  function switchPanel(panelId) {
    document.querySelectorAll('[role="tab"][data-panel]').forEach(function (tab) {
      var active = tab.dataset.panel === panelId;
      tab.classList.toggle('active', active); tab.setAttribute('aria-selected', active ? 'true' : 'false'); tab.tabIndex = active ? 0 : -1;
    });
    ['push-summary','push-detail'].forEach(function (id) { byId(id).hidden = id !== panelId; });
  }

  async function loadSummaries() {
    renderState('summary-list', 'loading');
    try {
      var data = await apiJson('/api/push-logs/by-table');
      summaries = data.tables || [];
      summaries.forEach(function (row) { knownSources[row.source] = true; knownTables[row.table_name] = true; });
      populateSelect('summary-source', knownSources, '全部源'); populateSelect('detail-source', knownSources, '全部源'); populateSelect('detail-table', knownTables, '全部表/事件');
      renderSummaryPage();
      byId('summary-refreshed').textContent = '最近刷新：' + fmtTime(new Date().toISOString());
    } catch (error) { renderState('summary-list', 'error', { message: error.message, retry: loadSummaries }); }
  }

  function renderSummaryPage() {
    var status = byId('summary-status').value;
    var source = byId('summary-source').value;
    var keyword = byId('summary-name').value.trim().toLowerCase();
    var filtered = summaries.filter(function (row) {
      return (!status || row.status === status) && (!source || row.source === source) && (!keyword || row.table_name.toLowerCase().indexOf(keyword) >= 0);
    });
    var limit = Number(byId('summary-limit').value); var pages = Math.max(1, Math.ceil(filtered.length / limit)); summaryPage = Math.min(summaryPage, pages);
    var pageRows = filtered.slice((summaryPage - 1) * limit, summaryPage * limit);
    byId('summary-page-info').textContent = '第 ' + summaryPage + ' / ' + pages + ' 页 · ' + fmtNumber(filtered.length) + ' 张表';
    byId('summary-prev').disabled = summaryPage <= 1; byId('summary-next').disabled = summaryPage >= pages;
    if (!pageRows.length) { renderState('summary-list', 'empty', { message: '没有匹配的推送表。' }); return; }
    var rows = pageRows.map(function (row) {
      var category = row.error_category || '—';
      return '<tr><td>' + esc(row.source) + '</td><td><strong>' + esc(row.table_name) + '</strong></td><td>' + esc(row.mode || '—') + '</td><td>' + pushStatus(row.status) + '</td><td>' + fmtNumber(row.rows || 0) + '</td><td>' + fmtNumber(row.steps_ok || 0) + ' / ' + fmtNumber(row.steps_failed || 0) + '</td><td>' + fmtDuration((row.duration_ms || 0) / 1000) + '</td>' +
        '<td><code title="' + esc(row.generation_id || '') + '">' + esc(row.generation_id ? row.generation_id.slice(0, 12) + '…' : '—') + '</code></td><td>' + fmtNumber(row.retry_count || 0) + '</td><td title="' + esc(categorySuggestion(category)) + '">' + esc(category) + '</td><td>' + fmtTime(row.last_at) + '</td>' +
        '<td><button type="button" class="btn-ghost table-detail-button" data-source="' + esc(row.source) + '" data-table="' + esc(row.table_name) + '">查看明细</button></td></tr>';
    }).join('');
    byId('summary-list').dataset.state = 'success';
    byId('summary-list').innerHTML = '<div class="table-scroll"><table class="data"><thead><tr><th>源</th><th>表</th><th>模式</th><th>状态</th><th>行数</th><th>成功/失败步骤</th><th>耗时</th><th>generation</th><th>重试</th><th>错误分类</th><th>最近推送</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  async function loadDetails() {
    renderState('detail-list', 'loading');
    var limit = Number(byId('detail-limit').value);
    var query = new URLSearchParams({ limit: String(limit), offset: String((detailPage - 1) * limit) });
    if (byId('detail-source').value) query.set('source', byId('detail-source').value);
    if (byId('detail-table').value) query.set('table', byId('detail-table').value);
    try {
      var data = await apiJson('/api/push-logs?' + query.toString());
      (data.push_logs || []).forEach(function (row) { knownSources[row.source] = true; if (row.table_name !== '*') knownTables[row.table_name] = true; });
      renderDetails(data.push_logs || []);
      var pages = Math.max(1, Math.ceil((data.total || 0) / limit)); detailPage = Math.min(detailPage, pages);
      byId('detail-page-info').textContent = '第 ' + detailPage + ' / ' + pages + ' 页 · ' + fmtNumber(data.total || 0) + ' 条';
      byId('detail-prev').disabled = detailPage <= 1; byId('detail-next').disabled = detailPage >= pages;
      byId('detail-refreshed').textContent = '最近刷新：' + fmtTime(new Date().toISOString());
    } catch (error) { renderState('detail-list', 'error', { message: error.message, retry: loadDetails }); }
  }

  function renderDetails(rows) {
    if (!rows.length) { renderState('detail-list', 'empty', { message: '没有推送记录（仅 HTTP sink 会记录）。' }); return; }
    var html = rows.map(function (row) {
      var canOpen = row.batch_id && row.table_name !== '*';
      return '<tr><td>#' + fmtNumber(row.id) + '</td><td>' + esc(row.source) + '</td><td>' + esc(row.table_name) + '</td><td>' + esc(row.step_kind) + '</td><td>' + pushStatus(row.status) + '</td><td>' + esc(retryText(row)) + '</td>' +
        '<td>' + esc(row.error_category || '—') + (row.error_category ? '<br><span class="meta">' + esc(categorySuggestion(row.error_category)) + '</span>' : '') + '</td><td>' + (row.retryable == null ? '—' : row.retryable ? '是' : '否') + '</td>' +
        '<td>' + (row.receipt_received == null ? '—' : row.receipt_received ? '已收到' : '未收到') + '</td><td>' + (row.idempotent_replay ? '是' : '否') + '</td><td><code title="' + esc(row.generation_id || '') + '">' + esc(row.generation_id ? row.generation_id.slice(0, 12) + '…' : '—') + '</code></td><td>' + fmtNumber(row.rows_count) + '</td><td>' + fmtDuration((row.duration_ms || 0) / 1000) + '</td><td>' + fmtTime(row.created_at) + '</td>' +
        '<td>' + (canOpen ? '<button type="button" class="btn-ghost batch-detail-button" data-batch-id="' + esc(row.batch_id) + '">批次</button>' : '—') + '</td></tr>' +
        (row.error_detail ? '<tr><td></td><td colspan="14" class="long-cell"><strong>错误：</strong>' + esc(row.error_detail) + '</td></tr>' : '');
    }).join('');
    byId('detail-list').dataset.state = 'success';
    byId('detail-list').innerHTML = '<div class="table-scroll"><table class="data"><thead><tr><th>ID</th><th>源</th><th>表</th><th>步骤</th><th>状态</th><th>attempt</th><th>错误分类/建议</th><th>可重试</th><th>回执</th><th>幂等命中</th><th>generation</th><th>行数</th><th>耗时</th><th>时间</th><th>操作</th></tr></thead><tbody>' + html + '</tbody></table></div>';
  }

  async function openBatch(batchId) {
    byId('push-detail-title').textContent = '批次 ' + batchId;
    renderState('push-detail-body', 'loading'); openModal('push-detail-overlay');
    try {
      var data = await apiJson('/api/push-logs/batch/' + encodeURIComponent(batchId));
      var progress = data.progress || {};
      var cards = '<dl class="push-stat-grid"><div class="push-stat"><dt>源 / 表</dt><dd>' + esc(data.source) + ' / ' + esc(data.table_name) + '</dd></div><div class="push-stat"><dt>模式</dt><dd>' + esc(data.mode || '—') + '</dd></div><div class="push-stat"><dt>成功 / 失败</dt><dd>' + fmtNumber(progress.ok || 0) + ' / ' + fmtNumber(progress.failed || 0) + '</dd></div><div class="push-stat"><dt>写入行数</dt><dd>' + fmtNumber(progress.rows || 0) + '</dd></div><div class="push-stat"><dt>状态</dt><dd>' + (progress.completed ? '平台已确认完成' : '未跨越表完成屏障') + '</dd></div></dl>';
      var rows = (data.steps || []).map(function (step) { return '<tr><td>' + esc(step.step_kind) + '</td><td>' + pushStatus(step.status) + '</td><td>' + fmtNumber(step.retry_count || 0) + '</td><td>' + (step.receipt_received ? '已收到' : '—') + '</td><td>' + (step.idempotent_replay ? '是' : '否') + '</td><td>' + esc(step.error_category || '—') + '</td><td>' + fmtNumber(step.rows_count) + '</td><td>' + fmtTime(step.created_at) + '</td><td class="long-cell">' + esc(step.error_detail || '—') + '</td></tr>'; }).join('');
      byId('push-detail-body').dataset.state = 'success'; byId('push-detail-body').innerHTML = cards + '<div class="table-scroll"><table class="data"><thead><tr><th>步骤</th><th>状态</th><th>重试</th><th>回执</th><th>幂等</th><th>错误分类</th><th>行数</th><th>时间</th><th>详情</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
    } catch (error) { renderState('push-detail-body', 'error', { message: error.message, retry: function () { openBatch(batchId); } }); }
  }

  function filterTable(source, table) {
    byId('detail-source').value = source; byId('detail-table').value = table; detailPage = 1; switchPanel('push-detail'); loadDetails();
  }
  function init() {
    document.querySelectorAll('[role="tab"][data-panel]').forEach(function (tab) { tab.addEventListener('click', function () { switchPanel(tab.dataset.panel); }); }); initTabs(document);
    ['summary-status','summary-source','summary-limit'].forEach(function (id) { byId(id).addEventListener('change', function () { summaryPage = 1; renderSummaryPage(); }); });
    byId('summary-name').addEventListener('input', function () { summaryPage = 1; renderSummaryPage(); }); byId('summary-refresh').addEventListener('click', loadSummaries);
    byId('summary-prev').addEventListener('click', function () { summaryPage = Math.max(1, summaryPage - 1); renderSummaryPage(); }); byId('summary-next').addEventListener('click', function () { summaryPage += 1; renderSummaryPage(); });
    ['detail-source','detail-table','detail-limit'].forEach(function (id) { byId(id).addEventListener('change', function () { detailPage = 1; loadDetails(); }); }); byId('detail-refresh').addEventListener('click', loadDetails);
    byId('detail-prev').addEventListener('click', function () { detailPage = Math.max(1, detailPage - 1); loadDetails(); }); byId('detail-next').addEventListener('click', function () { detailPage += 1; loadDetails(); });
    byId('summary-list').addEventListener('click', function (event) { var button = event.target.closest('.table-detail-button'); if (button) filterTable(button.dataset.source, button.dataset.table); });
    byId('detail-list').addEventListener('click', function (event) { var button = event.target.closest('.batch-detail-button'); if (button) openBatch(button.dataset.batchId); });
    byId('push-detail-close').addEventListener('click', function () { closeModal('push-detail-overlay'); }); byId('push-detail-overlay').addEventListener('click', function (event) { if (event.target === this) closeModal(this); });
    var query = new URLSearchParams(location.search); if (query.get('source')) byId('detail-source').innerHTML += '<option selected value="' + esc(query.get('source')) + '">' + esc(query.get('source')) + '</option>'; if (query.get('table')) byId('detail-table').innerHTML += '<option selected value="' + esc(query.get('table')) + '">' + esc(query.get('table')) + '</option>';
    loadSummaries(); loadDetails();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
