#!/usr/bin/env node
// 漂移检查:用与 api:generate 相同的生成链重新生成类型并与已提交的
// src/types/api.ts 比较;不一致即失败(改 OpenAPI 后必须 npm run api:generate)。
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const tmp = mkdtempSync(join(tmpdir(), 'd2a-api-types-'))
const out = join(tmp, 'api.ts')

try {
  execFileSync(
    process.execPath,
    ['scripts/generate-api-types.mjs', 'openapi.json', out],
    { stdio: ['ignore', 'ignore', 'inherit'] },
  )
  const generated = readFileSync(out, 'utf8')
  const committed = readFileSync('src/types/api.ts', 'utf8')
  if (generated !== committed) {
    console.error(
      'FAIL: src/types/api.ts 与 openapi.json 不一致;请运行 npm run api:generate 并提交结果',
    )
    process.exit(1)
  }
  console.log('OK: src/types/api.ts 与 openapi.json 一致')
} finally {
  rmSync(tmp, { recursive: true, force: true })
}
