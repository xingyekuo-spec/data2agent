#!/usr/bin/env node
/**
 * M3 浏览器验收:Dashboard / 管道页的关键用户判断(断言可见状态与关键文案,
 * 不是截图脚本)。
 *
 * Part A(Mock):dev server + 10 场景 —— healthy 摘要、刷新失败保留旧数据、
 *   apply 熔断双节点定位;
 * Part B(Real):临时 SQLite(展厅 seed → sync → apply → reconcile → ingest)
 *   + 真实 FastAPI console + dev:real —— REAL 标识、真实计数、M4 运行/审计/数据验收。
 *
 * 用法:
 *   node scripts/e2e-acceptance.mjs            # Mock + Real
 *   node scripts/e2e-acceptance.mjs --mock     # 仅 Mock
 *   node scripts/e2e-acceptance.mjs --real     # 仅 Real
 * 环境:D2A_PYTHON 指定后端解释器(默认 ../.venv/bin/python);
 *       PLAYWRIGHT_BROWSERS_PATH 可指向已有浏览器缓存(CI 先 npx playwright install)。
 */
import { spawn, spawnSync } from 'node:child_process'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { chromium } from 'playwright'

const ROOT = resolve('..')
const PYTHON = process.env.D2A_PYTHON ?? join(ROOT, '.venv/bin/python')
const ONLY = process.argv[2]
const SOURCE = 'digiwin_e10'

const MOCK_PORT = 5191
const REAL_UI_PORT = 5192
const CONSOLE_PORT = 8849

let passed = 0
const failures = []
function expect(cond, label) {
  if (cond) {
    passed += 1
    console.log(`  OK  ${label}`)
  } else {
    failures.push(label)
    console.error(`  FAIL ${label}`)
  }
}

function sh(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: 'pipe', encoding: 'utf8', ...opts })
  if (r.status !== 0) {
    throw new Error(`${cmd} ${args.join(' ')} failed:\n${r.stderr || r.stdout}`)
  }
  return r.stdout.trim()
}

function sqliteCounts(landing) {
  const script = `
import json, sqlite3
db = sqlite3.connect(${JSON.stringify(landing)})
queries = {
    "d2a_sync_state": "SELECT COUNT(*) FROM d2a_sync_state",
    "d2a_quarantine": "SELECT COUNT(*) FROM d2a_quarantine",
    "d2a_sync_run": "SELECT COUNT(*) FROM d2a_sync_run",
    "raw_CUSTOMER": 'SELECT COUNT(*) FROM "raw_digiwin_e10__CUSTOMER"',
    "obj_Customer": 'SELECT COUNT(*) FROM "obj_Customer"',
}
print(json.dumps({k: db.execute(sql).fetchone()[0] for k, sql in queries.items()}, sort_keys=True))
`
  return JSON.parse(sh(PYTHON, ['-c', script]))
}

function postIngestBatch(landing) {
  const script = `
from fastapi.testclient import TestClient
from data2agent.ingest.app import create_app

client = TestClient(create_app(${JSON.stringify(landing)}))
body = {
    "source": ${JSON.stringify(SOURCE)},
    "table": "CUSTOMER",
    "columns": [
        ["Id", "int"],
        ["CUSTOMER_CODE", "text"],
        ["CUSTOMER_NAME", "text"],
        ["CUSTOMER_SHORT_NAME", "text"],
        ["COUNTRY_REGION", "text"],
        ["CURRENCY_ID", "int"],
        ["PAYMENT_TERM_DAYS", "int"],
        ["CONTACT_NAME", "text"],
        ["CONTACT_PHONE", "text"],
        ["CONTACT_EMAIL", "text"],
        ["CREATE_DATE", "text"],
        ["CREATE_BY", "text"],
        ["LAST_MODIFIED_DATE", "text"],
        ["LAST_MODIFIED_BY", "text"],
        ["Owner_Org_ROid", "text"],
    ],
    "pk": ["Id"],
    "batch_id": "e2e-ingest-001",
    "rows": [{
        "Id": 999,
        "CUSTOMER_CODE": "C999",
        "CUSTOMER_NAME": "E2E Customer LLC",
        "CUSTOMER_SHORT_NAME": "E2E",
        "COUNTRY_REGION": "美国",
        "CURRENCY_ID": 1,
        "PAYMENT_TERM_DAYS": 30,
        "CONTACT_NAME": "E2E Admin",
        "CONTACT_PHONE": "+1-555-0100",
        "CONTACT_EMAIL": "e2e.admin@example.com",
        "CREATE_DATE": "2026-07-20 09:00:00",
        "CREATE_BY": "E2E",
        "LAST_MODIFIED_DATE": "2026-07-20 09:00:00",
        "LAST_MODIFIED_BY": "E2E",
        "Owner_Org_ROid": "ORG01",
    }],
}
resp = client.post("/ingest/batch", json=body)
resp.raise_for_status()
`
  sh(PYTHON, ['-c', script], { cwd: ROOT })
}

function insertQuarantineRecord(landing, source, object, keysJson, reason, rawJson) {
  // 使用 Python json 模块构造 JSON 字符串,避免 JS↔Python 序列化歧义
  const script = `import json, sqlite3
from datetime import datetime, timezone
db = sqlite3.connect(${JSON.stringify(landing)})
keys_val = json.dumps(${JSON.stringify(keysJson)})
raw_val = json.dumps(${JSON.stringify(rawJson)})
created_at = datetime.now(timezone.utc).isoformat()
db.execute(
    "INSERT INTO d2a_quarantine (source, object, keys_json, reason, raw_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?)",
    (${JSON.stringify(source)}, ${JSON.stringify(object)}, keys_val, ${JSON.stringify(reason)}, raw_val, created_at),
)
db.commit()`
  sh(PYTHON, ['-c', script], { cwd: ROOT })
}

async function waitFor(url, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(url)
      if (resp.status < 500) {
        return
      }
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 500))
  }
  throw new Error(`等待服务就绪超时: ${url}`)
}

async function waitText(page, selector, timeoutMs = 10000) {
  await page.locator(selector).first().waitFor({ state: 'visible', timeout: timeoutMs })
  return page.locator(selector).first().textContent()
}

function startProc(cmd, args, name, cwd = ROOT) {
  const proc = spawn(cmd, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] })
  proc.stdout.on('data', () => {})
  proc.stderr.on('data', () => {})
  const stop = () => {
    if (!proc.killed) {
      proc.kill('SIGTERM')
    }
  }
  process.on('exit', stop)
  return { proc, stop, name }
}

