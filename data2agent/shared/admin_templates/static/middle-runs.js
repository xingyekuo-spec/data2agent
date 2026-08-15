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

  /* generation 屏障:默认折叠;无 generation 不渲染占位(减少噪音)。 */
  function renderGeneration(generation) {
    if (!generation || !generation.generation_id) return '';
    var events = generation.events || [];
    var rows = events.map(function (event) {
      return '<tr><td>' + esc(event.step_kind) + '</td><td>' + badge(event.status) + '</td><td>' + fmtTime(event.created_at) + '</td><td>' + esc(event.error_category || '—') + '</td><td>' + (event.retryable == null ? '—' : event.retryable ? '可重试' : '不可重试') + '</td><td>' + fmtNumber(event.retry_count || 0) + '</td><td class="long-cell">' + esc(event.error_detail || '—') + '</td></tr>';
    }).join('');
    return '<details class="gen"><summary>generation 屏障（' + events.length + ' 事件）</summary>' +
      '<p class="meta">ID：<code>' + esc(generation.generation_id) + '</code>。平台只有在同一 generation 的计划表全部确认后才跨越屏障。</p>' +
      (rows ? '<div class="table-scroll"><table class="data"><thead><tr><th>事件</th><th>状态</th><th>时间</th><th>错误分类</th><th>重试性</th><th>重试</th><th>详情</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<p class="meta">暂无 generation 事件。</p>') + '</details>';
  }

  /* 逐表步骤:主行精简(表/状态/进度/耗时),错误/水位/对账计数收进可展开详情行。
     失败行整行高亮并默认展开,便于排障定位。 */
  function renderSteps(steps, runTypeValue) {
    if (!steps || !steps.length) return '<p class="meta">没有逐表步骤记录。</p>';
    var rows = steps.map(function (step, index) {
      var failed = step.status === 'failed' || step.status === 'error';
      var dur = step.started_at
        ? fmtDuration(Math.max(0, ((step.finished_at ? new Date(step.finished_at) : new Date()) - new Date(step.started_at)) / 1000))
        : '—';
      var detailBits = [];
      if (step.error) detailBits.push('<div><dt>错误</dt><dd class="step-err">' + esc(step.error) +
        ' · <a href="/logs?service=connector&level=ERROR">查连接器错误日志</a></dd></div>');
      detailBits.push('<div><dt>水位</dt><dd><code>' + esc(step.watermark_before || '—') + '</code> → <code>' + esc(step.watermark_after || '—') + '</code></dd></div>');
      detailBits.push('<div><dt>批次</dt><dd>' + fmtNumber(step.batches || 0) + '</dd></div>');
      if (runTypeValue === 'reconcile') detailBits.push('<div><dt>对账</dt><dd>修复 ' + fmtNumber(step.repaired || 0) + ' · 软删除 ' + fmtNumber(step.soft_deleted || 0) + '</dd></div>');
      detailBits.push('<div><dt>最近进度</dt><dd>' + fmtTime(step.progressed_at || step.finished_at || step.started_at) + '</dd></div>');
      var main = '<tr class="step-row' + (failed ? ' step-failed' : '') + '" data-step-idx="' + index + '" title="点击展开/收起详情">' +
        '<td><span class="step-toggle">' + (failed ? '▾' : '▸') + '</span>' + fmtNumber(step.ordinal || index + 1) + '</td>' +
        '<td>' + esc(step.target) + '</td><td>' + badge(step.status) + '</td><td>' + renderProgress(step) + '</td><td>' + esc(dur) + '</td></tr>';
      var detail = '<tr class="step-detail" data-step-detail="' + index + '"' + (failed ? '' : ' hidden') + '><td colspan="5"><dl class="step-detail-grid">' + detailBits.join('') + '</dl></td></tr>';
      return main + detail;
    }).join('');
    return '<div class="table-scroll"><table class="data"><thead><tr><th>#</th><th>表/目标</th><th>状态</th><th>进度</th><th>耗时</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  /* 失败摘要 + 整体进度:排障时先看到"坏了几张表",一键滚动定位。 */
  function renderSummary(steps, run) {
    var total = steps.length;
    var failed = steps.filter(function (s) { return s.status === 'failed' || s.status === 'error'; });
    var done = steps.filter(function (s) { return s.status === 'ok' || s.status === 'completed'; }).length;
    var pct = total ? Math.round(done / total * 100) : 0;
    var overall = '<div class="rd-overall"><div class="progress-bar"><div class="progress-bar-fill" style="width:' + pct + '%"></div></div>' +
      '<div class="meta">完成 ' + done + ' / ' + total + ' 表（' + pct + '%）· 行数 ' + fmtNumber(run.rows || 0) + '</div></div>';
    var fail;
    if (failed.length) {
      fail = '<div class="rd-fail-summary"><strong>失败 ' + failed.length + ' / ' + total + ' 表</strong>' +
        '<span>' + failed.map(function (s, i) {
          var idx = steps.indexOf(s);
          return '<button type="button" data-goto-step="' + idx + '">' + esc(s.target) + '</button>';
        }).join('、') + '</span></div>';
    } else if (total) {
      fail = '<div class="rd-fail-summary ok">全部 ' + total + ' 张表就绪</div>';
    } else { fail = ''; }
    return fail + overall;
  }

  function renderDetail(data) {
    var run = data.run || {};
    var steps = data.steps || [];
    byId('run-detail-body').dataset.state = 'success';
    byId('run-detail-status').innerHTML = badge(run.status) + ' <span class="meta">' + esc(runType(run.run_type)) + ' · ' + esc(run.source) + '</span>';
    byId('run-detail-body').innerHTML =
      renderSummary(steps, run) +
      '<div class="src-kv"><span>表数 <b>' + fmtNumber(run.tables || 0) + '</b></span><span>行数 <b>' + fmtNumber(run.rows || 0) + '</b></span><span>开始 <b>' + fmtTime(run.started_at) + '</b></span><span>耗时 <b>' + esc(duration(run)) + '</b></span></div>' +
      (run.detail ? '<p class="warn long-cell">' + esc(run.detail) + ' · <a href="/logs?service=connector">查看连接器日志</a></p>' : '') +
      '<section><h3>逐表步骤</h3>' + renderSteps(steps, run.run_type) + '</section>' +
      renderGeneration(data.generation);
    return run.status;
  }

  async function refreshDetail() {
    if (!activeRunId) return;
    try {
      var data = await apiJson('/api/runs/' + activeRunId, { timeoutMs: 10000 });
      pollFailures = 0;
      var status = renderDetail(data);
      var pollNote = byId('run-detail-poll');
      if (status === 'running') {
        pollNote.textContent = '运行中 · 自动刷新 · 上次 ' + fmtTime(new Date().toISOString());
        schedulePoll();
      } else {
        pollNote.textContent = '已结束 · 更新于 ' + fmtTime(new Date().toISOString());
        stopPolling(false); loadRuns();
      }
    } catch (error) {
      pollFailures += 1;
      byId('run-detail-poll').textContent = '刷新失败,重试中…';
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
    byId('run-detail-status').textContent = '';
    byId('run-detail-poll').textContent = '';
    renderState('run-detail-body', 'loading');
    openModal('run-detail-overlay');
    refreshDetail();
  }
  function closeDetail() { stopPolling(); closeModal('run-detail-overlay'); }

  /* 详情体事件委托:行展开/收起 + 失败表滚动定位。 */
  function bindDetailInteractions() {
    byId('run-detail-body').addEventListener('click', function (event) {
      var gotoBtn = event.target.closest('[data-goto-step]');
      if (gotoBtn) {
        var idx = gotoBtn.dataset.gotoStep;
        var detailRow = byId('run-detail-body').querySelector('[data-step-detail="' + idx + '"]');
        var mainRow = byId('run-detail-body').querySelector('[data-step-idx="' + idx + '"]');
        if (detailRow) detailRow.hidden = false;
        if (mainRow) {
          var tog = mainRow.querySelector('.step-toggle'); if (tog) tog.textContent = '▾';
          mainRow.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
        return;
      }
      var row = event.target.closest('tr.step-row');
      if (row) {
        var i = row.dataset.stepIdx;
        var det = byId('run-detail-body').querySelector('[data-step-detail="' + i + '"]');
        if (det) {
          det.hidden = !det.hidden;
          var t = row.querySelector('.step-toggle'); if (t) t.textContent = det.hidden ? '▸' : '▾';
        }
      }
    });
  }

  function init() {
    ['source-filter','run-type-filter','limit-select'].forEach(function (id) { byId(id).addEventListener('change', function () { page = 1; loadRuns(); }); });
    byId('runs-refresh').addEventListener('click', loadRuns);
    byId('prev-page').addEventListener('click', function () { page = Math.max(1, page - 1); loadRuns(); });
    byId('next-page').addEventListener('click', function () { page += 1; loadRuns(); });
    byId('runs-list').addEventListener('click', function (event) { var button = event.target.closest('.run-detail-button'); if (button) openDetail(button.dataset.runId); });
    byId('run-detail-close').addEventListener('click', closeDetail);
    byId('run-detail-overlay').addEventListener('click', function (event) { if (event.target === this) closeDetail(); });
    bindDetailInteractions();
    document.addEventListener('visibilitychange', function () { if (activeRunId) schedulePoll(); });
    var params = new URLSearchParams(location.search);
    var presetType = params.get('type');
    if (presetType && byId('run-type-filter').querySelector('option[value="' + presetType + '"]')) {
      byId('run-type-filter').value = presetType;
    }
    loadRuns();
    var watched = params.get('watch');
    if (watched && /^\d+$/.test(watched)) openDetail(Number(watched));
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
