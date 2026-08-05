<script setup lang="ts">
// 本体拓扑图(ECharts 按需引入,仿 dashboard/TrendChart 封装):
// 力导向类-属性图谱;节点/边数据由父视图派生,本组件只负责渲染与点击上报;
// jsdom 无 canvas 时静默跳过(init try/catch),不影响挂载与测试。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([GraphChart, LegendComponent, TooltipComponent, CanvasRenderer])

export interface OntologyGraphNode {
  id: string
  name: string
  kind: 'class' | 'property'
  /** 类目下标(领域 + 属性) */
  category: number
  symbol?: string
  symbolSize?: number
  itemStyle?: Record<string, unknown>
  /** 类节点:object 名;属性节点:所属类 object 名 */
  refObject: string
  propName?: string
  /** tooltip 补充(类:states/actions 摘要;属性:类型/敏感) */
  tip?: string
}

export interface OntologyGraphLink {
  source: string
  target: string
  labelText?: string
  lineStyle?: Record<string, unknown>
}

const props = defineProps<{
  nodes: OntologyGraphNode[]
  links: OntologyGraphLink[]
  categories: string[]
  categoryColors: string[]
}>()
const emit = defineEmits<{ 'node-click': [node: OntologyGraphNode] }>()

const el = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function render(): void {
  if (!el.value) {
    return
  }
  if (!chart) {
    try {
      chart = echarts.init(el.value)
    } catch {
      chart = null // jsdom:不渲染也不让挂载崩溃
      return
    }
    chart.on('click', (params) => {
      const data = (params as { data?: OntologyGraphNode }).data
      if (data && (data.kind === 'class' || data.kind === 'property')) {
        emit('node-click', data)
      }
    })
  }
  chart.setOption({
    color: props.categoryColors,
    tooltip: {
      formatter: (p: { data?: OntologyGraphNode }) => {
        const d = p.data
        if (!d) return ''
        return d.tip ? `${d.name}<br/>${d.tip}` : d.name
      },
    },
    legend: {
      data: props.categories,
      bottom: 0,
      itemWidth: 12,
      itemHeight: 12,
      textStyle: { fontSize: 11 },
    },
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      categories: props.categories.map((name) => ({ name })),
      force: { repulsion: 220, edgeLength: [36, 120], gravity: 0.08 },
      label: { show: true, fontSize: 10, position: 'right' },
      edgeLabel: { show: false },
      emphasis: {
        focus: 'adjacency',
        edgeLabel: {
          show: true,
          fontSize: 10,
          formatter: (p: { data?: OntologyGraphLink }) => p.data?.labelText ?? '',
        },
      },
      lineStyle: { color: 'source', curveness: 0.08, opacity: 0.7 },
      data: props.nodes,
      links: props.links,
    }],
  }, { notMerge: true })
}

onMounted(() => {
  render()
  if (el.value && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(el.value)
  }
})

watch(() => [props.nodes, props.links], render, { deep: true })

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div
    ref="el"
    class="ontology-graph"
    data-testid="topology-graph"
  />
</template>

<style scoped>
.ontology-graph {
  width: 100%;
  height: 540px;
}
</style>