async function runMock(browser) {
  console.log('\n== Part A: Mock 模式验收 ==')
  const dev = startProc('npm', ['run', 'dev', '--', '--port', String(MOCK_PORT), '--strictPort'], 'mock-dev', resolve('.'))
  try {
    await waitFor(`http://localhost:${MOCK_PORT}/v1/`)
    const page = await browser.newPage()
    await page.goto(`http://localhost:${MOCK_PORT}/v1/`, { waitUntil: 'networkidle' })

    // 仪表盘 healthy:30 秒判断要素齐全
    expect((await waitText(page, '[data-testid="topbar-title"]')).includes('仪表盘'), '顶栏显示当前页面标题')
    expect((await waitText(page, '[data-testid="env-badge"]')) === 'MOCK', '顶栏持续显示 MOCK 标识')
    expect((await page.locator('[data-testid="stat-value"]').allTextContents()).length === 4, '四张摘要卡')
    expect((await page.textContent('body')).includes('对象层总行数'), '摘要卡有口径标签')
    expect((await page.textContent('body')).includes('尚未启用(v0.3)'), 'dataset/object 版本显示尚未启用(不伪造版本号)')
    expect((await page.textContent('body')).includes('raw_rows'), '数量口径说明可见')

    // 管道页:7 节点 + overall;节点详情可开可关
    await page.locator('.el-menu-item', { hasText: '管道状态' }).click()
    await page.locator('[data-testid="pipeline-flow"]').waitFor({ state: 'visible' })
    expect((await page.locator('.flow__node').count()) === 7, '管道页固定 7 节点')
    expect((await page.locator('[data-testid="pipeline-overall"]').count()) === 1, 'overall 状态可见')
    await page.locator('.flow__node').first().click()
    expect((await page.locator('[data-testid="node-detail"]').count()) === 1, '节点详情可打开')
    expect((await page.textContent('[data-testid="node-detail"]')).includes('M4'), '详情入口说明(M4 前不死链)')
    await page.locator('.detail__close').click()
    expect((await page.locator('[data-testid="node-detail"]').count()) === 0, '节点详情可关闭')

    // 切场景:unknown-error → 刷新失败标记 + 旧数据保留
    await page.locator('.el-menu-item', { hasText: '仪表盘' }).click()
    await page.locator('[data-testid="scenario-switcher"] select').selectOption('unknown-error')
    await page.locator('[data-testid="refresh-error"]').waitFor({ state: 'visible' })
    expect(true, 'unknown-error:刷新失败标记可见')
    expect((await page.locator('[data-testid="stat-grid"]').count()) === 1, 'unknown-error:旧摘要数据保留(不变空)')

    // 切场景:apply 熔断 → 管道双节点可定位
    await page.locator('[data-testid="scenario-switcher"] select').selectOption('apply-circuit-broken')
    await page.locator('.el-menu-item', { hasText: '管道状态' }).click()
    await page.locator('[data-testid="pipeline-flow"]').waitFor({ state: 'visible' })
    const mapping = page.locator('.flow__node', { hasText: 'mapping' })
    const objects = page.locator('.flow__node', { hasText: 'objects' })
    expect((await mapping.getAttribute('data-status')) === 'failed', '熔断:映射节点 failed')
    expect((await mapping.textContent()).includes('熔断'), '熔断:映射原因含熔断说明')
    expect((await objects.getAttribute('data-status')) === 'stale', '熔断:对象层 stale(上一稳定结果)')

    // M4:运行列表 → step 详情 → URL 恢复同一 run
    await page.locator('[data-testid="scenario-switcher"] select').selectOption('healthy')
    await page.locator('.el-menu-item', { hasText: '运行记录' }).click()
    await page.locator('[data-testid="runs-table"]').waitFor({ state: 'visible' })
    await page.locator('[data-testid="runs-table"] tbody tr').first().click()
    await page.locator('[data-testid="steps-table"]').waitFor({ state: 'visible' })
    expect(true, 'M4:运行行点击打开详情抽屉')
    expect((await page.locator('[data-testid="steps-table"] tbody tr').count()) > 0, 'M4:详情含 step 行')
    expect((await page.textContent('[data-testid="steps-table"]')).includes('CUSTOMER'), 'M4:step 含目标表')
    expect((await page.textContent('body')).includes('2026-07-17 08:30:00'), 'M4:step 含水位证据')
    // URL 恢复:深链重新打开同一 run
    await page.goto(`http://localhost:${MOCK_PORT}/v1/runs?run_id=42`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="run-detail-drawer"]').waitFor({ state: 'visible' })
    expect(true, 'M4:run_id 深链自动打开详情')
    // 审计与数据页
    await page.goto(`http://localhost:${MOCK_PORT}/v1/audit`, { waitUntil: 'networkidle' })
    expect((await page.locator('[data-testid="sql-table"]').count()) === 1, 'M4:SQL 审计表可见')
    expect((await page.locator('[data-testid="sql-full"]').count()) === 0, 'M4:SQL 默认折叠(全文不展开)')
    await page.goto(`http://localhost:${MOCK_PORT}/v1/data`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="browse-CUSTOMER"]').click()
    await page.locator('[data-testid="raw-table"]').waitFor({ state: 'visible' })
    expect((await page.textContent('[data-testid="raw-table"]')).includes('***'), 'M4:raw 敏感列脱敏')

    // M6:MCP Lab Mock —— 查询表单、结果、建议卡入口,无写回控件
    await page.goto(`http://localhost:${MOCK_PORT}/v1/mcp`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="mcp-lab-page"]').waitFor({ state: 'visible' })
    expect((await page.locator('[data-testid="feature-placeholder"]').count()) === 0,
      'M6:MCP Lab 不再是占位页')
    expect((await page.textContent('[data-testid="mcp-scope-banner"]')).includes('进程内有效'),
      'M6:进程级 query ID 边界提示可见')
    await page.locator('[data-testid="object-run"]').click()
    await page.waitForTimeout(500)
    expect((await page.locator('[data-testid="object-result"]').count()) === 1, 'M6:Mock 对象查询有结果')
    expect((await page.textContent('[data-testid="no-execute-hint"]')).includes('不提供执行建议'),
      'M6:明确无执行建议/写回')
    await page.close()
  } finally {
    dev.stop()
  }
}

