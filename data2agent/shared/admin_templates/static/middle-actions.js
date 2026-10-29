/* 运行页顶栏的手动操作:同步 / L1 对账 / 深度对账。状态页只读观测;
   提交后在本页运行列表与详情中跟踪(d2a:run-triggered)。 */
(function () {
  'use strict';

  var sinks = {};

  function byId(id) { return document.getElementById(id); }

  function showResult(text) {
    var result = byId('action-result');
    result.textContent = text;
    result.hidden = !text;
  }

  /* 推送方式不支持对账(如 Webhook)时禁用对账按钮并说明原因 */
  function syncReconcileButtons() {
    var sink = sinks[byId('reconcile-source').value] || {};
    var unsupported = sink.supports_reconcile === false;
    var reason = unsupported ? (sink.reconcile_unsupported_reason || '该推送方式不支持对账') : '';
    ['btn-reconcile', 'btn-reconcile-deep'].forEach(function (id) {
      var button = byId(id);
      button.disabled = unsupported;
      button.title = reason;
    });
    showResult(unsupported ? '对账不可用：' + reason : '');
  }

  async function loadSources() {
    try {
      var data = await apiJson('/api/status', { timeoutMs: 10000 });
      var sel = byId('reconcile-source');
      sel.innerHTML = (data.sources || []).map(function (source) {
        return '<option value="' + esc(source.source) + '">' + esc(source.source) + '</option>';
      }).join('');
      if (!(data.sources || []).length) {
        showResult('尚未配置数据源，请先到「配置」完成首次配置。');
      }
    } catch (error) {
      showResult(error.message);
    }
    try {
      var config = await apiJson('/api/config', { timeoutMs: 10000 });
      Object.keys(config.sources || {}).forEach(function (name) {
        sinks[name] = (config.sources[name] || {}).sink || {};
      });
    } catch (_error) { /* 读不到配置时不预先禁用,由后端拒绝并提示 */ }
    syncReconcileButtons();
  }

  async function trigger(action, button) {
    var source = byId('reconcile-source').value || null;
    if (!source) { showResult('请先选择数据源'); return; }
    if (action === 'sync' &&
        !await confirmDialog('立即同步会访问 ' + source + ' 的配置表并推送到平台。', { title: '确认立即同步', okLabel: '启动同步' })) return;
    if (action === 'reconcile' &&
        !await confirmDialog('L1 对账会比对 ' + source + ' 的平台与 ERP 行数及校验和。', { title: '确认 L1 对账', okLabel: '启动对账' })) return;
    if (action === 'reconcile_deep' &&
        !await confirmDialog('深度对账会重读 ' + source + ' 的全部配置表并在平台执行修复（软删平台多余行），耗时较长，建议低峰执行。',
          { title: '确认深度对账', okLabel: '启动深度对账', danger: true, confirmText: source })) return;
    try {
      var body = await runAction(button, function () {
        return apiJson('/api/actions/trigger', {
          method: 'POST', body: JSON.stringify({ action: action, source: source }), timeoutMs: 15000
        });
      }, { busyLabel: '提交中…' });
      if (body && body.run_id) {
        showResult('');
        announce('success', '已提交运行 #' + body.run_id);
        document.dispatchEvent(new CustomEvent('d2a:run-triggered', { detail: { runId: body.run_id } }));
      } else {
        showResult(body.note || body.message || '请求已完成');
        announce('success', byId('action-result').textContent);
      }
    } catch (error) { showResult(error.message); }
  }

  byId('reconcile-source').addEventListener('change', syncReconcileButtons);
  byId('btn-sync').addEventListener('click', function () { trigger('sync', this); });
  byId('btn-reconcile').addEventListener('click', function () { trigger('reconcile', this); });
  byId('btn-reconcile-deep').addEventListener('click', function () { trigger('reconcile_deep', this); });

  loadSources();
})();
