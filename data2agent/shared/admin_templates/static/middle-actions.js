/* 中间机操作页:手动触发 同步 / L1对账 / 深度对账。动作集中于此,
   状态页只读观测;所有运行记录统一在「运行」页查看。 */
(function () {
  'use strict';

  function byId(id) { return document.getElementById(id); }

  async function loadSources() {
    try {
      var data = await apiJson('/api/status', { timeoutMs: 10000 });
      var sel = byId('reconcile-source');
      sel.innerHTML = (data.sources || []).map(function (source) {
        return '<option value="' + esc(source.source) + '">' + esc(source.source) + '</option>';
      }).join('');
      if (!(data.sources || []).length) {
        byId('action-result').textContent = '尚未配置数据源，请先到「连接」页完成首次配置。';
      }
    } catch (error) {
      byId('action-result').textContent = error.message;
    }
  }

  async function trigger(action, button) {
    var source = byId('reconcile-source').value || null;
    var result = byId('action-result');
    if (!source) { result.textContent = '请先选择数据源'; return; }
    if (action === 'sync' &&
        !await confirmDialog('立即同步会访问所选源 ' + source + ' 的配置表并推送到平台,确认启动?', { okLabel: '启动同步' })) return;
    if (action === 'reconcile_deep' &&
        !await confirmDialog('深度对账会重读所选源 ' + source + ' 的全部配置表并在平台执行修复,建议低峰执行。确认现在启动?', { okLabel: '启动深度对账' })) return;
    try {
      var body = await runAction(button, function () {
        return apiJson('/api/actions/trigger', {
          method: 'POST', body: JSON.stringify({ action: action, source: source }), timeoutMs: 15000
        });
      }, { busyLabel: '提交中…' });
      var type = action === 'sync' ? 'sync' : 'reconcile';
      if (body && body.run_id) {
        result.innerHTML = '已提交 — <a href="/runs?type=' + type + '&watch=' + body.run_id + '">在「运行」页查看 #' + body.run_id + '</a>';
      } else {
        result.textContent = body.note || body.message || '请求已完成';
      }
      announce('success', result.textContent || '动作已提交');
    } catch (error) { result.textContent = error.message; }
  }

  byId('btn-sync').addEventListener('click', function () { trigger('sync', this); });
  byId('btn-reconcile').addEventListener('click', function () { trigger('reconcile', this); });
  byId('btn-reconcile-deep').addEventListener('click', function () { trigger('reconcile_deep', this); });

  loadSources();
})();
