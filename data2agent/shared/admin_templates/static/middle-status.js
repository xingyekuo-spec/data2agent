(function () {
  'use strict';
  var latestStatus = null;
  var lastStatusSuccessAt = null;
  var pollTimer = null;
  var pollFailures = 0;

  function statusCardClass(ok, warning) {
    return ok ? 'healthy-card' : warning ? 'warning-card' : 'critical-card';
  }

  function renderReadiness(data) {
    var readiness = data.readiness || { ready: false, checks: [] };
    var html = '<div class="src-head"><h2>生产就绪度</h2>' +
      badge(readiness.ready ? 'pass' : 'failed', readiness.ready ? '可以部署' : '存在阻断项') + '</div>';
    html += '<p class="meta">状态时间：' + esc(fmtTime(data.observed_at)) +
      ' · 配置 revision：<code>' + esc(data.config_revision || '未知') + '</code></p>';
    html += '<div class="status-list">';
    (readiness.checks || []).forEach(function (check) {
      html += '<div class="status-item"><div><strong>' + esc(check.id) + '</strong><div class="meta">' +
        esc(check.detail) + (check.suggestion ? '<br>建议：' + esc(check.suggestion) : '') +
        '</div></div>' + badge(check.status, check.status === 'pass' ? '通过' : check.status === 'unknown' ? '未知' : '未通过') + '</div>';
    });
    html += '</div>';
    var panel = document.getElementById('readiness-panel');
    panel.className = 'card ' + statusCardClass(readiness.ready, false);
    panel.innerHTML = html;
  }

  function renderProcesses(data) {
    var ps = data.process_status || {};
    var version = data.version || {};
    var processes = ps.processes || [];
    var ok = ps.supervised && ps.connector_running && ps.maintenance_running;
    var html = '<section class="card ' + statusCardClass(ok, ps.stale) + '">';
    html += '<div class="src-head"><h2>真实进程健康</h2>' +
      badge(ok ? 'ok' : ps.stale ? 'warning' : 'failed', ok ? '监管正常' : ps.stale ? '状态过期' : '进程异常') + '</div>';
    html += '<p class="meta">启动方式：' + esc(ps.startup_mode || 'unknown') +
      ' · launcher PID：' + esc(ps.launcher_pid || '—') +
      ' · 状态年龄：' + esc(fmtDuration(ps.age_seconds)) +
      ' · 中间机版本：' + esc(version.middle_version || '未知') +
      ' · ingest 协议：v' + esc(version.ingest_protocol_version || '未知') +
      ' · <a href="/logs?service=launcher">launcher 日志</a></p>';
    if (!processes.length) html += '<p class="warn">没有 launcher 进程记录，请检查启动方式和 d2a-launcher.log。</p>';
    else {
      html += '<div class="table-scroll"><table class="data"><thead><tr><th>进程</th><th>状态</th><th>PID</th><th>重启</th><th>退出码</th><th>熔断时间</th><th>冷却至</th><th>配置 revision</th></tr></thead><tbody>';
      processes.forEach(function (process) {
        var logService = ['connector', 'maintenance', 'admin'].indexOf(process.name) >= 0 ? process.name : 'launcher';
        html += '<tr><td><a href="/logs?service=' + logService + '">' + esc(process.name) + '</a></td><td>' + badge(process.state) + '</td><td>' +
          esc(process.pid || '—') + '</td><td>' + fmtNumber(process.restarts) + '</td><td>' +
          esc(process.last_exit_code == null ? '—' : process.last_exit_code) + '</td><td>' +
          fmtTime(process.failed_at_epoch ? new Date(process.failed_at_epoch * 1000).toISOString() : null) + '</td><td>' +
          fmtTime(process.cooldown_until_epoch ? new Date(process.cooldown_until_epoch * 1000).toISOString() : null) + '</td><td><code>' +
          esc(process.loaded_config_revision || '—') + '</code></td></tr>';
      });
      html += '</tbody></table></div>';
    }
    return html + '</section>';
  }

  function renderMaintenance(data) {
    var maintenance = data.maintenance || {};
    var ok = maintenance.integrity === 'ok' && !maintenance.overdue && maintenance.status !== 'failed';
    var html = '<section class="card ' + statusCardClass(ok, maintenance.status === 'partial') + '">';
    html += '<div class="src-head"><h2>维护与状态库备份</h2>' +
      badge(ok ? 'ok' : maintenance.status || 'unknown') + '</div>';
    html += '<p class="meta"><strong>这是中间机控制状态库备份，不包含业务 Raw 数据。</strong></p>';
    html += '<p class="meta"><a href="/recovery">查看状态库只读校验与离线恢复指引</a>（页面不提供在线覆盖按钮）</p>';
    html += '<div class="src-kv"><span>最近尝试 <b>' + esc(fmtTime(maintenance.last_attempt_at)) +
      '</b></span><span>最近成功 <b>' + esc(fmtTime(maintenance.last_success_at)) +
      '</b></span><span>文件 <b>' + esc(maintenance.backup_file || '—') +
      '</b></span><span>大小 <b>' + esc(fmtBytes(maintenance.backup_size_bytes)) +
      '</b></span><span>完整性 <b>' + esc(maintenance.integrity || '未知') +
      '</b></span><span>可用空间 <b>' + esc(maintenance.free_gb == null ? '—' : maintenance.free_gb + ' GiB') +
      '</b></span><span>下次维护 <b>' + esc(fmtTime(maintenance.next_run_at)) + '</b></span></div>';
    if (maintenance.overdue) html += '<p class="warn">近期没有成功状态库备份，请检查维护进程、目录权限和磁盘空间。</p>';
    if (maintenance.error) html += '<p class="warn">最近错误：' + esc(maintenance.error) + '</p>';
    html += '<details><summary>清理与保留结果</summary><pre class="diagnostic">' +
      esc(JSON.stringify({ pruned: maintenance.pruned, abandoned: maintenance.abandoned,
        removed_backups: maintenance.removed_backups, errors: maintenance.errors }, null, 2)) + '</pre></details>';
    return html + '</section>';
  }

  function renderResidency(data) {
    var residency = data.data_residency || {};
    var autostart = data.autostart || {};
    var html = '<section class="card ' + statusCardClass(residency.compliant, false) + '">';
    html += '<div class="src-head"><h2>数据驻留边界</h2>' +
      badge(residency.compliant ? 'pass' : 'failed', residency.compliant ? '符合' : '违规') + '</div>';
    html += '<div class="src-kv"><span>部署模式 <b>' + esc(residency.deployment_mode || '—') +
      '</b></span><span>推送出口 <b>' + esc(residency.sink_type || '—') +
      '</b></span><span>状态库 <b>' + esc(residency.state_db_file || '—') +
      '</b></span><span>本机 Raw 表 <b>' + fmtNumber(residency.raw_table_count) +
      '</b></span><span>遗留 spool <b>' + fmtNumber(residency.orphan_spool_count) +
      '</b></span><span>开机任务 <b>' + esc(autostart.status || 'unknown') + '</b></span></div>';
    html += '<p class="meta">spool 策略：' + esc(JSON.stringify(residency.spool_policies || {})) + '</p>';
    if (!residency.compliant) html += '<div class="warn" role="alert">' +
      esc((residency.violations || []).join('；')) + '</div>';
    return html + '</section>';
  }

  function renderSource(source) {
    var configured = !!source.tables_configured;
    var health = source.health || { status: 'unknown', components: {} };
    var freshness = source.freshness || {};
    var schedule = source.schedule || {};
    var html = '<section class="card ' + statusCardClass(health.status === 'ok', health.status === 'warning') + '">';
    html += '<div class="src-head"><h2>源 ' + esc(source.source) + '</h2>' + badge(health.status) +
      badge(source.in_window ? 'ok' : 'paused', source.in_window ? '窗口内' : '窗口外') +
      badge(configured ? 'configured' : 'failed', configured ? '已选表' : '未选表') + '</div>';
    html += '<div class="src-kv"><span>调度 <b>' + esc(source.sync_every || '—') +
      '</b></span><span>窗口 <b>' + esc((source.windows || []).join(', ') || '全天') +
      '</b></span><span>时区 <b>' + esc(source.timezone || '—') +
      '</b></span><span>上次成功 <b>' + esc(fmtTime(source.latest_success && source.latest_success.finished_at)) +
      '</b></span><span>上次失败 <b>' + esc(fmtTime(source.latest_failure && source.latest_failure.finished_at)) +
      '</b></span><span>新鲜度 <b>' + esc(freshness.status || 'unknown') + ' / ' +
      esc(fmtDuration(freshness.age_seconds)) + '</b></span></div>';
    html += '<p class="meta">计划（推算）：下次同步 ' + esc(fmtTime(schedule.next_sync_at)) +
      ' · L1 ' + esc(fmtTime(schedule.next_reconcile_at)) +
      ' · 深度对账 ' + esc(fmtTime(schedule.next_deep_reconcile_at)) + '</p>';
    html += '<p class="meta">真实运行：' + (source.running_run ?
      '<a href="/runs?watch=' + source.running_run.id + '">正在运行 #' + source.running_run.id + '</a>' : '当前无运行') + '</p>';
    if (!configured) html += '<p class="warn">尚未配置抽取表。请先完成<a href="/metadata">元数据扫描</a>与<a href="/tables">抽取计划</a>。</p>';
    var components = health.components || {};
    html += '<p class="meta">分项：' + Object.keys(components).map(function (key) {
      return esc(key) + '=' + esc(components[key]);
    }).join(' · ') + '</p>';
    if ((source.watermarks || []).length) {
      html += '<details class="wm"><summary>水位（' + source.watermarks.length + ' 张表）</summary><div class="table-scroll"><table class="data"><thead><tr><th>表</th><th>列</th><th>高水位</th><th>类型</th><th>最近推进</th><th>连续未推进</th><th>上次同步</th></tr></thead><tbody>';
      source.watermarks.forEach(function (watermark) {
        var advance = watermark.recent_advance || {};
        var advanceText = advance.value == null ? '—' : advance.kind === 'duration_seconds' ? fmtDuration(advance.value) : advance.kind === 'numeric' ? String(advance.value) : advance.value ? '已变化' : '未变化';
        html += '<tr><td>' + esc(watermark.table_name) + '</td><td>' + esc(watermark.watermark_col) +
          '</td><td class="long-cell">' + esc(watermark.high_water) + '</td><td>' + esc(watermark.value_type || '—') +
          '</td><td>' + esc(advanceText) + '</td><td>' + (watermark.stalled ? badge('warning', fmtNumber(watermark.unchanged_successive_runs) + ' 轮，需关注') : fmtNumber(watermark.unchanged_successive_runs || 0)) +
          '</td><td>' + esc(fmtTime(watermark.last_run_at)) + '</td></tr>';
      });
      html += '</tbody></table></div></details>';
    }
    return html + '</section>';
  }

  function renderStatus(data) {
    renderReadiness(data);
    var sourceSelect = document.getElementById('status-source');
    var selected = sourceSelect.value;
    sourceSelect.innerHTML = (data.sources || []).map(function (source) {
      return '<option value="' + esc(source.source) + '">' + esc(source.source) + '</option>';
    }).join('');
    if (selected && (data.sources || []).some(function (source) { return source.source === selected; })) sourceSelect.value = selected;
    var html = renderProcesses(data) + renderMaintenance(data) + renderResidency(data);
    (data.sources || []).forEach(function (source) { html += renderSource(source); });
    document.getElementById('status-panel').innerHTML = html;
  }

  async function loadStatus() {
    var panel = document.getElementById('status-panel');
    try {
      var data = await apiJson('/api/status', { timeoutMs: 10000 });
      latestStatus = data; lastStatusSuccessAt = new Date().toISOString();
      renderStatus(data);
      return true;
    } catch (error) {
      renderState(panel, 'error', {
        message: error.message + (lastStatusSuccessAt ? '；最近成功刷新：' + fmtTime(lastStatusSuccessAt) : ''),
        retry: loadStatus
      });
      return false;
    }
  }

  async function loadRunsSummary() {
    var panel = document.getElementById('runs-summary');
    try {
      var data = await apiJson('/api/runs?limit=5');
      panel.dataset.lastSuccessAt = new Date().toISOString();
      if (!(data.runs || []).length) { renderState(panel, 'empty', { message: '暂无运行记录' }); return true; }
      panel.innerHTML = '<div class="table-scroll"><table class="data"><thead><tr><th>ID</th><th>源</th><th>状态</th><th>行数</th><th>开始</th></tr></thead><tbody>' +
        data.runs.map(function (run) { return '<tr><td><a href="/runs?watch=' + run.id + '">#' + run.id +
          '</a></td><td>' + esc(run.source) + '</td><td>' + badge(run.status) + '</td><td>' +
          fmtNumber(run.rows || 0) + '</td><td>' + esc(fmtTime(run.started_at)) + '</td></tr>'; }).join('') +
        '</tbody></table></div>';
      return true;
    } catch (error) { renderState(panel, 'error', { message: error.message + (panel.dataset.lastSuccessAt ? '；最近成功：' + fmtTime(panel.dataset.lastSuccessAt) : ''), retry: loadRunsSummary }); return false; }
  }

  async function loadPushSummary() {
    var panel = document.getElementById('push-summary');
    try {
      var data = await apiJson('/api/push-logs?limit=10');
      panel.dataset.lastSuccessAt = new Date().toISOString();
      var logs = data.push_logs || [];
      if (!logs.length) { renderState(panel, 'empty', { message: '暂无推送记录' }); return true; }
      var failures = logs.filter(function (item) { return item.status === 'failed'; }).length;
      panel.innerHTML = '<p class="meta">最近 ' + logs.length + ' 条，失败 ' + failures + ' 条</p>' +
        '<div class="table-scroll"><table class="data"><thead><tr><th>表</th><th>步骤</th><th>状态</th><th>时间</th></tr></thead><tbody>' +
        logs.slice(0, 5).map(function (item) { return '<tr><td>' + esc(item.table_name) + '</td><td>' +
          esc(item.step_kind) + '</td><td>' + badge(item.status) + '</td><td>' + esc(fmtTime(item.created_at)) +
          '</td></tr>'; }).join('') + '</tbody></table></div>';
      return true;
    } catch (error) { renderState(panel, 'error', { message: error.message + (panel.dataset.lastSuccessAt ? '；最近成功：' + fmtTime(panel.dataset.lastSuccessAt) : ''), retry: loadPushSummary }); return false; }
  }

  async function trigger(action, button) {
    var source = document.getElementById('status-source').value || null;
    if (action === 'sync' && !confirm('立即同步会访问所选源 ' + (source || '（未选择）') + ' 的配置表并推送到平台，确认启动？')) return;
    if (action === 'reconcile_deep' && !confirm('深度对账会重读所选源 ' + (source || '（未选择）') + ' 并执行修复，确认现在启动？')) return;
    var result = document.getElementById('action-result');
    try {
      var body = await runAction(button, function () {
        return apiJson('/api/actions/trigger', {
          method: 'POST', body: JSON.stringify({ action: action, source: source }), timeoutMs: 15000
        });
      }, { busyLabel: '提交中…' });
      if (body && body.run_id) result.innerHTML = '已提交 — <a href="/runs?watch=' + body.run_id + '">运行 #' + body.run_id + '</a>';
      else result.textContent = body.note || body.message || '请求已完成';
      announce('success', result.textContent || '动作已提交');
      loadStatus(); loadRunsSummary();
    } catch (error) { result.textContent = error.message; }
  }

  document.getElementById('btn-trigger').addEventListener('click', function () { trigger('sync', this); });
  document.getElementById('btn-reconcile').addEventListener('click', function () { trigger('reconcile', this); });
  document.getElementById('btn-reconcile-deep').addEventListener('click', function () { trigger('reconcile_deep', this); });
  document.getElementById('btn-test').addEventListener('click', async function () {
    var button = this, result = document.getElementById('action-result');
    var source = document.getElementById('status-source').value || null;
    try {
      var body = await runAction(button, function () {
        return apiJson('/api/connection/test', { method: 'POST', body: JSON.stringify({ source: source }) });
      }, { busyLabel: '测试中…' });
      sessionStorage.setItem('d2a_last_connection_test', JSON.stringify({ at: Date.now(), body: body }));
      result.textContent = body.status === 'failed' || body.error ? formatApiError(body, '连接失败') : 'ERP 连接正常';
    } catch (error) { result.textContent = error.message; }
  });
  document.getElementById('btn-diagnostic').addEventListener('click', function () {
    if (!latestStatus) return announce('error', '状态尚未加载');
    copyText(JSON.stringify({ observed_at: latestStatus.observed_at, version: latestStatus.version,
      process_status: latestStatus.process_status, readiness: latestStatus.readiness,
      data_residency: latestStatus.data_residency,
      sources: (latestStatus.sources || []).map(function (source) {
        return { source: source.source, health: source.health, freshness: source.freshness,
          latest_run: source.latest_run, schedule: source.schedule };
      }) }, null, 2));
  });

  function schedulePoll() {
    clearTimeout(pollTimer);
    var delay = document.hidden ? 60000 : Math.min(60000, 10000 * Math.pow(2, Math.min(3, pollFailures)));
    pollTimer = setTimeout(async function () {
      if (!document.hidden) {
        var results = await Promise.all([loadStatus(), loadRunsSummary(), loadPushSummary()]);
        var recovered = pollFailures > 0 && results.every(Boolean);
        pollFailures = results.every(Boolean) ? 0 : pollFailures + 1;
        if (recovered) announce('success', '管理 API 已恢复，状态已刷新');
      }
      schedulePoll();
    }, delay);
  }
  Promise.all([loadStatus(), loadRunsSummary(), loadPushSummary()]).then(function (results) {
    pollFailures = results.every(Boolean) ? 0 : 1;
  }).finally(schedulePoll);
})();
