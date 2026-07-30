/* data2agent 管理界面共享前端工具(中间端 / 平台端 admin 页面通用)。
   由 layout.html 在 htmx 之后引入,各页面模板不再各自复制这些函数。 */

/** HTML 转义,防注入;null/undefined 按空串处理。 */
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** 带 Token 的请求头(管理 API 均需 Bearer)。 */
function authHeaders() {
  var h = { 'Content-Type': 'application/json' };
  var token = sessionStorage.getItem('d2a_token');
  if (token) h['Authorization'] = 'Bearer ' + token;
  return h;
}

/** ISO 时间 → 本地化显示;空值为 —。 */
function fmtTime(t) {
  if (!t) return '—';
  try { return new Date(t).toLocaleString(); } catch (e) { return t; }
}

/** 完成时间 → 相对时长(如 5m 前);空/未来返回空串。 */
function fmtAge(finished) {
  if (!finished) return '';
  try {
    var ms = Date.now() - new Date(finished).getTime();
    if (ms < 0) return '';
    var s = Math.floor(ms / 1000);
    if (s < 60) return s + 's 前';
    if (s < 3600) return Math.floor(s / 60) + 'm 前';
    return Math.floor(s / 3600) + 'h 前';
  } catch (e) { return ''; }
}

/** 状态 → 彩色徽章 HTML(ok/failed/running/paused 等)。 */
function badge(status) {
  var cls = status === 'ok' || status === 'completed' ? 'badge-ok'
    : status === 'failed' ? 'badge-off'
    : status === 'running' ? 'badge-running'
    : status === 'paused' || status === 'started' ? 'badge-warn'
    : 'badge-off';
  return '<span class="badge ' + cls + '">' + esc(status) + '</span>';
}

/** 把 API 错误体格式化为「详情 — 建议:…」文案。 */
function formatApiError(body, fallback) {
  if (!body) return fallback || '请求失败';
  if (body.errors && body.errors.length) {
    return body.errors.map(function (e) {
      var m = (e.field ? e.field + ': ' : '') + (e.message || '');
      return e.suggestion ? (m + ' — 建议:' + e.suggestion) : m;
    }).join('\n') || fallback || '请求失败';
  }
  var d = body.detail;
  var detail = null;
  var suggestion = body.suggestion || body.error_suggestion || null;
  if (typeof d === 'string') {
    detail = d;
  } else if (d && typeof d === 'object') {
    detail = d.detail || d.message || null;
    suggestion = suggestion || d.suggestion || null;
  }
  if (!detail) {
    detail = body.message || body.error_detail || body.text || body.error || fallback || '请求失败';
  }
  return suggestion ? (detail + ' — 建议:' + suggestion) : detail;
}
