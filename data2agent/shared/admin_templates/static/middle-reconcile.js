/* 中间机对账页:L1/深度对账触发 + 对账历史。 */
(function () {
  'use strict';

  function byId(id) { return document.getElementById(id); }

  async function loadSources() {
    try {
      var data = await apiJson('/api/status', { timeoutMs: 10000 });
      byId('reconcile-source').innerHTML = (data.sources || []).map(function (source) {
        return '<option value="' + esc(source.source) + '">' + esc(source.source) + '</option>';
      }).join('');
    } catch (error) {
      byId('action-result').textContent = error.message;
    }
  }

  async function loadHistory() {
    var panel = byId('reconcile-list');
    try {
      var data = await apiJson('/api/runs?run_type=reconcile&limit=20', { timeoutMs: 10000 });
      if (!(data.runs || []).length) {
        renderState(panel, 'empty', { message: '暂无对账记录' });
        return;
      }
      panel.innerHTML = '<div class="table-scroll"><table class="data"><thead><tr>' +
        '<th>ID</th><th>源</th><th>状态</th><th>开始</th><th>耗时</th><th>摘要</th><th>操作</th>' +
        '</tr></thead><tbody>' +
        data.runs.map(function (run) {
          var end = run.finished_at ? new Date(run.finished_at) : new Date();
          var seconds = run.started_at ? Math.max(0, (end - new Date(run.started_at)) / 1000) : null;
          var deep = run.detail && run.detail.indexOf('reconcile-deep') >= 0 ? '深度' : 'L1';
          return '<tr><td>#' + fmtNumber(run.id) + '</td><td>' + esc(run.source) + '</td><td>' +
            badge(run.status) + '</td><td>' + esc(fmtTime(run.started_at)) + '</td><td>' +
            (seconds == null ? '—' : esc(fmtDuration(seconds))) + '</td><td>' + esc(deep) + '</td>' +
            '<td><button type="button" class="btn-ghost detail-btn" data-run-id="' + run.id + '">详情</button></td></tr>';
        }).join('') + '</tbody></table></div>';
    } catch (error) {
      renderState(panel, 'error', { message: error.message, retry: loadHistory });
    }
  }

  async function triggerReconcile(action, button) {
    var source = byId('reconcile-source').value || null;
    var result = byId('action-result');
    if (!source) { result.textContent = '请先选择数据源'; return; }
    if (action === 'reconcile_deep' &&
        !await confirmDialog('深度对账会重读所选源 ' + source + ' 的全部配置表并在平台执行修复,建议低峰执行。确认现在启动?', { okLabel: '启动深度对账' })) return;
    try {
      var body = await runAction(button, function () {
        return apiJson('/api/actions/trigger', {
          method: 'POST', body: JSON.stringify({ action: action, source: source }), timeoutMs: 15000
        });
      }, { busyLabel: '提交中…' });
      if (body && body.run_id) {
        result.innerHTML = '已提交 — <a href="#" class="run-open" data-run-id="' + body.run_id + '">运行 #' + body.run_id + '</a>';
      } else {
        result.textContent = body.note || body.message || '请求已完成';
      }
      announce('success', result.textContent || '动作已提交');
      loadHistory();
    } catch (error) { result.textContent = error.message; }
  }

  byId('btn-reconcile').addEventListener('click', function () { triggerReconcile('reconcile', this); });
  byId('btn-reconcile-deep').addEventListener('click', function () { triggerReconcile('reconcile_deep', this); });
  byId('reconcile-refresh').addEventListener('click', loadHistory);

  /* 详情弹窗:页内查看,不跳转运行页 */
  function renderDetail(data) {
    var run = data.run || {};
    var kv = '<div class="src-kv"><span>源 <b>' + esc(run.source) + '</b></span><span>状态 ' + badge(run.status) + '</span>' +
      '<span>开始 <b>' + esc(fmtTime(run.started_at)) + '</b></span><span>结束 <b>' + esc(fmtTime(run.finished_at)) + '</b></span></div>';
    var detail = run.detail ? '<p class="warn long-cell">' + esc(run.detail) + '</p>' : '';
    var steps = (data.steps || []).map(function (step, index) {
      return '<tr><td>' + fmtNumber(step.ordinal || index + 1) + '</td><td>' + esc(step.target) + '</td><td>' +
        badge(step.status) + '</td><td>' + fmtNumber(step.batches || 0) + '</td><td class="long-cell">' +
        esc(step.error || '—') + '</td></tr>';
    }).join('');
    var stepsHtml = steps
      ? '<div class="table-scroll"><table class="data"><thead><tr><th>#</th><th>表/目标</th><th>状态</th><th>批次</th><th>错误</th></tr></thead><tbody>' + steps + '</tbody></table></div>'
      : '<p class="meta">无逐表步骤记录。</p>';
    byId('run-detail-body').innerHTML = kv + detail + '<h3>逐表步骤</h3>' + stepsHtml;
  }

  async function openDetail(runId) {
    byId('run-detail-title').textContent = '对账运行 #' + runId;
    renderState(byId('run-detail-body'), 'loading');
    openModal('run-detail-overlay');
    try {
      var data = await apiJson('/api/runs/' + runId, { timeoutMs: 10000 });
      renderDetail(data);
    } catch (error) {
      renderState(byId('run-detail-body'), 'error', {
        message: error.message,
        retry: function () { openDetail(runId); }
      });
    }
  }

  byId('reconcile-list').addEventListener('click', function (event) {
    var btn = event.target.closest('.detail-btn');
    if (btn) openDetail(btn.dataset.runId);
  });
  byId('action-result').addEventListener('click', function (event) {
    var link = event.target.closest('.run-open');
    if (link) { event.preventDefault(); openDetail(link.dataset.runId); }
  });
  byId('run-detail-close').addEventListener('click', function () { closeModal('run-detail-overlay'); });

  loadSources();
  loadHistory();
})();
