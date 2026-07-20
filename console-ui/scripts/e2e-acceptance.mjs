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
