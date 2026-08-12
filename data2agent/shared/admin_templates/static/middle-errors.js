(function () {
  'use strict';
  var alerts = [];
  var category = '';
  var pollFailures = 0;
  var pollTimer = null;

  function activeAlerts() {
    return alerts.filter(function (item) {
      return item.status === 'active' && !item.silenced_until && (!category || item.category === category);
    });
  }

  function renderFilters() {
    var counts = {};
    alerts.filter(function (item) { return item.status === 'active' && !item.silenced_until; })
      .forEach(function (item) { counts[item.category] = (counts[item.category] || 0) + 1; });
    var entries = [['', '全部', Object.values(counts).reduce(function (a, b) { return a + b; }, 0)]];
    Object.keys(counts).sort().forEach(function (key) { entries.push([key, key, counts[key]]); });
    var root = document.getElementById('category-filters');
    root.innerHTML = '';
    entries.forEach(function (entry) {
      var button = document.createElement('button');
      button.type = 'button'; button.className = 'chip' + (category === entry[0] ? ' active' : '');
      button.textContent = entry[1] + ' ' + entry[2];
      button.setAttribute('aria-pressed', category === entry[0] ? 'true' : 'false');
      button.addEventListener('click', function () { category = entry[0]; render(); });
      root.appendChild(button);
    });
  }

  function renderCard(item, recovered) {
    var html = '<article class="alert-card' + (recovered ? ' recovered' : '') + '">';
    html += '<div class="alert-head"><h3>' + esc(item.title) + '</h3>' +
      badge(recovered ? 'ok' : item.severity === 'critical' ? 'failed' : 'warning',
        recovered ? '已恢复' : item.severity || '告警') + '</div>';
    html += '<p class="meta">分类：' + esc(item.category) +
      (item.source ? ' · 源：' + esc(item.source) : '') +
      (item.table ? ' · 表：' + esc(item.table) : '') +
      ' · 首次：' + esc(fmtTime(item.first_seen_at)) +
      ' · 最近：' + esc(fmtTime(item.last_seen_at)) +
      ' · 次数：' + fmtNumber(item.occurrences) + '</p>';
    if (item.detail) html += '<p class="meta">' + esc(item.detail) + '</p>';
    if (item.suggestion) html += '<p class="warn">建议：' + esc(item.suggestion) + '</p>';
    if (!recovered) {
      html += '<div class="alert-actions">';
      if (item.retryable) html += '<button type="button" class="btn-ghost" data-action="retry" data-key="' + esc(item.key) + '">重试</button>';
      (item.links || []).forEach(function (link, index) {
        html += '<a href="' + esc(link) + '">' + (index ? '相关页面' : '查看详情') + '</a>';
      });
      if (item.silence_allowed) {
        html += '<label>静默 <select data-silence-hours><option value="1">1 小时</option><option value="24" selected>24 小时</option><option value="168">7 天</option></select></label>' +
          '<button type="button" class="btn-ghost" data-action="silence" data-key="' + esc(item.key) + '">确认静默</button>';
      } else html += '<span class="meta">此生产阻断告警不可静默</span>';
      html += '</div>';
    }
    return html + '</article>';
  }

  function render() {
    renderFilters();
    var current = activeAlerts();
    var root = document.getElementById('alerts-list');
    if (!current.length) renderState(root, 'empty', { message: '当前没有符合条件的未处理告警' });
    else root.innerHTML = current.map(function (item) { return renderCard(item, false); }).join('');
    var recovered = alerts.filter(function (item) { return item.status === 'recovered'; });
    document.getElementById('recovered-count').textContent = recovered.length;
    document.getElementById('recovered-list').innerHTML = recovered.length ?
      recovered.map(function (item) { return renderCard(item, true); }).join('') : '<p class="meta">暂无已恢复记录</p>';
    document.querySelectorAll('[data-action]').forEach(function (button) {
      button.addEventListener('click', function () { handleAction(button); });
    });
  }

  function findAlert(key) { return alerts.find(function (item) { return item.key === key; }); }

  async function handleAction(button) {
    var item = findAlert(button.dataset.key);
    if (!item) return;
    try {
      if (button.dataset.action === 'silence') {
        var select = button.parentElement.querySelector('[data-silence-hours]');
        await runAction(button, function () {
          return apiJson('/api/alerts/silences', {
            method: 'POST', body: JSON.stringify({ alert_key: item.key, hours: Number(select.value) })
          });
        }, { busyLabel: '保存中…', successMessage: '告警已静默' });
        return loadAlerts();
      }
      if (button.dataset.action === 'retry') {
        var request;
        if (item.run_id && item.key.indexOf('run:') === 0) {
          request = function () { return apiJson('/api/runs/' + item.run_id + '/retry-failed', { method: 'POST', body: '{}' }); };
        } else {
          request = function () { return apiJson('/api/actions/trigger', {
            method: 'POST', body: JSON.stringify({ action: 'sync', source: item.source,
              tables: item.table ? [item.table] : null })
          }); };
        }
        var result = await runAction(button, request, { busyLabel: '提交中…', successMessage: '重试已提交' });
        if (result.run_id) location.assign('/runs?watch=' + result.run_id);
      }
    } catch (_error) { /* runAction 已统一提示 */ }
  }

  async function loadAlerts(options) {
    options = options || {};
    var root = document.getElementById('alerts-list');
    if (!options.silent) renderState(root, 'loading');
    try {
      var body = await apiJson('/api/alerts');
      alerts = body.alerts || [];
      render();
      document.getElementById('alerts-refreshed').textContent =
        '最近成功刷新：' + fmtTime(body.observed_at || new Date().toISOString());
      var recovered = pollFailures > 0;
      pollFailures = 0;
      if (recovered) announce('success', '告警 API 已恢复');
      return true;
    } catch (error) {
      pollFailures += 1;
      renderState(root, 'error', { message: error.message, retry: loadAlerts });
      return false;
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    var delay = document.hidden ? 60000 : Math.min(120000, 30000 * Math.pow(2, Math.min(2, pollFailures)));
    pollTimer = setTimeout(async function () {
      if (!document.hidden) await loadAlerts({ silent: true });
      schedulePoll();
    }, delay);
  }

  document.getElementById('refresh-alerts').addEventListener('click', loadAlerts);
  loadAlerts().finally(schedulePoll);
})();
