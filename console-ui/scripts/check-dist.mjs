#!/usr/bin/env node
// 生产构建产物检查(npm run build 之后执行):
// 1. dist/index.html 资源引用必须以 /assets/ 开头(生产 base 固定为 /);
// 2. 产物不得引用运行时 CDN;
// 3. 产物不得携带任何 Mock 痕迹:扫描 dist 全部文件,命中 MSW worker、
//    fixture 数据或场景标记即失败(生产构建通过 vite alias 排除 mocks,
//    见 vite.config.ts)。
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const dist = 'dist'
const html = readFileSync(join(dist, 'index.html'), 'utf8')

const assetRefs = [...html.matchAll(/(?:src|href)="([^"]+)"/g)].map((m) => m[1])
const local = assetRefs.filter((u) => !u.startsWith('data:'))
if (local.length === 0) {
  console.error('FAIL: dist/index.html 没有本地资源引用')
  process.exit(1)
}
for (const url of local) {
  if (!url.startsWith('/assets/')) {
    console.error(`FAIL: 资源引用未以 /assets/ 开头: ${url}`)
    process.exit(1)
  }
  if (/cdn\.|unpkg|jsdelivr|googleapis/.test(url)) {
    console.error(`FAIL: 产物引用运行时 CDN: ${url}`)
    process.exit(1)
  }
}

if (existsSync(join(dist, 'mockServiceWorker.js'))) {
  console.error('FAIL: dist 携带 mockServiceWorker.js(publicDir 应在 build 时关闭)')
  process.exit(1)
}

// Mock 痕迹标记:任一出现在产物中即说明 mocks 未被排除
const FORBIDDEN = [
  'mockServiceWorker',
  'token-invalid',
  'apply-circuit-broken',
  'partial-services-down',
  '北极星钓具', // fixture 示例数据
  'mockServiceWorker.js',
  'msw/browser',
]

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry)
    if (statSync(path).isDirectory()) {
      yield* walk(path)
    } else {
      yield path
    }
  }
}

let scanned = 0
for (const file of walk(dist)) {
  scanned += 1
  const content = readFileSync(file, 'utf8')
  for (const marker of FORBIDDEN) {
    if (content.includes(marker)) {
      console.error(`FAIL: ${file} 包含 Mock 标记 "${marker}"(mocks 未被生产构建排除)`)
      process.exit(1)
    }
  }
}

console.log(
  `OK: dist 产物 base=/、无 CDN、无 Mock 痕迹(扫描 ${scanned} 个文件,${local.length} 个资源引用)`,
)
