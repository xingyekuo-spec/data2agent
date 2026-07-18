#!/usr/bin/env node
/**
 * M3 浏览器验收:Dashboard / 管道页的关键用户判断(断言可见状态与关键文案,
 * 不是截图脚本)。
 *
 * Part A(Mock):dev server + 10 场景 —— healthy 摘要、刷新失败保留旧数据、
 *   apply 熔断双节点定位;
 * Part B(Real):临时 SQLite(展厅 seed → sync → apply)+ 真实 FastAPI console
 *   + dev:real —— REAL 标识、真实计数、push 本地直写 idle、mapping warning。
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