async function runReal(browser) {
  console.log('\n== Part B: Real 模式验收(临时 SQLite + 真实后端)==')
  const tmp = mkdtempSync(join(tmpdir(), 'd2a-e2e-'))
  const src = join(tmp, 'e10.sqlite')
  const landing = join(tmp, 'landing.sqlite')
  sh(PYTHON, ['-m', 'data2agent.showroom.seed'], { cwd: ROOT })
  // seed 默认写 showroom/e10.sqlite;复制到临时目录隔离
  sh('cp', [join(ROOT, 'showroom/e10.sqlite'), src])
  sh(PYTHON, ['-m', 'data2agent.connect', 'sync', '--sqlite', src, '--landing', landing], { cwd: ROOT })
  sh(PYTHON, ['-m', 'data2agent.connect', 'apply', '--landing', landing], { cwd: ROOT })
  sh(PYTHON, ['-m', 'data2agent.connect', 'reconcile', '--sqlite', src, '--landing', landing], { cwd: ROOT })
  postIngestBatch(landing)
  // M4:补一条 legacy 运行(无 run_type / 无 step)
  const legacyInsert = "import sqlite3;c=sqlite3.connect('" + landing + "');"
    + "c.execute(\"INSERT INTO d2a_sync_run (source, started_at, finished_at, status, detail) "
    + "VALUES ('digiwin_e10','2020-01-01 00:00:00','2020-01-01 00:00:05','ok','老记录')\");"
    + 'c.commit()'
  sh(PYTHON, ['-c', legacyInsert])
  // 带 sync_every 的配置:无配置时观测层对新鲜度只能判 unknown(诚实口径)
  const cfgPath = join(tmp, 'connect.yaml')
  writeFileSync(
    cfgPath,
    `templates: ${join(ROOT, 'templates')}\nlanding: ${landing}\nsources:\n  digiwin_e10:\n    adapter: sqlite_readonly\n    path: ${src}\n    sync_every: 30m\n`,
  )

  const consoleProc = startProc(
    PYTHON,
    ['-m', 'data2agent.console', '--config', cfgPath,
      '--port', String(CONSOLE_PORT), '--token', 'e2e-token'],
    'console',
  )
  const dev = startProc('npm', ['run', 'dev:real', '--', '--port', String(REAL_UI_PORT), '--strictPort'], 'real-dev', resolve('.'))
  try {
    await waitFor(`http://localhost:${REAL_UI_PORT}/v1/`)
    const page = await browser.newPage()
    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/`, { waitUntil: 'networkidle' })

    expect((await waitText(page, '[data-testid="env-badge"]')) === 'REAL', '顶栏显示 REAL 标识')
    expect((await page.locator('[data-testid="scenario-switcher"]').count()) === 0, 'REAL 无场景切换面板')
    // 认证:401 弹窗 → 输入 token → 数据加载
    await page.locator('input[data-testid="auth-token-input"]').fill('e2e-token')
    await page.locator('[data-testid="auth-submit"]').click()
    await page.locator('[data-testid="stat-grid"]').waitFor({ state: 'visible' })
    expect(true, '401 认证后可登录加载')
    const cards = await page.locator('[data-testid="stat-value"]').allTextContents()
    expect(cards[1] === '5/5', `真实覆盖率 5/5(实际 ${cards[1]})`)

    await page.locator('.el-menu-item', { hasText: '管道状态' }).click()
    await page.locator('[data-testid="pipeline-flow"]').waitFor({ state: 'visible' })
    const statusOf = async (name) =>
      page.locator('.flow__node', { hasText: name }).first().getAttribute('data-status')
    expect((await statusOf('erp')) === 'healthy', 'Real:erp healthy')
    expect((await statusOf('extract')) === 'healthy', 'Real:extract healthy')
    expect((await statusOf('push')) === 'idle', 'Real:push 本地直写 idle')
    expect((await statusOf('mapping')) === 'warning', 'Real:mapping warning(全部 draft)')
    expect((await statusOf('mcp')) === 'failed', 'Real:mcp failed(未启动)')

    // M4 Real:运行详情含真实结构化 step;legacy 显示无证据
    await page.locator('.el-menu-item', { hasText: '运行记录' }).click()
    await page.locator('[data-testid="runs-table"]').waitFor({ state: 'visible' })
    const rows = page.locator('[data-testid="runs-table"] tbody tr')
    expect((await rows.count()) >= 5, 'Real:运行列表含 sync/apply/reconcile/ingest/legacy')
    const runTypes = ['sync', 'apply', 'reconcile', 'ingest']
    for (const runType of runTypes) {
      await page.goto(`http://localhost:${REAL_UI_PORT}/v1/runs?type=${runType}`, { waitUntil: 'networkidle' })
      await page.locator('[data-testid="runs-table"]').waitFor({ state: 'visible' })
      await page.locator('[data-testid="runs-table"] tbody tr').first().click()
      await page.locator('[data-testid="steps-table"]').waitFor({ state: 'visible' })
      expect((await page.locator('[data-testid="steps-table"] tbody tr').count()) > 0,
        `Real:${runType} 运行有结构化 step`)
      await page.locator('.el-drawer__close-btn').click()
    }
    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/runs`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="runs-table"]').waitFor({ state: 'visible' })
    const legacyRow = page.locator('[data-testid="runs-table"] tbody tr', { hasText: '类型未知' })
    await legacyRow.first().click()
    await page.locator('[data-testid="legacy-note"]').waitFor({ state: 'visible' })
    expect(true, 'Real:legacy 运行显示"没有逐步证据"')
    await page.locator('.el-drawer__close-btn').click()

    // M4 Real:SQL 筛选、raw 拒绝审计、注入搜索与业务副作用
    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/audit?tab=sql&source=${SOURCE}&action=read`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="sql-table"]').waitFor({ state: 'visible' })
    expect((await page.textContent('[data-testid="sql-table"]')).includes('read'),
      'Real:SQL 审计 action 筛选生效')
    expect((await page.textContent('[data-testid="sql-table"]')).includes(SOURCE),
      'Real:SQL 审计 source 筛选生效')
    const deniedRaw = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/data/raw/${SOURCE}/CUSTOMER`,
    )
    expect(deniedRaw.status() === 401, 'Real:raw 无 token 请求被拒绝')
    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/audit?tab=access&allowed=false`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="access-table"]').waitFor({ state: 'visible' })
    expect((await page.textContent('[data-testid="access-table"]')).includes('unauthorized'),
      'Real:raw 拒绝请求进入访问审计')
    const injected = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/data/raw/${SOURCE}/CUSTOMER`,
      {
        headers: { Authorization: 'Bearer e2e-token' },
        params: { q: "' OR 1=1 --" },
      },
    )
    const injectedBody = await injected.json()
    expect(injected.status() === 200 && injectedBody.total === 0,
      'Real:raw 搜索注入不扩张结果集')
    const badSource = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/data/raw/bogus/CUSTOMER`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(badSource.status() === 404, 'Real:raw source 注入/越界返回 404')
    const badTable = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/data/raw/${SOURCE}/sqlite_master`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(badTable.status() === 404, 'Real:raw table 注入/越界返回 404')
    const countsBeforeBrowse = sqliteCounts(landing)

    // M4 Real:数据浏览(脱敏 + 无副作用)
    await page.locator('.el-menu-item', { hasText: '数据浏览' }).click()
    await page.locator('[data-testid="raw-catalog"]').waitFor({ state: 'visible' })
    await page.locator('[data-testid="browse-CUSTOMER"]').click()
    await page.locator('[data-testid="raw-table"]').waitFor({ state: 'visible' })
    expect((await page.textContent('[data-testid="raw-table"]')).includes('***'),
      'Real:raw 敏感列脱敏')
    const panes = page.locator('.el-tabs__item', { hasText: '对象层' })
    await panes.click()
    await page.locator('[data-testid="obj-catalog"]').waitFor({ state: 'visible' })
    await page.locator('[data-testid="browse-Customer"]').click()
    await page.locator('[data-testid="obj-table"]').waitFor({ state: 'visible' })
    expect((await page.textContent('[data-testid="obj-table"]')).includes('***'),
      'Real:对象敏感属性脱敏')
    await page.locator('[data-testid="json-toggle"]').click()
    expect((await page.textContent('[data-testid="json-view"]')).includes('***'),
      'Real:对象 JSON 与表格同源脱敏')
    expect(JSON.stringify(sqliteCounts(landing)) === JSON.stringify(countsBeforeBrowse),
      'Real:数据浏览不改变业务表/水位/运行记录')

    // ============================================================
    // M5: 隔离与模板只读验证(§8.2)
    // ============================================================

    // 确保至少有一条隔离记录(种子数据 + apply 可能已产生;若无则显式插入)
    const ensureQuarantine = `
