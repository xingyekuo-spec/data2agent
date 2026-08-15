(function () {
  'use strict';
  var latestStatus = null;
  var autoSwitchedToSystem = false;
  var lastStatusSuccessAt = null;
  var pollTimer = null;
  var pollFailures = 0;

  function statusCardClass(ok, warning) {
    return ok ? 'healthy-card' : warning ? 'warning-card' : 'critical-card';
  }

  function kv(key, value) {
    return '<dt>' + esc(key) + '</dt><dd>' + esc(value) + '</dd>';
  }

  var COUNT_LABELS = {
    runs: '同步运行', run_steps: '运行步骤', audit: '审计日志',
    push_logs: '推送记录', receipts: '回执', reconcile: '对账记录',
    staging: '暂存快照'
  };

  function fmtCountDict(dict) {
    if (!dict || !Object.keys(dict).length) return '无';
    return Object.keys(dict).map(function (key) {
      return (COUNT_LABELS[key] || key) + ' ' + fmtNumber(dict[key]);
    }).join(' · ');
  }

  function isBad(status) { return status === 'failed' || status === 'critical'; }
  function isWarn(status) { return status === 'warning' || status === 'unknown'; }

  /* 统一状态词汇:正常/警告/异常/未知(领域 badge 如窗口内外、选表状态不变) */
  function healthBadge(status) {
    var map = {
      ok: ['ok', '正常'], pass: ['ok', '正常'], fresh: ['ok', '正常'],
      warning: ['warning', '警告'], partial: ['warning', '警告'], stale: ['warning', '警告'],
      failed: ['failed', '异常'], critical: ['failed', '异常']
    };
    var entry = map[status] || ['unknown', '未知'];
    return badge(entry[0], entry[1]);
  }

  /* ---- 关注项忽略:localStorage 持久化,可恢复 ---- */
  var DISMISS_KEY = 'd2a_dismissed_issues';
  function loadDismissed() {
    try {
      var value = JSON.parse(localStorage.getItem(DISMISS_KEY) || '[]');
      return Array.isArray(value) ? value : [];
    } catch (_error) { return []; }
  }
  function saveDismissed(list) {
    localStorage.setItem(DISMISS_KEY, JSON.stringify(list));
  }

  /* ---- 聚合判定:把四个分区 + 源健康折算成一句话结论 ---- */
  function collectIssues(data) {
    var issues = [];
    function add(key, text) { issues.push({ key: key, text: text }); }
    var readiness = data.readiness || { ready: false };
    if (!readiness.ready) {
      var failedChecks = (readiness.checks || []).filter(function (c) { return c.status === 'fail'; });
      add('readiness', '生产就绪度存在 ' + (failedChecks.length || '若') + ' 干阻断项');
    }
    var ps = data.process_status || {};
    if (!(ps.supervised && ps.connector_running && ps.maintenance_running)) {
      add('processes', ps.stale ? '进程监管状态过期' : 'connector / maintenance 进程未全部运行');
    }
    var m = data.maintenance || {};
    if (m.status === 'failed') add('maintenance', '最近状态库维护失败');
    else if (m.overdue) add('backup-overdue', '状态库备份超期未成功');
    var residency = data.data_residency || {};
    if (residency.compliant === false) add('residency', '数据驻留边界违规(本机不得持久化业务 Raw)');
    (data.sources || []).forEach(function (source) {
      var status = ((source.health || {}).status) || 'unknown';
      if (isBad(status)) add('source-health-' + source.source, '源 ' + source.source + ' 健康检查失败');
      else if (status === 'warning') add('source-warn-' + source.source, '源 ' + source.source + ' 存在警告');
      if (!source.tables_configured) add('tables-' + source.source, '源 ' + source.source + ' 尚未选表');
      var stalled = (source.watermarks || []).filter(function (w) { return w.stalled; }).length;
      if (stalled) add('stall-' + source.source, '源 ' + source.source + ':' + stalled + ' 张表水位连续未推进');
    });
    return issues;
  }

  /* ---- L1:总览横幅 —— 一眼结论 + 每源一行 ---- */
  function renderOverview(data) {
    var all = collectIssues(data);
    var dismissed = loadDismissed();
    var issues = all.filter(function (i) { return dismissed.indexOf(i.key) < 0; });
    var dismissedCount = all.length - issues.length;
    var level = issues.length === 0 ? 'ok'
      : issues.some(function (i) { return /阻断|违规|失败|未全部/.test(i.text); }) ? 'critical' : 'warning';
    var panel = document.getElementById('overview-panel');
    panel.className = 'card ' + (level === 'ok' ? 'healthy-card' : level === 'warning' ? 'warning-card' : 'critical-card');
    var headBadge = level === 'ok'
      ? badge('pass', dismissedCount ? '已忽略 ' + dismissedCount + ' 项' : '全部检查通过')
      : badge(level === 'warning' ? 'warning' : 'failed', issues.length + ' 项需处理');
    var html = '<div class="overview-head"><h2 class="overview-title">' +
      (level === 'ok' ? '✅ 运行正常' : level === 'warning' ? '⚠️ 存在需关注项' : '❌ 存在异常') +
      '</h2>' + headBadge + '</div>';
    html += '<p class="meta">状态时间:' + esc(fmtTime(data.observed_at)) +
      ' · 本页每 10 秒自动刷新,详情分区异常时会自动展开' +
      (dismissedCount ? ' · <a href="#" class="issue-restore">恢复 ' + dismissedCount + ' 项已忽略关注</a>' : '') + '</p>';
    if (issues.length) {
      html += '<ul class="issue-list">' + issues.map(function (i) {
        return '<li>' + esc(i.text) +
          ' <button type="button" class="issue-dismiss" data-key="' + esc(i.key) +
          '" title="不再显示此项(可恢复)" aria-label="忽略此项">×</button></li>';
      }).join('') + '</ul>';
    }
    (data.sources || []).forEach(function (source) {
      var freshness = source.freshness || {};
      var schedule = source.schedule || {};
      html += '<div class="src-mini"><span class="name">' + esc(source.source) + '</span>' +
        healthBadge(((source.health || {}).status) || 'unknown') +
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
      healthBadge(readiness.ready ? 'ok' : 'failed') +
      (readiness.ready ? '' : '<span class="meta">' + problems.length + ' 项未通过</span>') + '</div>';
    function item(check) {
      return '<div class="status-item"><div><strong>' + esc(check.id) + '</strong><div class="meta">' +
        esc(check.detail) + (check.suggestion ? '<br>建议:' + esc(check.suggestion) : '') +
        '</div></div>' + badge(check.status, check.status === 'pass' ? '通过' : check.status === 'unknown' ? '未知' : '未通过') + '</div>';
    }
    if (!problems.length) {
      html += '<p class="oknote">全部 ' + checks.length + ' 项检查通过。' +
        '<details class="card-details" data-k="ready-all"><summary>查看完整清单</summary><div class="status-list">' +
        checks.map(item).join('') + '</div></details></p>';
    } else {
      html += '<div class="status-list">' + problems.map(item).join('') + '</div>';
      var passed = checks.filter(function (c) { return c.status === 'pass'; });
      if (passed.length) {
        html += '<details class="card-details" data-k="ready-passed"><summary>已通过 ' + passed.length + ' 项</summary><div class="status-list">' +
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
      healthBadge(ok ? 'ok' : ps.stale ? 'stale' : 'failed') + '</div>';
    if (!processes.length) {
      html += '<p class="warn">没有 launcher 进程记录,请检查启动方式和 <a href="/logs?service=launcher">d2a-launcher.log</a>。</p>';
      return html + '</section>';
    }
    if (ps.stale) html += '<p class="warn">监管状态过期,进程可能已脱离监管。</p>';
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
    html += '<details class="card-details" data-k="proc"' + (ok ? '' : ' open') + '><summary>进程明细</summary>' + table + '</details>';
    return html + '</section>';
  }

  /* ---- L2:维护 —— 摘要一行;清理结果折叠;异常自动展开 ---- */
  function renderMaintenance(data) {
    var maintenance = data.maintenance || {};
    var ok = maintenance.integrity === 'ok' && !maintenance.overdue && maintenance.status !== 'failed';
    var partial = maintenance.status === 'partial';
    var html = '<section class="card ' + statusCardClass(ok, partial) + '">';
    html += '<div class="src-head"><h2>状态库备份</h2>' +
      healthBadge(ok ? 'ok' : maintenance.status === 'partial' ? 'partial' : maintenance.status === 'failed' ? 'failed' : 'unknown') + '</div>';
    html += '<div class="src-kv"><span>最近成功 <b>' + esc(fmtTime(maintenance.last_success_at)) +
      '</b></span><span>大小 <b>' + esc(fmtBytes(maintenance.backup_size_bytes)) +
      '</b></span><span>可用空间 <b>' + esc(maintenance.free_gb == null ? '—' : maintenance.free_gb + ' GiB') +
      '</b></span><span>下次维护 <b>' + esc(fmtTime(maintenance.next_run_at)) + '</b></span></div>';
    html += '<p class="meta">仅中间机控制状态(水位/运行/推送),不含业务 Raw · <a href="/recovery">离线恢复指引</a></p>';
    if (maintenance.overdue) html += '<p class="warn">近期没有成功状态库备份,请检查维护进程、目录权限和磁盘空间。</p>';
    if (maintenance.error) html += '<p class="warn">最近错误:' + esc(maintenance.error) + '</p>';
    var maintErrors = maintenance.errors || [];
    html += '<details class="card-details" data-k="maint"' + (ok ? '' : ' open') + '><summary>清理与保留明细</summary><dl class="kv-list">' +
      kv('最近尝试', fmtTime(maintenance.last_attempt_at)) +
      kv('备份文件', maintenance.backup_file || '—') +
      kv('完整性', maintenance.integrity || '未知') +
      kv('历史记录清理', fmtCountDict(maintenance.pruned)) +
      kv('孤儿暂存清理', fmtCountDict(maintenance.abandoned)) +
      kv('备份轮换删除', fmtNumber(maintenance.removed_backups || 0) + ' 份') +
      kv('清理错误', maintErrors.length ? maintErrors.map(function (e) {
        return (e.step || '?') + ': ' + (e.error || '?');
      }).join(';') : '无') +
      '</dl></details>';
    return html + '</section>';
  }

  /* ---- L2:驻留 —— 合规一行;违规展开 ---- */
  function renderResidency(data) {
    var residency = data.data_residency || {};
    var autostart = data.autostart || {};
    var ok = residency.compliant !== false;
    var html = '<section class="card ' + statusCardClass(ok, false) + '">';
    html += '<div class="src-head"><h2>数据驻留边界</h2>' +
      healthBadge(ok ? 'ok' : 'failed') + '</div>';
    html += '<p class="meta">部署模式 ' + esc(residency.deployment_mode || '—') +
      ' · 推送出口 ' + esc(residency.sink_type || '—') +
      ' · 本机 Raw 表 <b>' + fmtNumber(residency.raw_table_count) + '</b>' +
      ' · 遗留 spool <b>' + fmtNumber(residency.orphan_spool_count) + '</b>' +
      ' · 开机任务 ' + esc(autostart.status || 'unknown') + '</p>';
    if (!ok) {
      html += '<div class="warn" role="alert">' + esc((residency.violations || []).join(';')) + '</div>';
    }
    var spoolSources = residency.spool_sources || {};
    var spoolRows = Object.keys(spoolSources).map(function (name) {
      var s = spoolSources[name] || {};
      var text = s.policy || '—';
      if (s.directory_configured) {
        text += ' · 目录保护' + (s.directory_protected === true ? '✓' : s.directory_protected === false ? '✗' : '未知');
      }
      if (s.encrypted_at_rest) text += ' · 静态加密已确认';
      if (s.active_count) text += ' · 活跃 ' + fmtNumber(s.active_count);
      if (s.orphan_count) text += ' · 遗留 ' + fmtNumber(s.orphan_count);
      return kv('spool · ' + name, text);
    }).join('');
    if (!spoolRows) {
      var policies = residency.spool_policies || {};
      spoolRows = Object.keys(policies).map(function (name) {
        return kv('spool · ' + name, policies[name]);
      }).join('') || kv('spool 策略', '—');
    }
    var autostartText = autostart.status === 'installed'
      ? '已安装(' + (autostart.task_name || '?') + ',校验于 ' + fmtTime(autostart.checked_at) + ')'
      : autostart.status === 'not_installed' ? '未安装' : '未知';
    html += '<details class="card-details" data-k="res"' + (ok ? '' : ' open') + '><summary>驻留明细</summary><dl class="kv-list">' +
      kv('状态库文件', residency.state_db_file || '—') +
      kv('Raw 表指纹', (residency.raw_table_name_digests || []).join(', ') || '无') +
      spoolRows +
      kv('开机任务', autostartText) +
      '</dl></details>';
    return html + '</section>';
  }

  /* ---- L2:源卡 —— 首行结论 + 4 关键项;调度/分项/水位折叠 ---- */
  function renderSource(source) {
    var configured = !!source.tables_configured;
    var health = source.health || { status: 'unknown', components: {} };
    var schedule = source.schedule || {};
    var healthy = health.status === 'ok' && configured;
    var html = '<section class="card ' + statusCardClass(healthy, health.status === 'warning' || !configured) + '">';
    html += '<div class="src-head"><h2>源 ' + esc(source.source) + '</h2>' + healthBadge(health.status) +
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
        var advanceText;
        if (advance.value == null) advanceText = '—';
        else if (advance.kind === 'duration_seconds') advanceText = advance.value > 0 ? fmtDuration(advance.value) : '未推进';
        else if (advance.kind === 'numeric') advanceText = advance.value ? String(advance.value) : '未变化';
        else advanceText = advance.value ? '已变化' : '未变化';
        detailsHtml += '<tr><td>' + esc(watermark.table_name) + '</td><td>' + esc(watermark.watermark_col) +
          '</td><td class="long-cell">' + esc(watermark.high_water) + '</td><td>' + esc(watermark.value_type || '—') +
          '</td><td>' + esc(advanceText) + '</td><td>' + (watermark.stalled ? badge('warning', fmtNumber(watermark.unchanged_successive_runs) + ' 轮,需关注') : fmtNumber(watermark.unchanged_successive_runs || 0)) +
          '</td><td>' + esc(fmtTime(watermark.last_run_at)) + '</td></tr>';
      });
      detailsHtml += '</tbody></table></div>';
    }
    if (detailsHtml) {
      html += '<details class="card-details wm" data-k="src-' + esc(source.source) + '"' + (detailOpen ? ' open' : '') + '><summary>水位与健康分项(' +
        (source.watermarks || []).length + ' 张表' + (stalledCount ? ',' + stalledCount + ' 张停滞' : '') +
        ')</summary>' + detailsHtml + '</details>';
    }
    return html + '</section>';
  }

  /* 轮询重渲时保留用户手动展开的 details(按 data-k 标识) */
  function captureOpenDetails() {
    var open = {};
    document.querySelectorAll('details[data-k]').forEach(function (d) {
      if (d.open) open[d.dataset.k] = true;
    });
    return open;
  }
  function restoreOpenDetails(open) {
    document.querySelectorAll('details[data-k]').forEach(function (d) {
      if (open[d.dataset.k]) d.open = true;
    });
  }

  function renderStatus(data) {
    var openDetails = captureOpenDetails();
    renderOverview(data);
    renderReadiness(data);
    document.getElementById('system-panel').innerHTML =
      renderProcesses(data) + renderMaintenance(data) + renderResidency(data);
    var sourcesHtml = '';
    (data.sources || []).forEach(function (source) { sourcesHtml += renderSource(source); });
    document.getElementById('sources-panel').innerHTML =
      sourcesHtml || '<div class="card"><p class="meta">尚未配置数据源,请先到「配置」页完成首次配置。</p></div>';
    restoreOpenDetails(openDetails);
    // 不就绪时把"系统与维护"顶到前台,让阻断项直接可见;用户手动切回后不再强制
    var ready = (data.readiness || {}).ready !== false;
    if (!ready && !autoSwitchedToSystem) {
      autoSwitchedToSystem = true;
      if (typeof activateTabGroup === 'function') activateTabGroup('status', 'status-tab-system');
    } else if (ready) {
      autoSwitchedToSystem = false;
    }
  }

  async function loadStatus() {
    try {
      var data = await apiJson('/api/status', { timeoutMs: 10000 });
      latestStatus = data; lastStatusSuccessAt = new Date().toISOString();
      renderStatus(data);
      return true;
    } catch (error) {
      var staleNote = lastStatusSuccessAt ? ';最近成功刷新:' + fmtTime(lastStatusSuccessAt) : '';
      renderState(document.getElementById('sources-panel'), 'error', {
        message: error.message + staleNote, retry: loadStatus
      });
      renderState(document.getElementById('system-panel'), 'error', {
        message: error.message + staleNote, retry: loadStatus
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

  /* 手动触发动作已迁移至「操作」页(middle-actions.js),本页只读观测。 */

  /* 页签切换由共享 initTabs 接管(点击 + 键盘),见 admin.js。 */

  /* 关注项忽略/恢复(事件委托,渲染后持续有效) */
  document.getElementById('overview-panel').addEventListener('click', function (event) {
    var btn = event.target.closest('.issue-dismiss');
    if (btn) {
      var list = loadDismissed();
      if (list.indexOf(btn.dataset.key) < 0) list.push(btn.dataset.key);
      saveDismissed(list);
      if (latestStatus) renderOverview(latestStatus);
      return;
    }
    var restore = event.target.closest('.issue-restore');
    if (restore) {
      event.preventDefault();
      saveDismissed([]);
      if (latestStatus) renderOverview(latestStatus);
    }
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
