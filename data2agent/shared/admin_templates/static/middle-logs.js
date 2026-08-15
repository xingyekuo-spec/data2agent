(function () {
  'use strict';
  function byId(id) { return document.getElementById(id); }
  var autoTimer = null;

  async function loadLogs() {
    var output = byId('log-output');
    // 自动刷新时保留滚动位置体验:若原本在底部附近,刷新后继续贴底
    var stickToBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
    output.setAttribute('aria-busy', 'true');
    var query = new URLSearchParams({ service: byId('log-service').value, lines: byId('log-lines').value || '200' });
    if (byId('log-level').value.trim()) query.set('level', byId('log-level').value.trim());
    try {
      var body = await apiJson('/api/logs?' + query.toString());
      output.textContent = body.ok ? (body.text || '（空）') : formatApiError(body, '读取失败');
      if (stickToBottom) output.scrollTop = output.scrollHeight;
      byId('log-refreshed').textContent = '最近成功刷新：' + fmtTime(new Date().toISOString());
    } catch (error) { output.textContent = error.message + '\n建议：检查管理进程、登录状态和日志目录权限。'; }
    finally { output.removeAttribute('aria-busy'); }
  }
  function syncAuto() {
    clearInterval(autoTimer); autoTimer = null;
    if (byId('log-auto').checked) autoTimer = setInterval(function () { if (!document.hidden) loadLogs(); }, 5000);
  }
  function init() {
    var query = new URLSearchParams(location.search);
    if (query.get('service') && byId('log-service').querySelector('option[value="' + CSS.escape(query.get('service')) + '"]')) byId('log-service').value = query.get('service');
    if (query.get('level')) byId('log-level').value = query.get('level');
    byId('log-refresh').addEventListener('click', loadLogs); byId('log-service').addEventListener('change', loadLogs);
    byId('log-auto').addEventListener('change', function () { syncAuto(); if (byId('log-auto').checked) loadLogs(); });
    document.addEventListener('visibilitychange', function () { if (!document.hidden && byId('log-auto').checked) loadLogs(); });
    byId('log-copy').addEventListener('click', function () { copyText(byId('log-output').textContent); }); loadLogs();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