import json, sqlite3
db = sqlite3.connect(${JSON.stringify(landing)})
existing = db.execute(
    "SELECT COUNT(*) FROM d2a_quarantine WHERE source=? AND object=?",
    [${JSON.stringify(SOURCE)}, "Customer"]
).fetchone()[0]
if existing == 0:
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    keys = {"CUSTOMER_CODE": "QTEST001"}
    raw = {"Id": 888, "CUSTOMER_CODE": "QTEST001", "CUSTOMER_NAME": "E2E Quarantine Test",
           "CUSTOMER_SHORT_NAME": "QT", "COUNTRY_REGION": "\\u5357\\u6781", "CURRENCY_ID": 1}
    db.execute(
        "INSERT INTO d2a_quarantine (source, object, keys_json, reason, raw_json, batch_id, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [${JSON.stringify(SOURCE)}, "Customer", json.dumps(keys),
         "e2e-test: enum value missing in mapping", json.dumps(raw), "e2e-m5-001", now])
    db.commit()
    print("inserted-1")
else:
    print(f"already-{existing}")
`
    sh(PYTHON, ['-c', ensureQuarantine])

    // M5-4b 夹具:含敏感字段的隔离记录(须在只读基线之前插入)
    insertQuarantineRecord(
      landing, SOURCE, 'Customer',
      { CUSTOMER_CODE: 'C-ALICE' },
      'customer_code: 源码值未映射',
      { CUSTOMER_CODE: 'C-ALICE', CUSTOMER_NAME: 'Alice',
        CONTACT_EMAIL: 'alice.secret@corp.example.com',
        CONTACT_PHONE: '+1-555-0001' },
    )

    // 所有夹具插入完成后再取基线;后续只比较只读浏览前后
    const countsBeforeM5Browse = sqliteCounts(landing)

    // M5-1: 隔离列表不含 raw(QuarantineRecord 不同于 QuarantineDetail)
    const qListResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/quarantine?limit=50&offset=0`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(qListResp.status() === 200, 'M5:隔离列表 200')
    const qListBody = await qListResp.json()
    expect(Array.isArray(qListBody) && qListBody.length > 0, 'M5:隔离列表含记录')
    expect(qListBody[0].id !== undefined, 'M5:隔离列表记录含 id')
    expect(qListBody[0].source === SOURCE, 'M5:隔离列表 source 正确')
    // QuarantineRecord 不含 raw;只有 QuarantineDetail(/api/quarantine/{id})有
    expect(!('raw' in qListBody[0]), 'M5:隔离列表 QuarantineRecord 不含 raw 字段')
    expect('keys' in qListBody[0], 'M5:隔离列表含 keys')
    expect('reason' in qListBody[0], 'M5:隔离列表含 reason')
    expect('batch_id' in qListBody[0], 'M5:隔离列表含 batch_id')
    const qTotalHeader = qListResp.headers()['x-total-count']
    expect(qTotalHeader !== undefined && Number(qTotalHeader) > 0, 'M5:隔离列表有 X-Total-Count')

    // M5-2: 隔离分组(按 object 聚合)
    const groupsResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/quarantine/groups`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(groupsResp.status() === 200, 'M5:隔离分组 200')
    const groupsBody = await groupsResp.json()
    expect(Array.isArray(groupsBody) && groupsBody.length > 0, 'M5:隔离分组含对象')
    const customerGroup = groupsBody.find(g => g.object === 'Customer')
    expect(customerGroup !== undefined, 'M5:隔离分组含 Customer')
    if (customerGroup) {
      expect(customerGroup.pending > 0, 'M5:Customer 分组 pending > 0')
      expect(typeof customerGroup.breaker_threshold === 'number', 'M5:分组含 breaker_threshold')
      expect(customerGroup.breaker_threshold > 0, 'M5:breaker_threshold > 0')
      expect(['ok', 'warning', 'tripped', 'unknown'].includes(customerGroup.rate_state),
        'M5:分组含 rate_state')
      expect(['fresh', 'stale', 'not_materialized', 'unavailable', 'unknown'].includes(customerGroup.serving_state),
        'M5:分组含 serving_state')
      expect(customerGroup.latest_batch_id !== undefined, 'M5:分组含 latest_batch_id')
    }
    // 未知 source/object 不隐藏(保留为事实——取决于实际数据)

    // M5-3: 隔离 raw 详情需要 token(强制 Bearer auth)
    const qid = qListBody[0].id
    expect(typeof qid === 'number', 'M5:隔离记录 id 为数字')

    // 无 token → 403
    const noTokenResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/quarantine/${qid}`,
    )
    expect(noTokenResp.status() === 403 || noTokenResp.status() === 401,
      `M5:隔离详情无 token → 403/401(实际 ${noTokenResp.status()})`)

    // 错误 token → 401
    const badTokenResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/quarantine/${qid}`,
      { headers: { Authorization: 'Bearer wrong-token-e2e' } },
    )
    expect(badTokenResp.status() === 401,
      `M5:隔离详情错误 token → 401(实际 ${badTokenResp.status()})`)

    // M5-4: 有效 token 返回脱敏详情(raw sanitisied, truncations present)
    const detailResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/quarantine/${qid}`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(detailResp.status() === 200, 'M5:隔离详情有效 token 200')
    const detailBody = await detailResp.json()
    expect(typeof detailBody.raw === 'object' && detailBody.raw !== null, 'M5:详情含 raw 预览')
    expect(detailBody.keys !== null || detailBody.keys_json !== null, 'M5:详情含 keys')
    expect(typeof detailBody.reason === 'string', 'M5:详情含 reason')
    expect(typeof detailBody.request_id === 'string', 'M5:详情含 request_id(request_id)')
    expect(Array.isArray(detailBody.truncations), 'M5:详情含 truncations 数组')
    // raw 中的 JSON 不得含 traceback/SQL/敏感原文
    const rawStr = JSON.stringify(detailBody.raw)
    expect(!rawStr.includes('Traceback') && !rawStr.includes('traceback'),
      'M5:raw 预览不含 traceback')
    expect(!rawStr.includes('SELECT ') && !rawStr.includes('INSERT '),
      'M5:raw 预览不含 SQL 片段')

    // M5-4b: 验证含 CONTACT_EMAIL 的夹具 raw 详情已掩码到 ***
    // 获取最新隔离记录的详情(按 id 逆序第一条)
    const qListResp2 = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/quarantine?source=${SOURCE}&object=Customer&limit=1`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(qListResp2.status() === 200, 'M5:再次获取隔离列表 200')
    const qListBody2 = await qListResp2.json()
    expect(qListBody2.length > 0, 'M5:新增隔离记录可见')
    const emailDetailResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/quarantine/${qListBody2[0].id}`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(emailDetailResp.status() === 200, 'M5:CONTACT_EMAIL 隔离详情 200')
    const emailDetail = await emailDetailResp.json()
    const emailRawStr = JSON.stringify(emailDetail.raw)
    expect(!emailRawStr.includes('alice.secret@corp.example.com'), 'M5:raw 不含明文邮箱')
    expect(!emailRawStr.includes('+1-555-0001'), 'M5:raw 不含明文电话')
    expect(emailRawStr.includes('***') || emailRawStr.includes('MASKED'),
      'M5:raw 敏感字段已掩码')
    // 允许:quarantine_detail 请求
    const allowedAuditResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/audit/access?limit=10&offset=0&resource_type=quarantine_raw&allowed=true`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(allowedAuditResp.status() === 200, 'M5:访问审计 allowed 200')
    const allowedAudit = await allowedAuditResp.json()
    expect(allowedAudit.total > 0, 'M5:访问审计含 quarantine_raw 允许记录')
    const allowItem = allowedAudit.items[0]
    expect(allowItem.subject !== undefined && allowItem.subject !== '', 'M5:审计记录含 subject')
    expect(allowItem.resource_type === 'quarantine_raw', 'M5:审计 resource_type=quarantine_raw')
    // 审计记录不含 Token / q 原文 / 返回值
    const allowStr = JSON.stringify(allowItem)
    expect(!allowStr.includes('e2e-token'), 'M5:审计记录不含 token')
    expect(!allowStr.includes('QTEST001'), 'M5:审计记录不含查询值原文')

    // 拒绝:quarantine_detail 被拒绝请求
    const deniedAuditResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/audit/access?limit=10&offset=0&resource_type=quarantine_raw&allowed=false`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(deniedAuditResp.status() === 200, 'M5:访问审计 denied 200')
    const deniedAudit = await deniedAuditResp.json()
    // 403/401 拒绝应产生审计;不强制数量断言(取决于审计写入时序)
    if (deniedAudit.total > 0) {
      expect(deniedAudit.items[0].allowed === false, 'M5:拒绝审计记录 allowed=false')
    }

    // M5-6: side-effect——浏览隔离/模板不改变 raw/object/watermark/quarantine/run 状态
    const countsAfterBrowse = sqliteCounts(landing)
    expect(countsAfterBrowse.d2a_quarantine === countsBeforeM5Browse.d2a_quarantine,
      'M5:浏览隔离不改变 quarantine 表')
    expect(countsAfterBrowse.raw_CUSTOMER === countsBeforeM5Browse.raw_CUSTOMER,
      'M5:浏览隔离不改变 raw 表')
    expect(countsAfterBrowse.obj_Customer === countsBeforeM5Browse.obj_Customer,
      'M5:浏览隔离不改变对象表')
    expect(countsAfterBrowse.d2a_sync_run === countsBeforeM5Browse.d2a_sync_run,
      'M5:浏览隔离不改变运行表')
    expect(countsAfterBrowse.d2a_sync_state === countsBeforeM5Browse.d2a_sync_state,
      'M5:浏览隔离不改变水位表')

    // M5-7: 模板页 GET /api/templates
    const templatesResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/templates`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(templatesResp.status() === 200, 'M5:模板列表 200')
    const templates = await templatesResp.json()
    expect(Array.isArray(templates) && templates.length >= 5,
      `M5:模板含 >=5 个对象(实际 ${templates.length})`)
    // 每个对象含 properties、bindings、materialized
    for (const tmpl of templates) {
      expect(Array.isArray(tmpl.properties), `M5:${tmpl.object} 含 properties 数组`)
      expect(Array.isArray(tmpl.bindings), `M5:${tmpl.object} 含 bindings 数组`)
      expect(tmpl.source_of_truth !== undefined, `M5:${tmpl.object} 含 source_of_truth`)
    }

    // 检查敏感标记
    const customerTmpl = templates.find(t => t.object === 'Customer')
    expect(customerTmpl !== undefined, 'M5:模板含 Customer')
    if (customerTmpl) {
      const sensitiveProps = customerTmpl.properties.filter(p => p.sensitive)
      expect(sensitiveProps.length > 0, 'M5:Customer 含敏感属性标记(脱敏目标)')
    }

    // Quotation binding enum_map(枚举映射)
    const quotationTmpl = templates.find(t => t.object === 'Quotation')
    if (quotationTmpl) {
      const hasEnumMap = quotationTmpl.bindings.some(
        b => b.enum_map && Object.keys(b.enum_map).length > 0,
      )
      // enum_values 也可能在 property 级
      const hasEnumProps = quotationTmpl.properties.some(
        p => Array.isArray(p.enum_values) && p.enum_values.length > 0,
      )
      expect(hasEnumMap || hasEnumProps, 'M5:Quotation 含枚举映射')
    }

    // SalesOrder derived 决策表
    const salesOrderTmpl = templates.find(t => t.object === 'SalesOrder')
    if (salesOrderTmpl) {
      const hasDerived = salesOrderTmpl.bindings.some(
        b => b.derived && Object.keys(b.derived).length > 0,
      )
      expect(hasDerived, 'M5:SalesOrder binding 含 derived 决策表')
    }

    // binding status 字段可见(draft/verified/disabled)
    const allBindings = templates.flatMap(t => t.bindings)
    expect(allBindings.length > 0, 'M5:模板含 binding 记录')
    const hasStatus = allBindings.some(b => ['draft', 'verified', 'disabled'].includes(b.status))
    expect(hasStatus, 'M5:binding status 字段可见')
    // disabled binding 不隐藏
    const disabledBindings = allBindings.filter(b => b.status === 'disabled')
    // 不强制存在 disabled;如其存在,enabled 字段必为 false
    if (disabledBindings.length > 0) {
      expect(disabledBindings.every(b => b.enabled === false), 'M5:disabled binding 的 enabled=false')
    }

    // materialized 状态
    const matTmpls = templates.filter(t => t.materialized !== null && t.materialized !== undefined)
    expect(matTmpls.length > 0, 'M5:模板含 materialized 状态')
    const validStates = ['materialized', 'not_materialized', 'unknown']
    expect(matTmpls.every(t => validStates.includes(t.materialized.state)),
      'M5:materialized.state 为合法枚举值')

    // quarantine_pending 计数字段
    const hasQP = templates.some(t => typeof t.quarantine_pending === 'number')
    expect(hasQP, 'M5:模板含 quarantine_pending 计数')

    // M5-8: 模板指标 GET /api/templates/metrics
    const metricsResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/templates/metrics`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(metricsResp.status() === 200, 'M5:指标列表 200')
    const metrics = await metricsResp.json()
    expect(Array.isArray(metrics) && metrics.length > 0, 'M5:指标列表非空')
    // draft 指标可见(不隐藏)
    const draftMetrics = metrics.filter(m => m.status === 'draft')
    expect(draftMetrics.length > 0, 'M5:存在 draft 指标')
    // calibration_state 字段
    const hasCalState = metrics.every(m => ['calibrated', 'uncalibrated', 'deprecated'].includes(m.calibration_state))
    expect(hasCalState, 'M5:指标含 calibration_state')
    // quote_response_hours 指标(若存在)为未校准
    const quoteMetric = metrics.find(m => m.metric === 'quote_response_hours')
    if (quoteMetric) {
      expect(quoteMetric.calibration_state === 'uncalibrated',
        'M5:quote_response_hours calibration_state=uncalibrated')
      expect(typeof quoteMetric.caveats === 'string' && quoteMetric.caveats.length > 0,
        'M5:quote_response_hours caveat 可见')
      expect(typeof quoteMetric.formula === 'string' && quoteMetric.formula.length > 0,
        'M5:quote_response_hours 含 formula')
    }

    // M5-9: side-effect——浏览模板也不改变状态
    const countsAfterTemplates = sqliteCounts(landing)
    expect(countsAfterTemplates.d2a_quarantine === countsBeforeM5Browse.d2a_quarantine,
      'M5:浏览模板不改变 quarantine 表')
    expect(countsAfterTemplates.d2a_sync_run === countsBeforeM5Browse.d2a_sync_run,
      'M5:浏览模板不改变运行表')
    expect(countsAfterTemplates.raw_CUSTOMER === countsBeforeM5Browse.raw_CUSTOMER,
      'M5:浏览模板不改变 raw 表')

    // M5-12: 浏览隔离页与模板页 UI(须在 API retry 清空隔离之前,否则可能合法空态)
    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/quarantine`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(2000)
    const qPageText = await page.textContent('body')
    expect(qPageText.includes('隔离') || qPageText.includes('Quarantine'),
      'M5:隔离页可访问')
    const hasGroupsTable = await page.locator('[data-testid="quarantine-groups-table"]').count()
    const hasRecordsTable = await page.locator('[data-testid="quarantine-records-table"]').count()
    expect(hasGroupsTable > 0 || hasRecordsTable > 0,
      'M5:隔离页含分组表或记录表')

    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/templates`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(2000)
    const tplPageText = await page.textContent('body')
    expect(tplPageText.includes('模板') || tplPageText.includes('Template'),
      'M5:模板页可访问')
    expect(tplPageText.includes('Customer'), 'M5:模板页含 Customer')
    const soItemPre = page.locator('[data-testid="tpl-item-SalesOrder"]')
    if (await soItemPre.count() > 0) {
      await soItemPre.first().click()
      await page.waitForTimeout(1000)
      const detailTabs = await page.locator('[data-testid="tpl-detail-tabs"]').count()
      expect(detailTabs > 0, 'M5:点击 SalesOrder 可打开详情')
    }

    // M5-10: 对象级 retry(POST /api/actions/retry)
    const retryResp = await page.request.post(
      `http://localhost:${CONSOLE_PORT}/api/actions/retry`,
      {
        data: { source: SOURCE, object: 'Customer' },
        headers: { Authorization: 'Bearer e2e-token' },
      },
    )
    const retryBody = await retryResp.json()
    if (retryResp.status() === 200) {
      // 成功:RetryActionResult
      expect(retryBody.executed === true, 'M5:retry 成功 executed=true')
      expect(retryBody.status === 'ok', 'M5:retry 成功 status=ok')
      expect(typeof retryBody.run_id === 'number' && retryBody.run_id > 0,
        'M5:retry 成功含 run_id')
      expect(typeof retryBody.step_id === 'number' && retryBody.step_id > 0,
        'M5:retry 成功含 step_id')
      expect(typeof retryBody.detail_path === 'string' && retryBody.detail_path.length > 0,
        'M5:retry 成功含 detail_path')
      expect(typeof retryBody.total === 'number', 'M5:retry 成功含 total')
      expect(typeof retryBody.mapped === 'number', 'M5:retry 成功含 mapped')
      expect(typeof retryBody.quarantined === 'number', 'M5:retry 成功含 quarantined')
    } else if (retryResp.status() === 409) {
      // 熔断:RetryActionError
      const reasonCodes = ['circuit_broken', 'execution_failed', 'observation_failed', 'preflight_failed']
      expect(reasonCodes.includes(retryBody.reason_code),
        `M5:retry 409 reason_code 合法(实际 ${retryBody.reason_code})`)
      expect(retryBody.status === 'aborted' || retryBody.status === 'failed',
        'M5:retry 409 status=aborted/failed')
      expect(retryBody.run_id !== undefined || retryBody.detail_path !== undefined,
        'M5:retry 409 含 run_id 或 detail_path')
    } else if (retryResp.status() === 500) {
      // 执行失败:RetryActionError
      expect(['circuit_broken', 'execution_failed', 'observation_failed', 'preflight_failed'].includes(retryBody.reason_code),
        `M5:retry 500 reason_code 合法(实际 ${retryBody.reason_code})`)
      expect(typeof retryBody.detail === 'string' && retryBody.detail.length > 0,
        'M5:retry 500 含安全错误摘要')
      expect(retryBody.error_id !== undefined || retryBody.run_id !== undefined,
        'M5:retry 500 含 error_id 或 run_id')
    } else {
      // 422/404 等——检查是否因为 readonly/disabled 等原因
      expect(retryResp.status() === 422 || retryResp.status() === 404,
        `M5:retry 非预期状态码 ${retryResp.status()}`)
    }

    // M5-11: retry 写审计(如果执行了的话,检查审计记录不含敏感值)
    if (retryResp.status() === 200 || retryResp.status() === 409 || retryResp.status() === 500) {
      const retryStr = JSON.stringify(retryBody)
      expect(!retryStr.includes('Traceback'), 'M5:retry 错误不含 traceback')
      expect(!retryStr.includes('e2e-token'), 'M5:retry 错误不含 token')
    }

    // M5-12b: 隔离页 UI retry 流——点击按钮、确认对话框、确认 Runs step
    // 前面的 API retry 可能已 resolve Customer 隔离;重新插入确保按钮可见
    insertQuarantineRecord(
      landing, SOURCE, 'Customer',
      { CUSTOMER_CODE: 'RETRY-UI-001' },
      'e2e-retry-ui: 验证 UI retry 流',
      { CUSTOMER_CODE: 'RETRY-UI-001', CUSTOMER_NAME: 'Retry UI Test' },
    )
    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/quarantine`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(2000)
    // 查找 Customer 分组的 retry 按钮
    const retryBtn = page.locator('[data-testid="retry-Customer"]')
    expect(await retryBtn.count() > 0, 'M5:retry-Customer 按钮存在')
    // 点击 retry 按钮,应弹出 Element Plus 确认对话框
    await retryBtn.first().click()
    await page.waitForTimeout(1000)
    // 确认对话框展示 display_name(有则本地化名),否则技术对象名
    const confirmBox = page.locator('.el-message-box')
    const confirmVisible = await confirmBox.count()
    expect(confirmVisible > 0, 'M5:retry 确认对话框可见')
    const confirmText = await confirmBox.textContent()
    const customerLabel = (customerGroup && customerGroup.display_name) || 'Customer'
    expect(confirmText.includes(customerLabel),
      `M5:确认对话框含对象展示名 ${customerLabel}`)
    // 点击确认按钮
    const confirmBtn = confirmBox.locator('.el-button--primary').first()
    await confirmBtn.click()
    await page.waitForTimeout(3000)
    // 等待 retry 结果对话框(成功或失败)——必须存在
    const resultBox = page.locator('[data-testid="retry-result-dialog"]')
    const retryRunLink = page.locator('[data-testid="retry-run-link"]')
    const retryErrorRunLink = page.locator('[data-testid="retry-error-run-link"]')
    const hasResult = await resultBox.count()
    const hasRunLink = await retryRunLink.count() > 0
    const hasErrorLink = await retryErrorRunLink.count() > 0
    expect(hasResult > 0, 'M5:retry 结果对话框可见')
    expect(hasRunLink || hasErrorLink, 'M5:retry 结果含 run 链接')
    // 验证 step kind="object": 获取 run_id 后通过 API 验证
    if (hasRunLink) {
      const href = await retryRunLink.getAttribute('href')
      const runIdMatch = href && href.match(/\d+$/)
      expect(runIdMatch !== null, 'M5:retry run link 含 run_id')
      const runId = parseInt(runIdMatch[0], 10)
      const runDetailResp = await page.request.get(
        `http://localhost:${CONSOLE_PORT}/api/runs/${runId}`,
        { headers: { Authorization: 'Bearer e2e-token' } },
      )
      expect(runDetailResp.status() === 200, 'M5:retry run 详情可访问')
      const runDetail = await runDetailResp.json()
      expect(runDetail.steps_state !== undefined, 'M5:retry run 含 steps_state')
      // steps 是 RunDetailResponse 顶层数组(非 steps_state 内嵌)
      const steps = runDetail.steps ?? []
      expect(Array.isArray(steps) && steps.length > 0, 'M5:retry run 含 step')
      const hasObjectStep = steps.some(s => s.kind === 'object')
      expect(hasObjectStep, 'M5:retry run step kind 含 object')
    } else if (hasErrorLink) {
      // 重试失败也有 run link(熔断/执行失败)
      const href = await retryErrorRunLink.getAttribute('href')
      expect(href && href.includes('/runs?run_id='), 'M5:retry 失败也有 runs 链接')
    }

    // M5-12c: stale serving_state —— 创建真实 stale 场景
    // 使 raw 表的 _d2a_extracted_at 晚于 obj 表的 _d2a_mapped_at,触发 serving_state=stale
    // 前面 UI retry 可能已 resolve;重新插入 Customer 隔离以保证分组中出现
    insertQuarantineRecord(
      landing, SOURCE, 'Customer',
      { CUSTOMER_CODE: 'STALE-TEST-001' },
      'e2e-stale-test: 验证 stale 服务状态',
      { CUSTOMER_CODE: 'STALE-TEST-001', CUSTOMER_NAME: 'Stale Test' },
    )
    const makeStaleSh = `
import json, sqlite3
from datetime import datetime, timezone, timedelta
db = sqlite3.connect(${JSON.stringify(landing)})
# 确认 obj_Customer 有 _d2a_mapped_at
row = db.execute('SELECT MAX("_d2a_mapped_at") AS m FROM "obj_Customer"').fetchone()
mapped_at = row[0]
assert mapped_at is not None, "obj_Customer 缺少 _d2a_mapped_at,无法构造 stale"
# 将 raw 表的 _d2a_extracted_at 设为未来时间,使其明显晚于 mapped_at
future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
updated = db.execute(
    'UPDATE "raw_digiwin_e10__CUSTOMER" SET "_d2a_extracted_at" = ?'
    ' WHERE "_d2a_extracted_at" <= ?', (future, mapped_at))
db.commit()
print(f"mapped_at={mapped_at} updated={updated.rowcount}")
`
    sh(PYTHON, ['-c', makeStaleSh])
    const staleGroupsResp = await page.request.get(
      `http://localhost:${CONSOLE_PORT}/api/quarantine/groups?source=${SOURCE}`,
      { headers: { Authorization: 'Bearer e2e-token' } },
    )
    expect(staleGroupsResp.status() === 200, 'M5:stale 场景分组 200')
    const staleGroups = await staleGroupsResp.json()
    const customerStaleGroup = staleGroups.find(g => g.object === 'Customer')
    expect(customerStaleGroup !== undefined, 'M5:stale 场景 Customer 在分组中')
    expect(customerStaleGroup.serving_state === 'stale',
      `M5:Customer serving_state=stale(实际 ${customerStaleGroup.serving_state})`)
    expect(customerStaleGroup.quarantine_rate !== null,
      `M5:Customer quarantine_rate 有值(实际 ${customerStaleGroup.quarantine_rate})`)
    // 验证各 serving_state 枚举值均合法
    const allStates = staleGroups.map(g => g.serving_state)
    const validServingStates = ['fresh', 'stale', 'not_materialized', 'unavailable', 'unknown']
    expect(allStates.every(s => validServingStates.includes(s)),
      `M5:serving_state 均为合法值(含 ${new Set(allStates).size} 种)`)

    // M5-13: 回归——M4 运行/审计/数据页仍可用
    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/runs`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="runs-table"]').waitFor({ state: 'visible' })
    expect(true, 'M5:回归-M4 运行列表可用')

    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/audit?tab=sql`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="sql-table"]').waitFor({ state: 'visible' })
    expect(true, 'M5:回归-M4 SQL 审计可用')

    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/data`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="raw-catalog"]').waitFor({ state: 'visible' })
    expect(true, 'M5:回归-M4 数据浏览可用')

    // 回归:Jinja2 /v0 仍可访问(FastAPI 路由,非 Vite)
    const v0Chk = await page.request.get(`http://localhost:${CONSOLE_PORT}/v0`)
    expect(v0Chk.status() === 200, 'M5:回归-/v0 200')
    // 管道页仍可用
    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="stat-grid"]').waitFor({ state: 'visible' })
    expect(true, 'M5:回归-M3 仪表盘可用')

    // ============================================================
    // M6: MCP Lab Real —— 查询 → 建议卡 evidence → 八页面可达
    // ============================================================
    const mcpQ = await page.request.post(
      `http://localhost:${CONSOLE_PORT}/api/debug/mcp-call`,
      {
        data: { tool: 'query_objects', params: { object: 'Quotation', limit: 1 } },
        headers: { Authorization: 'Bearer e2e-token' },
      },
    )
    expect(mcpQ.status() === 200, 'M6:Real mcp-call 200')
    const mcpBody = await mcpQ.json()
    expect(mcpBody.meta?.query_id, 'M6:Real 查询含 query_id')
    expect(mcpBody.meta?.evidence_scope === 'process', 'M6:Real evidence_scope=process')
    const proposalResp = await page.request.post(
      `http://localhost:${CONSOLE_PORT}/api/gateway/proposals`,
      {
        data: {
          object: 'Quotation',
          action: 'quote_review',
          conclusion: 'E2E 说档建议',
          evidence: [{ claim: '报价可见', query_id: mcpBody.meta.query_id }],
        },
        headers: { Authorization: 'Bearer e2e-token' },
      },
    )
    expect(proposalResp.status() === 200, `M6:Real proposal 200(实际 ${proposalResp.status()})`)
    const proposalBody = await proposalResp.json()
    expect(typeof proposalBody.governance === 'string' && proposalBody.governance.includes('未执行'),
      'M6:Real proposal 含说档治理文案')

    await page.goto(`http://localhost:${REAL_UI_PORT}/v1/mcp`, { waitUntil: 'networkidle' })
    await page.locator('[data-testid="mcp-lab-page"]').waitFor({ state: 'visible' })
    expect((await page.locator('[data-testid="mcp-scope-banner"]').count()) === 1,
      'M6:Real MCP Lab 页可访问')

    // 八页面冒烟
    for (const [path, testid] of [
      ['/v1/', 'stat-grid'],
      ['/v1/pipeline', 'pipeline-flow'],
      ['/v1/runs', 'runs-table'],
      ['/v1/audit', 'sql-table'],
      ['/v1/data', 'raw-catalog'],
      ['/v1/quarantine', 'quarantine-refresh'],
      ['/v1/templates', 'tpl-item-Customer'],
      ['/v1/mcp', 'mcp-lab-page'],
    ]) {
      await page.goto(`http://localhost:${REAL_UI_PORT}${path}`, { waitUntil: 'networkidle' })
      await page.waitForTimeout(800)
      const n = await page.locator(`[data-testid="${testid}"]`).count()
      expect(n > 0, `M6:八页面 ${path} 可见 ${testid}`)
    }

    await page.close()
  } finally {
    dev.stop()
    consoleProc.stop()
  }
}

const browser = await chromium.launch()
try {
  if (ONLY !== '--real') {
    await runMock(browser)
  }
  if (ONLY !== '--mock') {
    await runReal(browser)
  }
} finally {
  await browser.close()
}

console.log(`\n通过 ${passed} 项;失败 ${failures.length} 项`)
if (failures.length > 0) {
  console.error('失败项:', failures.join('; '))
  process.exit(1)
}
console.log('E2E ACCEPTANCE PASSED')
