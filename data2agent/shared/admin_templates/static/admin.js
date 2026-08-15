/* data2agent 中间机/平台轻量管理页共享工具。禁止在页面脚本中直接 fetch。 */
(function (global) {
  'use strict';

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function authHeaders(extra) {
    var headers = Object.assign({ 'Content-Type': 'application/json' }, extra || {});
    var token = sessionStorage.getItem('d2a_token');
    if (token) headers.Authorization = 'Bearer ' + token;
    return headers;
  }

  function ApiError(message, status, body, code) {
    this.name = 'ApiError';
    this.message = message || '请求失败';
    this.status = status || 0;
    this.body = body || null;
    this.code = code || null;
    this.retryable = !status || status === 408 || status === 425 || status === 429 || status >= 500;
    if (Error.captureStackTrace) Error.captureStackTrace(this, ApiError);
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function formatApiError(body, fallback) {
    if (!body) return fallback || '请求失败';
    if (body.errors && body.errors.length) {
      return body.errors.map(function (item) {
        var message = (item.field ? item.field + ': ' : '') + (item.message || '');
        return item.suggestion ? message + ' — 建议：' + item.suggestion : message;
      }).join('\n') || fallback || '请求失败';
    }
    var detail = body.detail;
    var suggestion = body.suggestion || body.error_suggestion || null;
    if (detail && typeof detail === 'object') {
      suggestion = suggestion || detail.suggestion;
      detail = detail.detail || detail.message;
    }
    detail = detail || body.message || body.error_detail || body.text || body.error || fallback || '请求失败';
    return suggestion ? detail + ' — 建议：' + suggestion : String(detail);
  }

  function showLogin(message) {
    var panel = document.getElementById('login-panel');
    var content = document.getElementById('page-content');
    var error = document.getElementById('login-error');
    if (panel) panel.hidden = false;
    if (content) content.hidden = true;
    if (error) {
      error.textContent = message || '';
      error.hidden = !message;
    }
    var input = document.getElementById('d2a-token-input');
    if (input) setTimeout(function () { input.focus(); }, 0);
  }

  function hideLogin() {
    var panel = document.getElementById('login-panel');
    var content = document.getElementById('page-content');
    if (panel) panel.hidden = true;
    if (content) content.hidden = false;
  }

  function handleUnauthorized(message) {
    sessionStorage.removeItem('d2a_token');
    if (location && location.pathname) {
      sessionStorage.setItem('d2a_return_path', location.pathname + location.search);
    }
    showLogin(message || '登录已失效，请重新登录。');
    document.dispatchEvent(new CustomEvent('d2a:unauthorized'));
  }

  async function apiFetch(input, init) {
    init = Object.assign({}, init || {});
    var timeoutMs = Number(init.timeoutMs || 15000);
    delete init.timeoutMs;
    init.headers = authHeaders(init.headers || {});
    var controller = new AbortController();
    var externalSignal = init.signal;
    init.signal = controller.signal;
    var timer = setTimeout(function () { controller.abort(); }, timeoutMs);
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort();
      else externalSignal.addEventListener('abort', function () { controller.abort(); }, { once: true });
    }
    var response;
    try {
      response = await fetch(input, init);
    } catch (error) {
      clearTimeout(timer);
      if (error && error.name === 'AbortError') {
        throw new ApiError('请求超时（' + Math.round(timeoutMs / 1000) + ' 秒）', 0, null, 'timeout');
      }
      throw new ApiError('管理 API 不可达：' + (error && error.message ? error.message : error), 0, null, 'network');
    }
    clearTimeout(timer);
    if (!response.ok) {
      var body = null;
      try { body = await response.clone().json(); } catch (_error) {
        try { body = { detail: await response.clone().text() }; } catch (_ignored) { body = null; }
      }
      var message = formatApiError(body, '请求失败（HTTP ' + response.status + '）');
      if (response.status === 401) handleUnauthorized(message);
      throw new ApiError(message, response.status, body,
        body && (body.error_code || body.code || (body.detail && body.detail.code)));
    }
    global.d2aLastApiSuccessAt = new Date().toISOString();
    return response;
  }

  async function apiJson(input, init) {
    var response = await apiFetch(input, init);
    try { return await response.json(); }
    catch (_error) { throw new ApiError('API 返回的不是有效 JSON', response.status, null, 'invalid_json'); }
  }

  function fmtTime(value) {
    if (!value) return '—';
    try {
      var date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString('zh-CN', { hour12: false }) + ' (' + fmtAge(value) + ')';
    } catch (_error) { return String(value); }
  }

  function fmtAge(value) {
    if (!value) return '未知';
    var ms = Date.now() - new Date(value).getTime();
    if (!Number.isFinite(ms)) return '未知';
    if (ms < -1000) return '未来 ' + fmtDuration(-ms / 1000);
    var seconds = Math.max(0, Math.floor(ms / 1000));
    return fmtDuration(seconds) + '前';
  }

  function fmtDuration(seconds) {
    if (seconds == null || seconds === '') return '—';
    seconds = Number(seconds);
    if (!Number.isFinite(seconds)) return '—';
    if (seconds < 60) return Math.round(seconds) + ' 秒';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' 小时';
    return Math.floor(seconds / 86400) + ' 天';
  }

  function fmtBytes(bytes) {
    if (bytes == null || bytes === '') return '—';
    bytes = Number(bytes);
    if (!Number.isFinite(bytes)) return '—';
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    var index = 0;
    while (Math.abs(bytes) >= 1024 && index < units.length - 1) {
      bytes /= 1024; index += 1;
    }
    return bytes.toLocaleString('zh-CN', { maximumFractionDigits: index ? 2 : 0 }) + ' ' + units[index];
  }

  function fmtNumber(value) {
    if (value == null || value === '') return '—';
    var number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString('zh-CN') : '—';
  }

  var STATUS_LABELS = {
    ok: '成功', completed: '已完成', failed: '失败', running: '运行中',
    paused: '已暂停', started: '已启动', partial: '部分失败', unknown: '未知',
    fresh: '新鲜', overdue: '已过期', configured: '已配置', pass: '通过',
    warning: '警告', circuit_open: '已熔断', restarting: '重启中', stopped: '已停止',
    retrying: '正在退避重试'
  };

  function badge(status, label) {
    var good = ['ok', 'completed', 'fresh', 'pass', 'running', 'configured'].indexOf(status) >= 0;
    var warning = ['paused', 'started', 'partial', 'warning', 'restarting', 'unknown'].indexOf(status) >= 0;
    var css = good ? 'badge-ok' : warning ? 'badge-warn' : 'badge-off';
    return '<span class="badge ' + css + '">' + esc(label || STATUS_LABELS[status] || status) + '</span>';
  }

  function announce(kind, message, options) {
    options = options || {};
    var region = document.getElementById('notification-region');
    if (!region) return;
    var node = document.createElement('div');
    node.className = 'notice notice-' + (kind || 'info');
    node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    node.textContent = message;
    region.appendChild(node);
    if (!options.persistent) setTimeout(function () { node.remove(); }, options.timeout || 6000);
  }

  function renderState(target, state, options) {
    options = options || {};
    var node = typeof target === 'string' ? document.getElementById(target) : target;
    if (!node) return;
    node.dataset.state = state;
    if (state === 'loading') node.innerHTML = '<p class="state state-loading">正在加载…</p>';
    else if (state === 'empty') node.innerHTML = '<p class="state state-empty">' + esc(options.message || '暂无数据') + '</p>';
    else if (state === 'error') {
      node.innerHTML = '<div class="state state-error" role="alert"><p>' + esc(options.message || '加载失败') + '</p>' +
        (options.retry ? '<button type="button" class="btn-ghost state-retry">重试</button>' : '') + '</div>';
      if (options.retry) node.querySelector('.state-retry').addEventListener('click', options.retry);
    }
  }

  async function runAction(button, work, options) {
    options = options || {};
    if (button && button.disabled) return null;
    var original = button ? button.textContent : '';
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); button.textContent = options.busyLabel || '执行中…'; }
    try {
      var result = await work();
      if (options.successMessage) announce('success', options.successMessage);
      return result;
    } catch (error) {
      announce('error', error.message || String(error), { persistent: true });
      throw error;
    } finally {
      if (button) { button.disabled = false; button.removeAttribute('aria-busy'); button.textContent = original; }
    }
  }

  var activeModal = null;
  var modalReturnFocus = null;
  var globalAlertTimer = null;
  var globalAlertFailures = 0;
  function openModal(element) {
    activeModal = typeof element === 'string' ? document.getElementById(element) : element;
    if (!activeModal) return;
    modalReturnFocus = document.activeElement;
    activeModal.hidden = false;
    activeModal.style.display = 'flex';
    activeModal.setAttribute('aria-modal', 'true');
    var focusable = activeModal.querySelector('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])');
    if (focusable) focusable.focus();
  }
  function closeModal(element) {
    var modal = element ? (typeof element === 'string' ? document.getElementById(element) : element) : activeModal;
    if (!modal) return;
    modal.hidden = true; modal.style.display = 'none'; modal.removeAttribute('aria-modal');
    activeModal = null;
    if (modalReturnFocus && modalReturnFocus.focus) modalReturnFocus.focus();
    modalReturnFocus = null;
  }

  async function copyText(text) {
    await navigator.clipboard.writeText(String(text));
    announce('success', '已复制到剪贴板');
  }

  /* 页签组的统一点击切换:各页面只需标准 data-tabs 标记即可获得完整
     交互(点击 + 键盘),不再需要每页重复接线。切换后派发
     d2a:tab-activated 事件供页面附加逻辑(如配置页的保存按钮显隐)。 */
  function activateTabGroup(group, panelId) {
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
    document.dispatchEvent(new CustomEvent('d2a:tab-activated', {
      detail: { group: group, panelId: panelId }
    }));
  }

  function initTabs(root) {
    (root || document).querySelectorAll('[role="tablist"]').forEach(function (list) {
      var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
      tabs.forEach(function (tab, index) {
        // data-tabs 组由共享层接管点击;data-sub 等页面自有机制不受影响
        if (tab.dataset.tabs && !tab.dataset.sharedBound) {
          tab.dataset.sharedBound = '1';
          tab.addEventListener('click', function () {
            activateTabGroup(tab.dataset.tabs, tab.dataset.tab);
          });
        }
        if (tab.dataset.kbBound) return;
        tab.dataset.kbBound = '1';
        tab.addEventListener('keydown', function (event) {
          var next = null;
          if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
          if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
          if (event.key === 'Home') next = 0;
          if (event.key === 'End') next = tabs.length - 1;
          if (next != null) { event.preventDefault(); tabs[next].focus(); tabs[next].click(); }
        });
      });
    });
  }

  async function refreshGlobalAlerts() {
    if (document.body.dataset.needsSetup === 'true') return true;
    if (document.body.dataset.needsToken === 'true' && !sessionStorage.getItem('d2a_token')) return true;
    var banner = document.getElementById('global-alert-banner');
    if (!banner) return true;
    // 状态页的总览横幅已聚合相同内容(就绪度/进程/源健康),
    // 全局横幅在此页抑制,避免同一异常两处重复展示。
    if (document.body.dataset.page === 'status') { banner.hidden = true; return true; }
    try {
      var status = await apiJson('/api/alerts', { timeoutMs: 10000 });
      var alerts = (status.alerts || []).filter(function (alert) {
        return alert.status === 'active' && !alert.silenced_until &&
          ['critical', 'error'].indexOf(alert.severity) >= 0;
      });
      var recovered = globalAlertFailures > 0;
      globalAlertFailures = 0;
      if (recovered) announce('success', '全局状态检查已恢复');
      if (!alerts.length) { banner.hidden = true; banner.innerHTML = ''; return true; }
      banner.hidden = false;
      banner.innerHTML = '<strong>需要处理的严重状态（' + alerts.length + '）</strong><ul>' +
        alerts.slice(0, 5).map(function (alert) {
          return '<li>' + esc(alert.title) + (alert.suggestion ? ' — ' + esc(alert.suggestion) : '') + '</li>';
        }).join('') + '</ul><a href="/status">查看状态</a> · <a href="/logs">查看日志</a>';
      banner.dataset.lastSuccessAt = new Date().toISOString();
      return true;
    } catch (error) {
      globalAlertFailures += 1;
      if (error.status === 401) return false;
      banner.hidden = false;
      banner.innerHTML = '<strong>管理状态刷新失败</strong> — ' + esc(error.message) +
        (banner.dataset.lastSuccessAt ? '；最近成功：' + esc(fmtTime(banner.dataset.lastSuccessAt)) : '');
      return false;
    }
  }

  function scheduleGlobalAlerts() {
    clearTimeout(globalAlertTimer);
    var delay = document.hidden ? 60000 : Math.min(
      120000, 15000 * Math.pow(2, Math.min(3, globalAlertFailures)));
    globalAlertTimer = setTimeout(async function () {
      if (!document.hidden) await refreshGlobalAlerts();
      scheduleGlobalAlerts();
    }, delay);
  }

  function initShell() {
    document.body.addEventListener('htmx:configRequest', function (event) {
      var token = sessionStorage.getItem('d2a_token');
      if (token) event.detail.headers.Authorization = 'Bearer ' + token;
    });
    document.body.addEventListener('htmx:responseError', function (event) {
      if (event.detail.xhr.status === 401) handleUnauthorized('登录已失效，请重新登录。');
      else announce('error', '请求失败（HTTP ' + event.detail.xhr.status + '）', { persistent: true });
    });

    var path = location.pathname;
    document.querySelectorAll('nav a[data-nav]').forEach(function (link) {
      if (link.getAttribute('data-nav') === path) {
        link.classList.add('active'); link.setAttribute('aria-current', 'page');
      }
    });

    var needsToken = document.body.dataset.needsToken === 'true';
    if (needsToken && !sessionStorage.getItem('d2a_token')) showLogin();
    else hideLogin();

    var form = document.getElementById('token-form');
    if (form) form.addEventListener('submit', async function (event) {
      event.preventDefault();
      var input = document.getElementById('d2a-token-input');
      var submit = form.querySelector('button[type="submit"]');
      var token = input.value.trim();
      if (!token) return;
      sessionStorage.setItem('d2a_token', token);
      try {
        await runAction(submit, function () { return apiJson('/api/auth/check'); }, { busyLabel: '验证中…' });
        hideLogin();
        var returnPath = sessionStorage.getItem('d2a_return_path');
        sessionStorage.removeItem('d2a_return_path');
        location.assign(returnPath || location.href);
      } catch (_error) {
        sessionStorage.removeItem('d2a_token');
        showLogin('登录密码无效，请重试。');
      }
    });

    var logout = document.getElementById('logout-button');
    if (logout) logout.addEventListener('click', function () {
      sessionStorage.removeItem('d2a_token');
      sessionStorage.setItem('d2a_return_path', location.pathname + location.search);
      showLogin('已退出管理界面。');
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && activeModal) closeModal(activeModal);
      if (event.key === 'Tab' && activeModal) {
        var nodes = activeModal.querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])');
        if (!nodes.length) return;
        var first = nodes[0], last = nodes[nodes.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    initTabs(document);
    refreshGlobalAlerts().finally(scheduleGlobalAlerts);
    document.addEventListener('visibilitychange', scheduleGlobalAlerts);
  }

  /* 共享确认弹窗:替代原生 confirm(风格统一、可访问性内建)。
     返回 Promise<boolean>;调用方: if (await confirmDialog('…')) { … } */
  function confirmDialog(message, options) {
    options = options || {};
    return new Promise(function (resolve) {
      var overlay = document.createElement('div');
      overlay.className = 'modal-overlay';
      overlay.innerHTML =
        '<div class="modal-card" role="dialog" aria-modal="true" aria-label="确认操作" style="max-width:26rem">' +
        '<div class="modal-header"><h2>' + esc(options.title || '确认操作') + '</h2></div>' +
        '<p class="meta" style="margin:0 0 16px">' + esc(message) + '</p>' +
        '<div class="actions"><button type="button" data-act="ok">' + esc(options.okLabel || '确认') + '</button>' +
        '<button type="button" data-act="cancel" class="btn-ghost">取消</button></div></div>';
      function done(value) {
        document.removeEventListener('keydown', onKey);
        overlay.remove();
        resolve(value);
      }
      function onKey(event) {
        if (event.key === 'Escape') { event.preventDefault(); done(false); }
        if (event.key === 'Enter') { event.preventDefault(); done(true); }
      }
      overlay.addEventListener('click', function (event) {
        if (event.target === overlay) return done(false);
        var act = event.target.closest('[data-act]');
        if (act) done(act.dataset.act === 'ok');
      });
      document.addEventListener('keydown', onKey);
      document.body.appendChild(overlay);
      overlay.hidden = false;
      var okBtn = overlay.querySelector('[data-act="ok"]');
      if (okBtn) okBtn.focus();
    });
  }

  Object.assign(global, {
    esc: esc, authHeaders: authHeaders, ApiError: ApiError,
    apiFetch: apiFetch, apiJson: apiJson, formatApiError: formatApiError,
    fmtTime: fmtTime, fmtAge: fmtAge, fmtDuration: fmtDuration,
    fmtBytes: fmtBytes, fmtNumber: fmtNumber, badge: badge,
    announce: announce, renderState: renderState, runAction: runAction,
    openModal: openModal, closeModal: closeModal, copyText: copyText,
    initTabs: initTabs, activateTabGroup: activateTabGroup, refreshGlobalAlerts: refreshGlobalAlerts,
    confirmDialog: confirmDialog,
    handleUnauthorized: handleUnauthorized
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initShell);
  else initShell();
})(window);
