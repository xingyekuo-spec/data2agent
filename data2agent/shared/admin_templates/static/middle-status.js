(function () {
  'use strict';
  var latestStatus = null;
  var lastStatusSuccessAt = null;
  var pollTimer = null;
  var pollFailures = 0;

  function statusCardClass(ok, warning) {
    return ok ? 'healthy-card' : warning ? 'warning-card' : 'critical-card';
  }

  function isBad(status) { return status === 'failed' || status === 'critical'; }
  function isWarn(status) { return status === 'warning' || status === 'unknown'; }

  /* ---- 聚合判定:把四个分区 + 源健康折算成一句话结论 ---- */
  function collectIssues(data) {
    var issues = [];
    var readiness = data.readiness || { ready: false };
    if (!readiness.ready) {
      var failedChecks = (readiness.checks || []).filter(function (c) { return c.status === 'fail'; });
      issues.push('生产就绪度存在 ' + (failedChecks.length || '若') + ' 干阻断项');
    }
    var ps = data.process_status || {};
    if (!(ps.supervised && ps.connector_running && ps.maintenance_running)) {
      issues.push(ps.stale ? '进程监管状态过期' : 'connector / maintenance 进程未全部运行');
    }
    var m = data.maintenance || {};
    if (m.status === 'failed') issues.push('最近状态库维护失败');
    else if (m.overdue) issues.push('状态库备份超期未成功');
    var residency = data.data_residency || {};
    if (residency.compliant === false) issues.push('数据驻留边界违规(本机不得持久化业务 Raw)');
    (data.sources || []).forEach(function (source) {
      var status = ((source.health || {}).status) || 'unknown';
      if (isBad(status)) issues.push('源 ' + source.source + ' 健康检查失败');
      else if (status === 'warning') issues.push('源 ' + source.source + ' 存在警告');
      if (!source.tables_configured) issues.push('源 ' + source.source + ' 尚未选表');
    });
    return issues;
  }

  /* ---- L1:总览横幅 —— 一眼结论 + 每源一行 ---- */
  function renderOverview(data) {
    var issues = collectIssues(data);
    var level = issues.length === 0 ? 'ok'
      : issues.some(function (i) { return /阻断|违规|失败|未全部/.test(i); }) ? 'critical' : 'warning';
    var panel = document.getElementById('overview-panel');
    panel.className = 'card ' + (level === 'ok' ? 'healthy-card' : level === 'warning' ? 'warning-card' : 'critical-card');
    var html = '<div class="overview-head"><h2 class="overview-title">' +
      (level === 'ok' ? '✅ 运行正常' : level === 'warning' ? '⚠️ 存在需关注项' : '❌ 存在异常') +
      '</h2>' + badge(level === 'ok' ? 'pass' : level === 'warning' ? 'warning' : 'failed',
        level === 'ok' ? '全部检查通过' : issues.length + ' 项需处理') + '</div>';
    html += '<p class="meta">状态时间:' + esc(fmtTime(data.observed_at)) +
      ' · 本页每 10 秒自动刷新,详情分区异常时会自动展开</p>';
    if (issues.length) {
      html += '<ul class="issue-list">' + issues.map(function (i) { return '<li>' + esc(i) + '</li>'; }).join('') + '</ul>';
    }
    (data.sources || []).forEach(function (source) {
      var freshness = source.freshness || {};
      var schedule = source.schedule || {};
      html += '<div class="src-mini"><span class="name">' + esc(source.source) + '</span>' +
        badge(((source.health || {}).status) || 'unknown') +
        '<span class="cell">上次成功 <b>' + esc(fmtTime(source.latest_success && source.latest_success.finished_at)) + '</b></span>' +
        '<span class="cell">新鲜度 <b>' + esc(freshness.status || 'unknown') + ' / ' + esc(fmtDuration(freshness.age_seconds)) + '</b></span>' +
        '<span class="cell">下次同步 <b>' + esc(fmtTime(schedule.next_sync_at)) + '</b></span>' +
        (source.running_run ? '<span class="cell"><a href="/runs?watch=' + source.running_run.id + '">正在运行 #' + source.running_run.id + '</a></span>' : '') +
        '</div>';
    });
    panel.innerHTML = html;
  }

  /* ---- L2:就绪度 —— 全过一行;有阻断直列失败项,通过项折叠 ---- */
  function renderReadiness(data) {
    var readiness = data.readiness || { ready: false, checks: [] };
    var checks = readiness.checks || [];
    var problems = checks.filter(function (c) { return c.status !== 'pass'; });
    var html = '<div class="src-head"><h2>生产就绪度</h2>' +
      badge(readiness.ready ? 'pass' : 'failed', readiness.ready ? '可以部署' : problems.length + ' 项未通过') + '</div>';
    function item(check) {
      return '<div class="status-item"><div><strong>' + esc(check.id) + '</strong><div class="meta">' +
        esc(check.detail) + (check.suggestion ? '<br>建议:' + esc(check.suggestion) : '') +
        '</div></div>' + badge(check.status, check.status === 'pass' ? '通过' : check.status === 'unknown' ? '未知' : '未通过') + '</div>';
    }
    if (!problems.length) {
      html += '<p class="oknote">全部 ' + checks.length + ' 项检查通过。' +
        '<details class="card-details"><summary>查看完整清单</summary><div class="status-list">' +
        checks.map(item).join('') + '</div></details></p>';
    } else {
      html += '<div class="status-list">' + problems.map(item).join('') + '</div>';
      var passed = checks.filter(function (c) { return c.status === 'pass'; });
      if (passed.length) {
        html += '<details class="card-details"><summary>已通过 ' + passed.length + ' 项</summary><div class="status-list">' +
          passed.map(item).join('') + '</div></details>';
      }
    }
    var panel = document.getElementById('readiness-panel');
    panel.className = 'card ' + statusCardClass(readiness.ready, false);
    panel.innerHTML = html;
  }

  /* ---- L2:进程 —— 正常一行摘要,异常自动展开全表 ---- */
  function renderProcesses(data) {
    var ps = data.process_status || {};
    var version = data.version || {};
    var processes = ps.processes || [];
    var ok = ps.supervised && ps.connector_running && ps.maintenance_running;
    var restarts = processes.reduce(function (sum, p) { return sum + (p.restarts || 0); }, 0);
    var html = '<section class="card ' + statusCardClass(ok, ps.stale) + '">';
    html += '<div class="src-head"><h2>进程监管</h2>' +
      badge(ok ? 'ok' : ps.stale ? 'warning' : 'failed', ok ? '监管正常' : ps.stale ? '状态过期' : '进程异常') + '</div>';
    if (!processes.length) {
      html += '<p class="warn">没有 launcher 进程记录,请检查启动方式和 <a href="/logs?service=launcher">d2a-launcher.log</a>。</p>';
      return html + '</section>';
    }
    html += '<p class="meta">' + processes.length + ' 个进程在监管中 · 累计重启 ' + fmtNumber(restarts) +
      ' 次 · 启动方式 ' + esc(ps.startup_mode || 'unknown') +
      ' · 中间机 v' + esc(version.middle_version || '未知') +
      ' · ingest 协议 v' + esc(version.ingest_protocol_version || '未知') +
      ' · <a href="/logs?service=launcher">launcher 日志</a></p>';
    var table = '<div class="table-scroll"><table class="data"><thead><tr><th>进程</th><th>状态</th><th>PID</th><th>重启</th><th>退出码</th><th>熔断时间</th><th>冷却至</th><th>配置 revision</th></tr></thead><tbody>';
    processes.forEach(function (process) {
      var logService = ['connector', 'maintenance', 'admin'].indexOf(process.name) >= 0 ? process.name : 'launcher';
      table += '<tr><td><a href="/logs?service=' + logService + '">' + esc(process.name) + '</a></td><td>' + badge(process.state) + '</td><td>' +
        esc(process.pid || '—') + '</td><td>' + fmtNumber(process.restarts) + '</td><td>' +
        esc(process.last_exit_code == null ? '—' : process.last_exit_code) + '</td><td>' +
        fmtTime(process.failed_at_epoch ? new Date(process.failed_at_epoch * 1000).toISOString() : null) + '</td><td>' +
        fmtTime(process.cooldown_until_epoch ? new Date(process.cooldown_until_epoch * 1000).toISOString() : null) + '</td><td><code>' +
        esc(process.loaded_config_revision || '—') + '</code></td></tr>';
    });
    table += '</tbody></table></div>';
    html += '<details class="card-details"' + (ok ? '' : ' open') + '><summary>进程明细</summary>' + table + '</details>';
    return html + '</section>';
  }

  /* ---- L2:维护 —— 摘要一行;清理结果折叠;异常自动展开 ---- */
  function renderMaintenance(data) {
    var maintenance = data.maintenance || {};
    var ok = maintenance.integrity === 'ok' && !maintenance.overdue && maintenance.status !== 'failed';
    var partial = maintenance.status === 'partial';
    var html = '<section class="card ' + statusCardClass(ok, partial) + '">';
    html += '<div class="src-head"><h2>状态库备份</h2>' +
      badge(ok ? 'ok' : maintenance.status || 'unknown') + '</div>';
    html += '<div class="src-kv"><span>最近成功 <b>' + esc(fmtTime(maintenance.last_success_at)) +
      '</b></span><span>大小 <b>' + esc(fmtBytes(maintenance.backup_size_bytes)) +
      '</b></span><span>可用空间 <b>' + esc(maintenance.free_gb == null ? '—' : maintenance.free_gb + ' GiB') +
      '</b></span><span>下次维护 <b>' + esc(fmtTime(maintenance.next_run_at)) + '</b></span></div>';
    html += '<p class="meta">仅中间机控制状态(水位/运行/推送),不含业务 Raw · <a href="/recovery">离线恢复指引</a></p>';
    if (maintenance.overdue) html += '<p class="warn">近期没有成功状态库备份,请检查维护进程、目录权限和磁盘空间。</p>';
    if (maintenance.error) html += '<p class="warn">最近错误:' + esc(maintenance.error) + '</p>';
    html += '<details class="card-details"' + (ok ? '' : ' open') + '><summary>清理与保留明细</summary><pre class="diagnostic">' +
      esc(JSON.stringify({ last_attempt_at: maintenance.last_attempt_at, integrity: maintenance.integrity,
        backup_file: maintenance.backup_file, pruned: maintenance.pruned, abandoned: maintenance.abandoned,
        removed_backups: maintenance.removed_backups, errors: maintenance.errors }, null, 2)) + '</pre></details>';
    return html + '</section>';
  }

  /* ---- L2:驻留 —— 合规一行;违规展开 ---- */
  function renderResidency(data) {
    var residency = data.data_residency || {};
    var autostart = data.autostart || {};
    var ok = residency.compliant !== false;
    var html = '<section class="card ' + statusCardClass(ok, false) + '">';
    html += '<div class="src-head"><h2>数据驻留边界</h2>' +
      badge(ok ? 'pass' : 'failed', ok ? '符合' : '违规') + '</div>';
    html += '<p class="meta">部署模式 ' + esc(residency.deployment_mode || '—') +
      ' · 推送出口 ' + esc(residency.sink_type || '—') +
      ' · 本机 Raw 表 <b>' + fmtNumber(residency.raw_table_count) + '</b>' +
      ' · 遗留 spool <b>' + fmtNumber(residency.orphan_spool_count) + '</b>' +
      ' · 开机任务 ' + esc(autostart.status || 'unknown') + '</p>';
    if (!ok) {
      html += '<div class="warn" role="alert">' + esc((residency.violations || []).join(';')) + '</div>';
    }
    html += '<details class="card-details"' + (ok ? '' : ' open') + '><summary>驻留明细</summary><pre class="diagnostic">' +
      esc(JSON.stringify({ state_db_file: residency.state_db_file, spool_policies: residency.spool_policies,
        raw_table_names_digest: residency.raw_table_names_digest, autostart: autostart }, null, 2)) + '</pre></details>';
    return html + '</section>';
  }

  /* ---- L2:源卡 —— 首行结论 + 4 关键项;调度/分项/水位折叠 ---- */
  function renderSource(source) {
    var configured = !!source.tables_configured;
    var health = source.health || { status: 'unknown', components: {} };
    var schedule = source.schedule || {};
    var healthy = health.status === 'ok' && configured;
    var html = '<section class="card ' + statusCardClass(healthy, health.status === 'warning' || !configured) + '">';
    html += '<div class="src-head"><h2>源 ' + esc(source.source) + '</h2>' + badge(health.status) +
      badge(source.in_window ? 'ok' : 'paused', source.in_window ? '窗口内' : '窗口外') +
      badge(configured ? 'configured' : 'failed', configured ? '已选表' : '未选表') + '</div>';
    if (!configured) {
      html += '<p class="warn">尚未配置抽取表。请先完成<a href="/metadata">元数据扫描</a>与<a href="/tables">抽取计划</a>。</p>';
    }
    html += '<p class="meta">调度 ' + esc(source.sync_every || '—') +
      ' · 窗口 ' + esc((source.windows || []).join(', ') || '全天') +
      ' · 上次失败 ' + esc(fmtTime(source.latest_failure && source.latest_failure.finished_at)) +
      ' · L1 对账计划 ' + esc(fmtTime(schedule.next_reconcile_at)) +
      ' · 深度对账 ' + esc(fmtTime(schedule.next_deep_reconcile_at)) + '</p>';
    var components = health.components || {};
    var stalledCount = (source.watermarks || []).filter(function (w) { return w.stalled; }).length;
    var detailOpen = !healthy || stalledCount > 0;
    var detailsHtml = '';
    if (Object.keys(components).length) {
      detailsHtml += '<p class="meta">健康分项:' + Object.keys(components).map(function (key) {
        return esc(key) + '=' + esc(components[key]);
      }).join(' · ') + '</p>';
    }
    if ((source.watermarks || []).length) {
      detailsHtml += '<div class="table-scroll"><table class="data"><thead><tr><th>表</th><th>列</th><th>高水位</th><th>类型</th><th>最近推进</th><th>连续未推进</th><th>上次同步</th></tr></thead><tbody>';
      source.watermarks.forEach(function (watermark) {
        var advance = watermark.recent_advance || {};
        var advanceText = advance.value == null ? '—' : advance.kind === 'duration_seconds' ? fmtDuration(advance.value) : advance.kind === 'numeric' ? String(advance.value) : advance.value ? '已变化' : '未变化';
        detailsHtml += '<tr><td>' + esc(watermark.table_name) + '</td><td>' + esc(watermark.watermark_col) +
          '</td><td class="long-cell">' + esc(watermark.high_water) + '</td><td>' + esc(watermark.value_type || '—') +
          '</td><td>' + esc(advanceText) + '</td><td>' + (watermark.stalled ? badge('warning', fmtNumber(watermark.unchanged_successive_runs) + ' 轮,需关注') : fmtNumber(watermark.unchanged_successive_runs || 0)) +
          '</td><td>' + esc(fmtTime(watermark.last_run_at)) + '</td></tr>';
      });
      detailsHtml += '</tbody></table></div>';
    }
    if (detailsHtml) {
      html += '<details class="card-details wm"' + (detailOpen ? ' open' : '') + '><summary>水位与健康分项(' +
        (source.watermarks || []).length + ' 张表' + (stalledCount ? ',' + stalledCount + ' 张停滞' : '') +
        ')</summary>' + detailsHtml + '</details>';
    }
    return html + '</section>';
  }

  function renderStatus(data) {
    renderOverview(data);
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
        message: error.message + (lastStatusSuccessAt ? ';最近成功刷新:' + fmtTime(lastStatusSuccessAt) : ''),
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
    } catch (error) { renderState(panel, 'error', { message: error.message + (panel.dataset.lastSuccessAt ? ';最近成功:' + fmtTime(panel.dataset.lastSuccessAt) : ''), retry: loadRunsSummary }); return false; }
  }

  async function loadPushSummary() {
    var panel = document.getElementById('push-summary');
    try {
      var data = await apiJson('/api/push-logs?limit=10');
      panel.dataset.lastSuccessAt = new Date().toISOString();
      var logs = data.push_logs || [];
      if (!logs.length) { renderState(panel, 'empty', { message: '暂无推送记录' }); return true; }
      var failures = logs.filter(function (item) { return item.status === 'failed'; }).length;
      panel.innerHTML = '<p class="meta">最近 ' + logs.length + ' 条,失败 ' + failures + ' 条</p>' +
        '<div class="table-scroll"><table class="data"><thead><tr><th>表</th><th>步骤</th><th>状态</th><th>时间</th></tr></thead><tbody>' +
        logs.slice(0, 5).map(function (item) { return '<tr><td>' + esc(item.table_name) + '</td><td>' +
          esc(item.step_kind) + '</td><td>' + badge(item.status) + '</td><td>' + esc(fmtTime(item.created_at)) +
          '</td></tr>'; }).join('') + '</tbody></table></div>';
      return true;
    } catch (error) { renderState(panel, 'error', { message: error.message + (panel.dataset.lastSuccessAt ? ';最近成功:' + fmtTime(panel.dataset.lastSuccessAt) : ''), retry: loadPushSummary }); return false; }
  }

  async function trigger(action, button) {
    var source = document.getElementById('status-source').value || null;
    if (action === 'sync' && !confirm('立即同步会访问所选源 ' + (source || '(未选择)') + ' 的配置表并推送到平台,确认启动?')) return;
    if (action === 'reconcile_deep' && !confirm('深度对账会重读所选源 ' + (source || '(未选择)') + ' 并执行修复,确认现在启动?')) return;
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
        if (recovered) announce('success', '管理 API 已恢复,状态已刷新');
      }
      schedulePoll();
    }, delay);
  }
  Promise.all([loadStatus(), loadRunsSummary(), loadPushSummary()]).then(function (results) {
    pollFailures = results.every(Boolean) ? 0 : 1;
  }).finally(schedulePoll);
})();
