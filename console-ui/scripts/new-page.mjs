#!/usr/bin/env node
/**
 * A 类列表页脚手架(规范 §3.2):生成合规的视图 + 测试骨架,新页面从规范起步。
 *
 * 用法: node scripts/new-page.mjs <ViewName> <路由路径> <中文标题>
 *   例: node scripts/new-page.mjs SupplierView /data/suppliers 供应商
 *
 * 生成后仍需手动:
 *  1. scripts/check-page-structure.mjs 的 PAGE_TYPES 登记类型;
 *  2. router/index.ts 的 viewComponents + NAV_GROUPS;
 *  3. store 与 API 接入(骨架中以 TODO 标注)。
 */
import { writeFileSync, existsSync } from 'node:fs'

const [name, path, title] = process.argv.slice(2)
if (!name || !path || !title || !/^[A-Z][A-Za-z]+$/.test(name)) {
  console.error('用法: node scripts/new-page.mjs <ViewName(大驼峰)> <路由路径> <中文标题>')
  process.exit(2)
}
const viewFile = `src/views/${name}.vue`
const testFile = `src/views/${name}.test.ts`
if (existsSync(viewFile)) {
  console.error(`${viewFile} 已存在,不覆盖`)
  process.exit(1)
}

writeFileSync(viewFile, `<script setup lang="ts">
// ${title}(A 类列表页,规范 05-console §3.2)。
import { onMounted, ref } from 'vue'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import Pager from '@/components/shared/PagerBar.vue'

// TODO: 接入 store(列表状态/筛选/分页)
const loading = ref(false)
const items = ref<unknown[]>([])
const total = ref(0)
const page = { offset: 0, limit: 50 }
const drawerOpen = ref(false)

function refresh(): void {
  // TODO: 调 store 刷新
}

function onPagerChange(offset: number, limit: number): void {
  page.offset = offset
  page.limit = limit
  refresh()
}

function openRow(_row: unknown): void {
  // TODO: 加载详情后打开抽屉(详情入口一律行点击,规范 §3.2-3)
  drawerOpen.value = true
}

onMounted(refresh)
</script>

<template>
  <section class="d2a-page-flush">
    <!-- 通栏工具栏:左筛选、右主操作 -->
    <div class="d2a-card d2a-toolbar">
      <!-- TODO: 筛选控件(select 140px / input 220px,全局已约定) -->
      <div class="d2a-toolbar__actions">
        <el-button
          size="small"
          data-testid="${name.replace(/View$/, '').toLowerCase()}-refresh"
          @click="refresh"
        >
          刷新
        </el-button>
      </div>
    </div>

    <!-- 表格卡片:三态 + 表格 + Pager -->
    <div class="d2a-card">
      <LoadingState v-if="loading" />
      <EmptyState
        v-else-if="items.length === 0"
        title="暂无数据"
      />
      <template v-else>
        <!-- TODO: el-table 列定义;状态列用 StatusBadge;空值 —;
             行点击 @row-click 开详情;操作列只放真实动作(禁详情/浏览按钮) -->
        <el-table
          :data="items"
          size="small"
          @row-click="openRow"
        />
        <Pager
          :total="total"
          :limit="page.limit"
          :offset="page.offset"
          @change="onPagerChange"
        />
      </template>
    </div>

    <!-- 详情抽屉(规范 §3.2-5):三态 + 刷新失败警告;安全 JSON 折叠在抽屉内底部 -->
    <el-drawer
      v-model="drawerOpen"
      title="详情"
      size="560px"
    >
      <!-- TODO: 详情内容(summary dl + 小节表格 + 安全 JSON toggle) -->
    </el-drawer>
  </section>
</template>
`, 'utf-8')

writeFileSync(testFile, `import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia, type Pinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import ${name} from './${name}.vue'

// 注意:locale 必须与生产一致(zhCn),否则分页等文案渲染为英文
function mountView(): ReturnType<typeof mount> {
  const pinia: Pinia = createPinia()
  return mount(${name}, { global: { plugins: [pinia, [ElementPlus, { locale: zhCn }]] } })
}

describe('${name}(${title})', () => {
  beforeEach(() => setScenario('healthy'))

  it('healthy:渲染工具栏与空态/表格', async () => {
    const wrapper = mountView()
    await flushPromises()
    // TODO: 按 fixture 断言三态与关键内容
    expect(wrapper.find('.d2a-toolbar').exists()).toBe(true)
  })
})
`, 'utf-8')

console.log(`已生成:
  ${viewFile}
  ${testFile}
待办:
  1. scripts/check-page-structure.mjs → PAGE_TYPES 登记 '${name}': 'A'
  2. router/index.ts → viewComponents + NAV_GROUPS(数据管理?)
  3. 接入 store/API(骨架中 TODO 处)
  4. npm run check:pages 验证`)
