"""控制台单页(内嵌 HTML,零外部资源;5 秒自动刷新)。"""

UI_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>data2agent 运维控制台</title>
<style>
  body{font-family:system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
       margin:0;background:#f2f4f7;color:#1c2733}
  header{background:#12314f;color:#fff;padding:12px 22px;display:flex;
         justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px}
  header b{font-size:16px}
  #meta{font-size:12px;opacity:.85}
  main{padding:16px 22px;display:grid;gap:14px;max-width:1200px;margin:0 auto}
  section{background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(16,34,54,.08)}
  h2{font-size:13px;margin:0 0 10px;color:#51606f;letter-spacing:.5px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #eef1f4}
  th{color:#6b7885;font-weight:600;white-space:nowrap}
  td{white-space:nowrap}td.wrap{white-space:normal;max-width:480px}
  .badge{display:inline-block;padding:1px 9px;border-radius:10px;font-size:12px}
  .ok{background:#e3f6e8;color:#1b7f37}.failed{background:#fde8e8;color:#b42323}
  .paused{background:#fff4d6;color:#946200}.running{background:#e5efff;color:#1b4f9c}
  button{background:#12314f;color:#fff;border:0;border-radius:6px;padding:6px 13px;
         cursor:pointer;margin:0 8px 6px 0;font-size:13px}
  button.secondary{background:#5b7186}
  button:disabled{opacity:.5;cursor:default}
  #msg{font-size:13px;color:#1b4f9c;margin-left:4px}
  .empty{color:#9aa6b1;font-size:13px;padding:4px 0}
</style>
</head>
<body>
<header><div><b>data2agent</b> 运维控制台</div><div id="meta">加载中…</div></header>
<main>
  <section><h2>动作(受错峰窗口 / 白名单约束,与调度器同一引擎)</h2>
    <div id="actions"><span class="empty">加载中…</span></div><span id="msg"></span>
  </section>
  <section><h2>水位状态</h2><div id="state"></div></section>
  <section><h2>对象层</h2><div id="objects"></div></section>
  <section><h2>最近运行</h2><div id="runs"></div></section>
  <section><h2>隔离区(未处理)</h2><div id="quarantine"></div></section>
  <section><h2>审计日志(发往源库的每条 SQL)</h2><div id="audit"></div></section>
</main>
<script>
const token = localStorage.getItem('d2a_token') || '';
async function j(url, opts = {}) {
  const headers = {'Content-Type': 'application/json'};
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const r = await fetch(url, {...opts, headers});
  if (r.status === 401) {
    const t = prompt('本控制台启用了 Token 认证,请输入 Token:');
    if (t) { localStorage.setItem('d2a_token', t); location.reload(); }
    throw new Error('未授权');
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function table(id, headers, rows) {
  const el = document.getElementById(id);
  if (!rows.length) { el.innerHTML = '<div class="empty">(空)</div>'; return; }
  el.innerHTML = '<table><tr>' + headers.map(h => `<th>${h}</th>`).join('') + '</tr>' +
    rows.map(r => '<tr>' + r.join('') + '</tr>').join('') + '</table>';
}
const td = (v, cls) => `<td${cls ? ` class="${cls}"` : ''}>${v}</td>`;
const badge = s => td(`<span class="badge ${esc(s)}">${esc(s)}</span>`);
let msgTimer;
function msg(text) {
  document.getElementById('msg').textContent = text;
  clearTimeout(msgTimer); msgTimer = setTimeout(() => msg(''), 8000);
}
async function act(path, body, label) {
  msg(label + '…');
  try {
    const res = await j('/api/actions/' + path, {method: 'POST', body: JSON.stringify(body)});
    msg(label + (res.executed === false ? ':' + (res.note || '未执行') : ' 完成') +
        (res.aborted && res.aborted.length ? ';熔断:' + res.aborted.join(',') : ''));
  } catch (e) { msg(label + ' 失败:' + e.message); }
  refresh();
}
async function refresh() {
  try {
    const o = await j('/api/overview');
    document.getElementById('meta').textContent =
      `${o.landing} · ${o.readonly ? '只读模式(未加载 --config,动作不可用)' : '完整模式'} · ` +
      new Date().toLocaleTimeString();
    const srcs = o.sources.map(s => s.source);
    document.getElementById('actions').innerHTML = o.readonly
      ? '<span class="empty">只读模式:以 --config connect.yaml 启动可启用动作</span>'
      : srcs.map(s =>
          `<button onclick="act('sync',{source:'${esc(s)}'},'同步 ${esc(s)}')">立即同步</button>` +
          `<button onclick="act('reconcile',{source:'${esc(s)}'},'对账 ${esc(s)}')">对账 L1</button>` +
          `<button class="secondary" onclick="act('reconcile',{source:'${esc(s)}',deep:true},'深度对账 ${esc(s)}')">深度对账</button>` +
          `<button class="secondary" onclick="act('apply',{source:'${esc(s)}'},'重新映射 ${esc(s)}')">重新映射</button>`
        ).join('<br>');
    table('state', ['源', '表', '水位列', '高水位', '最近同步'],
      o.sources.flatMap(s => s.state.map(t =>
        [td(esc(s.source)), td(esc(t.table_name)), td(esc(t.watermark_col)),
         td(esc(t.high_water)), td(esc(t.last_run_at))])));
    table('objects', ['对象', '行数', '物化时间', '未处理隔离'],
      o.objects.map(x => [td(`${esc(x.object)}(${esc(x.display_name)})`),
        td(x.rows ?? '未物化'), td(esc(x.mapped_at)), td(x.quarantined)]));
    const runs = await j('/api/runs?limit=12');
    table('runs', ['#', '开始', '结束', '状态', '表', '行', '说明'],
      runs.map(r => [td(r.id), td(esc(r.started_at)), td(esc(r.finished_at)),
        badge(r.status), td(r.tables), td(r.rows), td(esc(r.detail), 'wrap')]));
    const q = await j('/api/quarantine');
    table('quarantine', ['#', '对象', '业务键', '原因', '时间', '操作'],
      q.map(r => [td(r.id), td(esc(r.object)), td(esc(r.keys_json)),
        td(esc(r.reason), 'wrap'), td(esc(r.created_at)),
        td(`<button onclick="act('retry',{source:'${esc(r.source)}',object:'${esc(r.object)}'},'重试 ${esc(r.object)}')">修复后重试</button>`)]));
    const audit = await j('/api/audit?limit=30');
    table('audit', ['时间', '源', '动作', '行数', '耗时ms', 'SQL'],
      audit.map(r => [td(esc(r.ts)), td(esc(r.source)), td(esc(r.action)),
        td(r.rows), td(r.duration_ms), td(esc(r.sql), 'wrap')]));
  } catch (e) { document.getElementById('meta').textContent = '加载失败:' + e.message; }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
