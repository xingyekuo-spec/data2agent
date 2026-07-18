<script setup lang="ts">
// 最近 24h 抽取趋势(ECharts 按需引入):空数据不绘制假柱;
// init/resize/dispose 完整生命周期;提供无障碍文本。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import EmptyState from '@/components/shared/EmptyState.vue'

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

export interface TrendPoint {
  bucket: string
  rows: number
  runs: number
}

const props = defineProps<{
  points: TrendPoint[]
}>()

const el = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function bucketLabel(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : `${String(d.getHours()).padStart(2, '0')}:00`
}

function render(): void {
  if (!el.value || props.points.length === 0) {
    return
  }
  if (!chart) {
    try {
      chart = echarts.init(el.value)
    } catch {
      // 无 canvas 的环境(如 jsdom):不渲染也不让挂载崩溃
      chart = null
      return
    }
  }
  chart.setOption({
    grid: { left: 40, right: 12, top: 12, bottom: 24 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: props.points.map((p) => bucketLabel(p.bucket)),
      axisLabel: { fontSize: 10 },
    },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{ type: 'bar', name: '抽取行数', data: props.points.map((p) => p.rows) }],
  })
}

onMounted(() => {
  render()
  if (el.value && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(el.value)
  }
})

watch(() => props.points, render, { deep: true })

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="trend-chart">
    <EmptyState
      v-if="points.length === 0"
      title="暂无趋势数据"
      hint="最近 24 小时没有同步运行"
    />
    <div
      v-show="points.length > 0"
      ref="el"
      class="trend-chart__canvas"
      role="img"
      aria-label="最近 24 小时抽取趋势柱状图"
      data-testid="trend-chart"
    />
  </div>
</template>

<style scoped>
.trend-chart__canvas {
  width: 100%;
  height: 180px;
}
</style>
