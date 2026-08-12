(function () {
  'use strict';
  function byId(id) { return document.getElementById(id); }
  async function loadLogs() {
    var output = byId('log-output');
    output.textContent = '正在加载…'; output.setAttribute('aria-busy', 'true');
    var query = new URLSearchParams({ service: byId('log-service').value, lines: byId('log-lines').value || '200' });
    if (byId('log-level').value.trim()) query.set('level', byId('log-level').value.trim());
    try {
      var body = await apiJson('/api/logs?' + query.toString());
      output.textContent = body.ok ? (body.text || '（空）') : formatApiError(body, '读取失败');
      byId('log-refreshed').textContent = '最近成功刷新：' + fmtTime(new Date().toISOString());
    } catch (error) { output.textContent = error.message + '\n建议：检查管理进程、登录状态和日志目录权限。'; }
    finally { output.removeAttribute('aria-busy'); }
  }
  function init() {
    var query = new URLSearchParams(location.search);
    if (query.get('service') && byId('log-service').querySelector('option[value="' + CSS.escape(query.get('service')) + '"]')) byId('log-service').value = query.get('service');
    byId('log-refresh').addEventListener('click', loadLogs); byId('log-service').addEventListener('change', loadLogs);
    byId('log-copy').addEventListener('click', function () { copyText(byId('log-output').textContent); }); loadLogs();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
