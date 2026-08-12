(function () {
  'use strict';
  var page = 1;
  var activeRunId = null;
  var pollTimer = null;
  var pollFailures = 0;
  var knownSources = {};

  function byId(id) { return document.getElementById(id); }
  function duration(run) {
    if (!run.started_at) return '—';
    var end = run.finished_at ? new Date(run.finished_at) : new Date();
    return fmtDuration(Math.max(0, (end - new Date(run.started_at)) / 1000));
  }
  function runType(value) { return value === 'reconcile' ? '对账' : value === 'sync' ? '同步' : value || '未知'; }

  function renderRuns(data) {
    if (!data.runs || !data.runs.length) { renderState('runs-list', 'empty', { message: '当前筛选条件下没有运行记录。' }); return; }
    var rows = data.runs.map(function (run) {
      knownSources[run.source] = true;
      return '<tr><td>#' + fmtNumber(run.id) + '</td><td>' + esc(run.source) + '</td><td>' + esc(runType(run.run_type)) + '</td><td>' + badge(run.status) + '</td>' +
        '<td>' + fmtTime(run.started_at) + '</td><td>' + fmtNumber(run.tables || 0) + '</td><td>' + fmtNumber(run.rows || 0) + '</td><td>' + esc(duration(run)) + '</td>' +
        '<td><code title="' + esc(run.generation_id || '') + '">' + esc(run.generation_id ? run.generation_id.slice(0, 12) + '…' : '—') + '</code></td>' +
        '<td><button type="button" class="btn-ghost run-detail-button" data-run-id="' + run.id + '">详情</button></td></tr>';
    }).join('');
    byId('runs-list').dataset.state = 'success';
    byId('runs-list').innerHTML = '<div class="table-scroll"><table class="data"><thead><tr><th>ID</th><th>源</th><th>类型</th><th>状态</th><th>开始</th><th>表数</th><th>行数</th><th>耗时</th><th>generation</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
    populateSources();
  }

  function populateSources() {
    var select = byId('source-filter');
    var current = select.value;
    select.innerHTML = '<option value="">全部源</option>' + Object.keys(knownSources).sort().map(function (source) {
      return '<option value="' + esc(source) + '"' + (source === current ? ' selected' : '') + '>' + esc(source) + '</option>';
    }).join('');
  }

  function updatePager(total, limit) {
    var pages = Math.max(1, Math.ceil(total / limit));
    page = Math.min(page, pages);
    byId('page-info').textContent = '第 ' + page + ' / ' + pages + ' 页 · 共 ' + fmtNumber(total) + ' 条';
    byId('prev-page').disabled = page <= 1;
    byId('next-page').disabled = page >= pages;
  }

  async function loadRuns() {
    renderState('runs-list', 'loading');
    var limit = Number(byId('limit-select').value);
    var query = new URLSearchParams({ limit: String(limit), offset: String((page - 1) * limit) });
    if (byId('source-filter').value) query.set('source', byId('source-filter').value);
    if (byId('run-type-filter').value) query.set('run_type', byId('run-type-filter').value);
    try {
      var data = await apiJson('/api/runs?' + query.toString());
      renderRuns(data);
      updatePager(data.total || 0, limit);
      byId('runs-refreshed').textContent = '最近刷新：' + fmtTime(new Date().toISOString());
    } catch (error) {
      renderState('runs-list', 'error', { message: error.message, retry: loadRuns });
    }
  }

  function renderProgress(step) {
    if (step.expected_rows == null) return '<span>' + fmtNumber(step.rows_out) + ' 行</span>';
    var done = Number(step.rows_out || 0);
    var expected = Number(step.expected_rows || 0);
    var percent = expected ? Math.min(100, Math.round(done / expected * 100)) : (step.status === 'ok' ? 100 : 0);
    return '<div class="progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + percent + '"><div class="progress-bar-fill" style="width:' + percent + '%"></div></div><span>' + fmtNumber(done) + ' / ' + fmtNumber(expected) + '（' + percent + '%）</span>';
  }

  function renderGeneration(generation) {
    if (!generation || !generation.generation_id) return '<section><h3>generation 屏障</h3><p class="meta">此运行没有 generation（可能是本地开发模式或旧记录）。</p></section>';
    var events = generation.events || [];
    var rows = events.map(function (event) {
      return '<tr><td>' + esc(event.step_kind) + '</td><td>' + badge(event.status) + '</td><td>' + fmtTime(event.created_at) + '</td><td>' + esc(event.error_category || '—') + '</td><td>' + (event.retryable == null ? '—' : event.retryable ? '可重试' : '不可重试') + '</td><td>' + fmtNumber(event.retry_count || 0) + '</td><td class="long-cell">' + esc(event.error_detail || '—') + '</td></tr>';
    }).join('');
    return '<section><h3>generation 屏障</h3><p class="meta">ID：<code>' + esc(generation.generation_id) + '</code>。平台只有在同一 generation 的计划表全部确认后才跨越屏障。</p>' +
      (rows ? '<div class="table-scroll"><table class="data"><thead><tr><th>事件</th><th>状态</th><th>时间</th><th>错误分类</th><th>重试性</th><th>重试</th><th>详情</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<p class="meta">暂无 generation 事件。</p>') + '</section>';
  }

  function renderSteps(steps, runTypeValue) {
    if (!steps || !steps.length) return '<p class="meta">没有逐表步骤记录。</p>';
    var rows = steps.map(function (step, index) {
      var reconcile = runTypeValue === 'reconcile' ? '<br><span class="meta">修复 ' + fmtNumber(step.repaired || 0) + '，软删除 ' + fmtNumber(step.soft_deleted || 0) + '</span>' : '';
      var watermarks = '<details><summary>水位</summary><div class="long-cell"><code>' + esc(step.watermark_before || '—') + '</code> → <code>' + esc(step.watermark_after || '—') + '</code></div></details>';
      return '<tr><td>' + fmtNumber(step.ordinal || index + 1) + '</td><td>' + esc(step.target) + '</td><td>' + badge(step.status) + reconcile + '</td><td>' + renderProgress(step) + '</td><td>' + fmtNumber(step.batches) + '</td><td>' + fmtTime(step.progressed_at || step.finished_at || step.started_at) + '</td><td>' + watermarks + '</td><td class="long-cell">' + esc(step.error || '—') + '</td></tr>';
    }).join('');
    return '<div class="table-scroll"><table class="data"><thead><tr><th>#</th><th>表/目标</th><th>状态</th><th>进度</th><th>批次</th><th>最近进度</th><th>水位</th><th>错误</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  function renderDetail(data) {
    var run = data.run || {};
    byId('run-detail-body').dataset.state = 'success';
    byId('run-detail-body').innerHTML = '<div class="src-kv"><span>源 <b>' + esc(run.source) + '</b></span><span>类型 <b>' + esc(runType(run.run_type)) + '</b></span><span>状态 ' + badge(run.status) + '</span><span>表数 <b>' + fmtNumber(run.tables || 0) + '</b></span><span>行数 <b>' + fmtNumber(run.rows || 0) + '</b></span><span>开始 <b>' + fmtTime(run.started_at) + '</b></span><span>耗时 <b>' + esc(duration(run)) + '</b></span></div>' +
      (run.detail ? '<p class="warn long-cell">' + esc(run.detail) + ' · <a href="/logs?service=connector">查看连接器日志</a></p>' : '') + renderGeneration(data.generation) + '<section><h3>逐表步骤</h3>' + renderSteps(data.steps || [], run.run_type) + '</section>';
    return run.status;
  }

  async function refreshDetail() {
    if (!activeRunId) return;
    try {
      var data = await apiJson('/api/runs/' + activeRunId, { timeoutMs: 10000 });
      pollFailures = 0;
      var status = renderDetail(data);
      if (status === 'running') schedulePoll();
      else { stopPolling(false); loadRuns(); }
    } catch (error) {
      pollFailures += 1;
      renderState('run-detail-body', 'error', { message: error.message, retry: refreshDetail });
      schedulePoll();
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!activeRunId) return;
    var base = document.hidden ? 10000 : 2000;
    pollTimer = setTimeout(refreshDetail, Math.min(30000, base * Math.pow(2, Math.min(4, pollFailures))));
  }
  function stopPolling(clearActive) {
    clearTimeout(pollTimer); pollTimer = null;
    if (clearActive !== false) activeRunId = null;
  }
  function openDetail(runId) {
    activeRunId = Number(runId); pollFailures = 0;
    byId('run-detail-title').textContent = '运行 #' + activeRunId;
    renderState('run-detail-body', 'loading');
    openModal('run-detail-overlay');
    refreshDetail();
  }
  function closeDetail() { stopPolling(); closeModal('run-detail-overlay'); }

  function init() {
    ['source-filter','run-type-filter','limit-select'].forEach(function (id) { byId(id).addEventListener('change', function () { page = 1; loadRuns(); }); });
    byId('runs-refresh').addEventListener('click', loadRuns);
    byId('prev-page').addEventListener('click', function () { page = Math.max(1, page - 1); loadRuns(); });
    byId('next-page').addEventListener('click', function () { page += 1; loadRuns(); });
    byId('runs-list').addEventListener('click', function (event) { var button = event.target.closest('.run-detail-button'); if (button) openDetail(button.dataset.runId); });
    byId('run-detail-close').addEventListener('click', closeDetail);
    byId('run-detail-overlay').addEventListener('click', function (event) { if (event.target === this) closeDetail(); });
    document.addEventListener('visibilitychange', function () { if (activeRunId) schedulePoll(); });
    loadRuns();
    var watched = new URLSearchParams(location.search).get('watch');
    if (watched && /^\d+$/.test(watched)) openDetail(Number(watched));
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
