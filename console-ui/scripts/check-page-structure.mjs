#!/usr/bin/env node
/**
 * 页面结构规范检查(05-console §3.2 的机器执行层)。
 *
 * 规则:
 *  R1 视图一律禁止裸用 <el-pagination> —— 必须用 shared/PagerBar 组件;
 *  R2 视图 scoped 样式禁止自定义 .toolbar / .pager / .toolbar__total
 *     —— 工具栏/分页样式只能来自 tokens.css 全局规范类;
 *  R3 每个页面必须在 PAGE_TYPES 登记表声明类型(A/B/C/D),未登记即失败
 *     —— 新页面强制先想类型再写码;
 *  R4 A 类页面:根节点须 d2a-page-flush、首个卡片须为 d2a-card d2a-toolbar、
 *     须使用 Pager(除非显式 noPager: true,如无分页的纯目录页)。
 *
 * 用法: node scripts/check-page-structure.mjs   (CI 由 verify.py 前端任务调用)
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const VIEWS = new URL('../src/views', import.meta.url).pathname

/** 页面类型登记表:路由名 → 类型;A 类可注 noPager(纯目录/详情页)。 */
const PAGE_TYPES = {
  DashboardView: 'B',
  SourcesView: 'D',
  PipelineView: 'B',
  RunsView: 'A',
  QuarantineView: 'A',
  RawDataView: 'A',         // 目录型:分页在浏览抽屉内,页面本身无分页
  ObjectsDataView: 'A',
  DatasetsView: 'A',
  TemplatesView: 'D',
  ObjectGraphView: 'B',
  DeadStockValidationView: 'D',
  McpLabView: 'D',
  SettingsView: 'C',
  LogsView: 'C',
  AuditView: 'A(tabs)',   // 页签变体:通栏页签带 + 首位工具栏贴合
  ValidationView: 'C',
  SetupView: 'C',
}

/** A 类但页面级无分页(分页在抽屉/子组件内),须在注释中说明原因 */
const NO_PAGER = new Set(['RawDataView'])

const violations = []
const files = readdirSync(VIEWS).filter((f) => f.endsWith('.vue'))

for (const file of files) {
  const name = file.replace(/\.vue$/, '')
  const text = readFileSync(join(VIEWS, file), 'utf-8')
  const type = PAGE_TYPES[name]

  if (!type) {
    violations.push(`${file}: 未在 PAGE_TYPES 登记表声明页面类型(05-console §3.1)`)
    continue
  }

  // R1: 裸分页组件
  if (/<el-pagination[\s>]/.test(text)) {
    violations.push(`${file}: 禁止裸用 <el-pagination>,请用 shared/PagerBar 组件(规范 §3.2-2)`)
  }

  // R2: 本地工具栏/分页样式
  const styleBlocks = [...text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)]
    .map((m) => m[1]).join('\n')
  for (const banned of ['.toolbar {', '.toolbar{', '.pager {', '.toolbar__total']) {
    if (styleBlocks.includes(banned)) {
      violations.push(
        `${file}: scoped 样式禁止自定义 ${banned.trim()},工具栏/分页样式只能用 tokens.css 全局类`,
      )
    }
  }

  // R4: A 类结构
  if (type.startsWith('A')) {
    if (!/d2a-page-flush/.test(text)) {
      violations.push(`${file}: A 类页面根节点须带 d2a-page-flush(规范 §3.2)`)
    }
    if (!/d2a-card d2a-toolbar|d2a-toolbar\b/.test(text)) {
      violations.push(`${file}: A 类页面须含规范工具栏(d2a-card d2a-toolbar)`)
    }
    if (!/from '@\/components\/shared\/PagerBar\.vue'/.test(text) && !NO_PAGER.has(name)) {
      violations.push(`${file}: A 类页面须使用 PagerBar 分页组件(规范 §3.2-2)`)
    }
  }
}

if (violations.length) {
  console.error('页面结构规范违规:')
  for (const v of violations) console.error(`  - ${v}`)
  process.exit(1)
}
console.log(`页面结构规范检查通过:${files.length} 个视图(${Object.keys(PAGE_TYPES).length} 个已登记)`)
