/* 中间机配置页：白名单编辑、即时校验、diff、revision 冲突与重启语义。 */
(function () {
  'use strict';

  var needsSetup = document.body.dataset.needsSetup === 'true';
  var sourcesEl = document.getElementById('sources-container');
  var currentRevision = null;
  var currentConfig = null;
  var conflictRevision = null;

  function byId(id) { return document.getElementById(id); }
  function valueOrNull(value) { value = String(value == null ? '' : value).trim(); return value || null; }
  function selected(value, expected) { return value === expected ? ' selected' : ''; }
  function checked(value) { return value ? ' checked' : ''; }

  function field(label, hint, input) {
    return '<label class="field"><span class="field-label">' + label + '</span>' + input +
      (hint ? '<span class="field-hint">' + hint + '</span>' : '') + '</label>';
  }

  function activateTab(group, panelId) {
    document.querySelectorAll('[role="tab"][data-tabs="' + group + '"]').forEach(function (tab) {
      var active = tab.dataset.tab === panelId;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('[role="tabpanel"][data-tabs="' + group + '"]').forEach(function (panel) {
      var active = panel.id === panelId;
      panel.classList.toggle('active', active);
      panel.hidden = !active;
    });
    if (group === 'setup') syncSetupNavigation();
    if (group === 'daily') syncSaveActions(panelId);
  }

  function setupPanels() { return ['setup-push', 'setup-erp', 'setup-local']; }
  function syncSetupNavigation() {
    var ids = setupPanels();
    var active = document.querySelector('[role="tabpanel"][data-tabs="setup"].active');
    var index = active ? ids.indexOf(active.id) : 0;
    var previous = document.querySelector('.tab-nav[data-dir="-1"]');
    var next = document.querySelector('.tab-nav[data-dir="1"]');
    if (previous) previous.hidden = index <= 0;
    if (next) next.hidden = index >= ids.length - 1;
  }

  function syncSaveActions(panelId) {
    var actions = byId('daily-save-actions');
    if (actions) actions.hidden = panelId !== 'daily-paths' && panelId !== 'daily-source';
  }

  function activateSubtab(block, panelId) {
    block.querySelectorAll('[role="tab"]').forEach(function (tab) {
      var active = tab.dataset.sub === panelId;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
    });
    block.querySelectorAll('[role="tabpanel"]').forEach(function (panel) {
      var active = panel.dataset.sub === panelId;
      panel.classList.toggle('active', active);
      panel.hidden = !active;
    });
  }

  function renderSource(name, source) {
    var id = 'source-' + name.replace(/[^a-zA-Z0-9_-]/g, '_');
    var rate = source.rate || {};
    var sink = source.sink || {};
    var spool = source.spool || {};
    var windows = (source.windows || []).join(', ');
    var production = currentConfig && currentConfig.deployment_mode === 'production';
    var sinkState = production && sink.type !== 'http'
      ? '<p class="banner danger"><strong>生产阻断：</strong>该数据源不是 HTTP sink，connector 将拒绝启动。</p>'
      : '<p class="oknote">' + (sink.type === 'http' ? 'HTTP 推送已配置' : '开发/测试本地模式') + '</p>';
    return '<article class="source-block" data-source="' + esc(name) + '">' +
      '<h3 class="source-title">数据源：' + esc(name) + '</h3>' + sinkState +
      '<p class="source-lead">抽取、推送、对账和告警均绑定此 source，不会隐式操作其他数据源。</p>' +
      '<div class="subtabs" role="tablist" aria-label="' + esc(name) + ' 配置分组">' +
        '<button type="button" class="subtab active" id="' + id + '-tab-pace" role="tab" aria-selected="true" aria-controls="' + id + '-pace" tabindex="0" data-sub="' + id + '-pace">抽取与对账</button>' +
        '<button type="button" class="subtab" id="' + id + '-tab-rate" role="tab" aria-selected="false" aria-controls="' + id + '-rate" tabindex="-1" data-sub="' + id + '-rate">资源保护</button>' +
        '<button type="button" class="subtab" id="' + id + '-tab-sink" role="tab" aria-selected="false" aria-controls="' + id + '-sink" tabindex="-1" data-sub="' + id + '-sink">推送/TLS</button>' +
        '<button type="button" class="subtab" id="' + id + '-tab-spool" role="tab" aria-selected="false" aria-controls="' + id + '-spool" tabindex="-1" data-sub="' + id + '-spool">临时数据策略</button>' +
        '<button type="button" class="subtab" id="' + id + '-tab-cred" role="tab" aria-selected="false" aria-controls="' + id + '-cred" tabindex="-1" data-sub="' + id + '-cred">敏感配置状态</button>' +
      '</div>' +
      '<section id="' + id + '-pace" class="subpanel active" role="tabpanel" aria-labelledby="' + id + '-tab-pace" data-sub="' + id + '-pace"><div class="group-grid">' +
        field('抽取间隔', '支持 30s / 30m / 1h / 1d，至少 1 秒。', '<input type="text" data-key="sync_every" value="' + esc(source.sync_every || '') + '" required>') +
        field('首轮启动时间（可空）', 'HH:MM；留空表示服务启动后立即进入调度判断。', '<input type="time" data-key="sync_start_at" value="' + esc(source.sync_start_at || '') + '">') +
        field('全局抽取开始日期（可空）', '表级日期优先；不会抽取更早历史。', '<input type="date" data-key="start_date" value="' + esc(source.start_date || '') + '">') +
        field('错峰窗口（可空）', '多个窗口以逗号分隔，例如 22:00-06:00, 12:00-13:00。', '<input type="text" data-key="windows" value="' + esc(windows) + '">') +
        field('回看窗口', '用于覆盖迟到更新，例如 3d。', '<input type="text" data-key="lookback" value="' + esc(source.lookback || '') + '" required>') +
        field('每日 L1 对账', 'HH:MM；留空禁用自动 L1。', '<input type="time" data-key="reconcile_at" value="' + esc(source.reconcile_at || '') + '">') +
        field('L2 深度对账', '低峰执行；深度对账可能重抽并识别物理删除。', '<input type="time" data-key="reconcile_deep_at" value="' + esc(source.reconcile_deep_at || '') + '">') +
        field('L2 执行星期', '未配置 L2 时间时会自动清空。', '<select data-key="reconcile_deep_day_of_week"><option value="">每天/未设置</option>' +
          ['mon','tue','wed','thu','fri','sat','sun'].map(function (day) { return '<option value="' + day + '"' + selected(source.reconcile_deep_day_of_week, day) + '>' + day + '</option>'; }).join('') + '</select>') +
      '</div></section>' +
      '<section id="' + id + '-rate" class="subpanel" role="tabpanel" aria-labelledby="' + id + '-tab-rate" data-sub="' + id + '-rate" hidden><div class="group-grid">' +
        field('每批行数', '范围 1–50,000。', '<input type="number" min="1" max="50000" data-key="rate.batch_size" value="' + esc(rate.batch_size) + '" required>') +
        field('限速（行/秒）', '范围 1–1,000,000。', '<input type="number" min="1" max="1000000" data-key="rate.rows_per_second" value="' + esc(rate.rows_per_second) + '" required>') +
      '</div></section>' +
      '<section id="' + id + '-sink" class="subpanel" role="tabpanel" aria-labelledby="' + id + '-tab-sink" data-sub="' + id + '-sink" hidden><div class="group-grid">' +
        field('平台接收地址', '生产环境必须为 HTTP sink；推荐 HTTPS。', '<input type="url" data-key="sink.url" value="' + esc(sink.url || '') + '" required>') +
        field('请求超时（秒）', '范围 1–600，推荐 30。', '<input type="number" min="1" max="600" step="0.5" data-key="sink.timeout_seconds" value="' + esc(sink.timeout_seconds) + '" required>') +
        field('最多尝试次数', '包含首次请求，范围 1–10。', '<input type="number" min="1" max="10" data-key="sink.retries" value="' + esc(sink.retries) + '" required>') +
        field('私有 CA bundle（可空）', 'PEM 路径；主机名校验始终启用，不提供关闭证书校验选项。', '<input type="text" data-key="sink.ca_bundle" value="' + esc(sink.ca_bundle || '') + '">') +
        field('受控内网 HTTP 例外', '仅在隔离内网并完成风险评估时启用。', '<span><input type="checkbox" data-key="sink.allow_insecure_http"' + checked(sink.allow_insecure_http) + '>允许明文 HTTP</span>') +
      '</div></section>' +
      '<section id="' + id + '-spool" class="subpanel" role="tabpanel" aria-labelledby="' + id + '-tab-spool" data-sub="' + id + '-spool" hidden><div class="group-grid">' +
        field('全量 spool 策略', '生产只允许严格流式或经确认的加密临时卷；temporary_file 仅开发/测试。', '<select data-key="spool.policy"><option value="strict_stream"' + selected(spool.policy, 'strict_stream') + '>strict_stream（磁盘 spool 禁用）</option><option value="encrypted_temp_volume"' + selected(spool.policy, 'encrypted_temp_volume') + '>encrypted_temp_volume</option><option value="temporary_file"' + selected(spool.policy, 'temporary_file') + '>temporary_file（仅开发/测试）</option></select>') +
        field('加密临时卷目录（可空）', '仅 encrypted_temp_volume 使用；目录须受最小权限保护。', '<input type="text" data-key="spool.directory" value="' + esc(spool.directory || '') + '">') +
        field('静态加密已现场确认', '选择加密临时卷时必须勾选。', '<span><input type="checkbox" data-key="spool.encrypted_at_rest"' + checked(spool.encrypted_at_rest) + '>已确认该卷静态加密</span>') +
      '</div></section>' +
      '<section id="' + id + '-cred" class="subpanel" role="tabpanel" aria-labelledby="' + id + '-tab-cred" data-sub="' + id + '-cred" hidden>' +
        '<p class="panel-lead">仅展示是否配置；不返回 DSN、Token 或 CA 内容。</p><div class="cred-box">' +
          '<div class="cred-item"><strong>ERP DSN 环境变量</strong>' + esc(source.dsn_env || '未命名') + ' · ' + (source.dsn_env_set ? '已配置' : '未配置') + '</div>' +
          '<div class="cred-item"><strong>推送 Token 环境变量</strong>' + esc(sink.token_env || '未命名') + ' · ' + (sink.token_env_set ? '已配置' : '未配置') + '</div>' +
          '<div class="cred-item"><strong>私有 CA</strong>' + (sink.ca_bundle_configured ? '已配置路径' : '未配置（使用系统信任库）') + '</div>' +
          '<div class="cred-item"><strong>secrets 最近修改</strong>' + fmtTime(currentConfig.sensitive_config && currentConfig.sensitive_config.updated_at) + '</div>' +
        '</div></section>' +
      '</article>';
  }

  function bindSourceControls() {
    sourcesEl.querySelectorAll('.source-block').forEach(function (block) {
      block.querySelectorAll('[role="tab"]').forEach(function (tab) {
        tab.addEventListener('click', function () { activateSubtab(block, tab.dataset.sub); });
      });
      block.querySelectorAll('input,select').forEach(function (input) {
        input.addEventListener('input', updateDiff);
        input.addEventListener('change', updateDiff);
      });
      initTabs(block);
    });
  }

  function setPath(root, path, value) {
    var parts = path.split('.');
    var cursor = root;
    parts.slice(0, -1).forEach(function (part) { cursor = cursor[part] = cursor[part] || {}; });
    cursor[parts[parts.length - 1]] = value;
  }

  function collectPatch() {
    var sources = {};
    sourcesEl.querySelectorAll('.source-block').forEach(function (block) {
      var source = {};
      block.querySelectorAll('[data-key]').forEach(function (input) {
        var key = input.dataset.key;
        var value = input.type === 'checkbox' ? input.checked : input.value;
        if (key === 'windows') value = value ? value.split(',').map(function (item) { return item.trim(); }).filter(Boolean) : [];
        else if (input.type === 'number') value = value === '' ? null : Number(value);
        else if (['sync_start_at','start_date','reconcile_at','reconcile_deep_at','reconcile_deep_day_of_week','sink.ca_bundle','spool.directory'].indexOf(key) >= 0) value = valueOrNull(value);
        setPath(source, key, value);
      });
      if (!source.reconcile_deep_at) source.reconcile_deep_day_of_week = null;
      if (source.spool && source.spool.policy === 'strict_stream') {
        source.spool.directory = null;
        source.spool.encrypted_at_rest = false;
      }
      sources[block.dataset.source] = source;
    });
    return { templates: byId('cfg-templates').value.trim(), state_db: byId('cfg-state-db').value.trim(), sources: sources };
  }

  function getPath(root, path) {
    return path.split('.').reduce(function (value, part) { return value && value[part]; }, root);
  }
  function flatten(root, prefix, output) {
    output = output || {};
    Object.keys(root || {}).forEach(function (key) {
      var path = prefix ? prefix + '.' + key : key;
      var value = root[key];
      if (value && typeof value === 'object' && !Array.isArray(value)) flatten(value, path, output);
      else output[path] = value;
    });
    return output;
  }
  function printable(value) {
    if (value == null || value === '') return '（空）';
    return Array.isArray(value) ? value.join(', ') || '（空）' : String(value);
  }

  function updateDiff() {
    if (!currentConfig || needsSetup) return;
    var flat = flatten(collectPatch());
    var rows = Object.keys(flat).filter(function (path) {
      return JSON.stringify(flat[path]) !== JSON.stringify(getPath(currentConfig, path));
    }).map(function (path) {
      return '<tr><td><code>' + esc(path) + '</code></td><td><code>' + esc(printable(getPath(currentConfig, path))) + '</code></td><td><code>' + esc(printable(flat[path])) + '</code></td></tr>';
    });
    byId('config-diff-body').innerHTML = rows.length
      ? '<div class="table-scroll"><table class="data diff-table"><thead><tr><th>字段</th><th>当前服务器值</th><th>将保存</th></tr></thead><tbody>' + rows.join('') + '</tbody></table></div>'
      : '<p>当前没有待保存改动。</p>';
  }

  function validatePatch(patch) {
    var errors = [];
    var duration = /^\d+(?:\.\d+)?[smhd]$/;
    var time = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
    Object.keys(patch.sources).forEach(function (name) {
      var source = patch.sources[name];
      if (!duration.test(source.sync_every || '')) errors.push(name + '：抽取间隔格式无效');
      if (!duration.test(source.lookback || '')) errors.push(name + '：回看窗口格式无效');
      (source.windows || []).forEach(function (windowValue) {
        var pair = windowValue.split('-');
        if (pair.length !== 2 || !time.test(pair[0]) || !time.test(pair[1]) || pair[0] === pair[1]) errors.push(name + '：错峰窗口无效 ' + windowValue);
      });
      try {
        var parsed = new URL(source.sink.url);
        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') errors.push(name + '：平台地址仅支持 HTTP(S)');
        if (parsed.protocol === 'http:' && !source.sink.allow_insecure_http) errors.push(name + '：HTTP 地址必须显式确认受控内网例外，或改用 HTTPS');
      } catch (_error) { errors.push(name + '：平台接收地址不是有效 URL'); }
      if (source.spool.policy === 'encrypted_temp_volume' && (!source.spool.directory || !source.spool.encrypted_at_rest)) errors.push(name + '：加密临时卷必须填写目录并确认静态加密');
      if (currentConfig.deployment_mode === 'production' && source.spool.policy === 'temporary_file') errors.push(name + '：生产环境不允许 temporary_file spool');
    });
    if (!patch.templates) errors.push('模板目录不能为空');
    if (!patch.state_db) errors.push('中间机状态库路径不能为空');
    return errors;
  }

  function populateSourceSelectors() {
    var options = Object.keys(currentConfig.sources || {}).map(function (name) { return '<option value="' + esc(name) + '">' + esc(name) + '</option>'; }).join('');
    byId('link-source').innerHTML = options;
    var connectionSource = byId('conn-source');
    if (connectionSource) connectionSource.innerHTML = options;
    updateLinkFields();
  }

  function updateLinkFields() {
    var name = byId('link-source').value;
    var source = currentConfig && currentConfig.sources[name];
    byId('link-url').value = source && source.sink ? source.sink.url || '' : '';
    byId('link-token').value = '';
  }

  async function checkRevisionStatus() {
    try {
      var status = await apiJson('/api/status', { timeoutMs: 10000 });
      var process = status.process_status && status.process_status.connector;
      var running = process && process.loaded_config_revision;
      byId('config-revision-running').textContent = running || '未知';
      if (!running) byId('config-revision-state').innerHTML = badge('unknown', '无法确认 connector 已加载版本');
      else if (running === currentRevision) byId('config-revision-state').innerHTML = badge('pass', '已生效');
      else byId('config-revision-state').innerHTML = badge('warning', '等待 connector 重启加载');
    } catch (error) {
      byId('config-revision-state').innerHTML = badge('unknown', '状态检查失败：' + error.message);
    }
  }

  async function loadDaily(options) {
    options = options || {};
    try {
      var config = await apiJson('/api/config');
      if (config.needs_setup) return;
      currentConfig = config;
      currentRevision = config.revision;
      conflictRevision = null;
      byId('config-conflict').hidden = true;
      byId('deployment-mode').textContent = config.deployment_mode === 'production' ? '生产' : config.deployment_mode;
      byId('config-revision-loaded').textContent = currentRevision || '未知';
      byId('legacy-state-warning').hidden = !config.legacy_landing_key;
      byId('cfg-templates').value = config.templates || '';
      byId('cfg-state-db').value = config.state_db || '';
      sourcesEl.innerHTML = Object.keys(config.sources || {}).map(function (name) { return renderSource(name, config.sources[name]); }).join('') || '<p class="state state-empty">当前配置没有数据源。</p>';
      bindSourceControls();
      populateSourceSelectors();
      updateDiff();
      await checkRevisionStatus();
      if (options.announce) announce('success', '已读取服务器最新配置');
    } catch (error) {
      byId('config-errors').textContent = error.message;
    }
  }

  function restartMessage(response, subject) {
    if (!response.restart_required) return subject + '已生效，无需重启。';
    if (response.restart_automatic) return subject + '已保存；受监管 launcher 将自动重启 connector。';
    return subject + '已保存；当前不是受监管便携启动模式，请人工重启 connector 后生效。';
  }

  function extractCurrentRevision(error) {
    var detail = error.body && error.body.detail;
    return (detail && typeof detail === 'object' && detail.current_revision) || (error.body && error.body.current_revision) || null;
  }

  async function saveConfig(button, revisionOverride) {
    var patch = collectPatch();
    var errors = validatePatch(patch);
    byId('config-errors').textContent = errors.join('\n');
    if (errors.length) { announce('error', '配置有 ' + errors.length + ' 个校验问题'); return; }
    await runAction(button, async function () {
      var validation = await apiJson('/api/config/validate', { method: 'POST', body: JSON.stringify(patch) });
      if (!validation.ok) throw new ApiError(formatApiError(validation, '服务端校验失败'), 422, validation, 'validation');
      patch.revision = revisionOverride || currentRevision;
      try {
        var response = await apiJson('/api/config', { method: 'POST', body: JSON.stringify(patch) });
        if (!response.ok) throw new ApiError(formatApiError(response, '保存失败'), 422, response, 'validation');
        currentRevision = response.revision || currentRevision;
        byId('config-revision-loaded').textContent = currentRevision;
        byId('restart-banner').hidden = false;
        byId('restart-banner').textContent = restartMessage(response, '配置');
        announce('success', restartMessage(response, '配置'));
        await loadDaily();
      } catch (error) {
        if (error.status === 409) {
          conflictRevision = extractCurrentRevision(error);
          byId('config-conflict').hidden = false;
          byId('config-conflict-detail').textContent = '页面 revision：' + (currentRevision || '未知') + '；服务器 revision：' + (conflictRevision || '未知') + '。当前编辑内容仍保留。';
          announce('warning', '配置已被其他会话修改，请选择刷新或重新应用。', { persistent: true });
          return;
        }
        throw error;
      }
    }, { busyLabel: '校验并保存中…' });
  }

  async function saveConnection(button) {
    var source = byId('link-source').value;
    var body = { source: source, platform_url: byId('link-url').value.trim(), revision: currentRevision };
    var token = byId('link-token').value.trim();
    if (token) body.ingest_token = token;
    await runAction(button, async function () {
      var response = await apiJson('/api/config/connection', { method: 'POST', body: JSON.stringify(body) });
      if (!response.ok) throw new ApiError(formatApiError(response, '保存失败'), 422, response, 'validation');
      currentRevision = response.revision || currentRevision;
      byId('link-token').value = '';
      byId('link-save-result').textContent = restartMessage(response, source + ' 平台对接信息');
      byId('restart-banner').hidden = false;
      byId('restart-banner').textContent = restartMessage(response, '平台对接信息');
      announce('success', restartMessage(response, '平台对接信息'));
      await loadDaily();
    }, { busyLabel: '保存中…' });
  }

  async function checkPlatform(button) {
    var source = byId('link-source').value;
    byId('link-check-result').textContent = '正在检查…';
    await runAction(button, async function () {
      var result = await apiJson('/api/config/connection-check?source=' + encodeURIComponent(source));
      byId('link-check-result').textContent = result.ok
        ? source + '：平台可达；本机协议 v' + (result.local_protocol || '?') + '；平台支持 ' + (result.platform_supported || []).join(', ') + '；' + (result.compatible ? '兼容' : '不兼容')
        : source + '：' + (result.detail || '检查失败');
      announce(result.ok && result.compatible ? 'success' : 'warning', byId('link-check-result').textContent);
    }, { busyLabel: '检查中…' });
  }

  async function testErp(button) {
    var selector = byId('conn-source');
    var source = selector ? selector.value : null;
    byId('conn-result').textContent = '正在测试…';
    await runAction(button, async function () {
      var result = await apiJson('/api/connection/test', { method: 'POST', body: JSON.stringify({ source: source }) });
      if (result.status === 'failed' || result.error) throw new ApiError(formatApiError(result, 'ERP 连接失败'), 503, result, result.error);
      byId('conn-result').textContent = source + '：' + (result.detail || result.status || '连接成功');
      byId('link-metadata').hidden = false;
      announce('success', 'ERP 连接测试通过');
    }, { busyLabel: '测试中…' });
  }

  function setupValidation(form) {
    var invalid = form.querySelector(':invalid');
    if (invalid) {
      var panel = invalid.closest('[role="tabpanel"]');
      if (panel) activateTab('setup', panel.id);
      invalid.focus();
      return '请检查必填项及字段格式。';
    }
    var duration = form.elements.sync_every.value.trim();
    if (!/^\d+(?:\.\d+)?[smhd]$/.test(duration)) return '抽取间隔格式无效；请使用 30m、1h 等格式。';
    return '';
  }

  async function submitSetup(form, button) {
    var message = setupValidation(form);
    byId('setup-errors').textContent = message;
    if (message) return;
    var body = {};
    new FormData(form).forEach(function (value, key) { body[key] = value; });
    body.erp_port = Number(body.erp_port || 1433);
    await runAction(button, async function () {
      var response = await apiJson('/api/setup', { method: 'POST', body: JSON.stringify(body), timeoutMs: 30000 });
      if (!response.ok) throw new ApiError(formatApiError(response, '首次配置失败'), 422, response, 'validation');
      sessionStorage.setItem('d2a_token', body.admin_token);
      announce('success', response.message || '首次配置已保存');
      location.assign('/metadata?from=config');
    }, { busyLabel: '保存并检查中…' });
  }

  function bindEvents() {
    document.querySelectorAll('[role="tab"][data-tabs]').forEach(function (tab) {
      tab.addEventListener('click', function () { activateTab(tab.dataset.tabs, tab.dataset.tab); });
    });
    document.querySelectorAll('.tab-nav').forEach(function (button) {
      button.addEventListener('click', function () {
        var ids = setupPanels();
        var active = document.querySelector('[role="tabpanel"][data-tabs="setup"].active');
        var next = ids[ids.indexOf(active.id) + Number(button.dataset.dir)];
        if (next) activateTab('setup', next);
      });
    });
    var setupForm = byId('setup-form');
    if (setupForm) setupForm.addEventListener('submit', function (event) { event.preventDefault(); submitSetup(setupForm, setupForm.querySelector('[type="submit"]')).catch(function (error) { byId('setup-errors').textContent = error.message; }); });
    var configForm = byId('config-form');
    if (configForm) configForm.addEventListener('submit', function (event) { event.preventDefault(); saveConfig(byId('btn-config-save')).catch(function (error) { byId('config-errors').textContent = error.message; }); });
    byId('btn-link-save') && byId('btn-link-save').addEventListener('click', function () { saveConnection(this).catch(function (error) { byId('link-save-result').textContent = error.message; }); });
    byId('btn-link-check') && byId('btn-link-check').addEventListener('click', function () { checkPlatform(this).catch(function (error) { byId('link-check-result').textContent = error.message; }); });
    byId('btn-conn-test') && byId('btn-conn-test').addEventListener('click', function () { testErp(this).catch(function (error) { byId('conn-result').textContent = error.message; }); });
    byId('link-source') && byId('link-source').addEventListener('change', updateLinkFields);
    byId('cfg-templates') && byId('cfg-templates').addEventListener('input', updateDiff);
    byId('cfg-state-db') && byId('cfg-state-db').addEventListener('input', updateDiff);
    byId('btn-conflict-reload') && byId('btn-conflict-reload').addEventListener('click', function () { loadDaily({ announce: true }); });
    byId('btn-conflict-reapply') && byId('btn-conflict-reapply').addEventListener('click', function () {
      if (!conflictRevision) { byId('config-errors').textContent = '服务器 revision 未知，请先读取服务器新配置。'; return; }
      saveConfig(byId('btn-conflict-reapply'), conflictRevision).catch(function (error) { byId('config-errors').textContent = error.message; });
    });
  }

  function init() {
    bindEvents();
    initTabs(document);
    if (needsSetup) syncSetupNavigation();
    else { syncSaveActions('daily-conn'); loadDaily(); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
