<script setup lang="ts">
// 平台日志页。
import { onMounted, reactive, ref } from 'vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import { getLogs } from '@/api/services'
import type { ApiError } from '@/api/errors'

const services = [
  { label: 'ingest', value: 'ingest' },
  { label: 'apply', value: 'apply' },
  { label: 'mcp', value: 'mcp' },
  { label: 'console', value: 'console' },
]

const form = reactive({
  service: 'console',
  lines: 200,
  level: '',
})
const loading = ref(false)
const text = ref('')
const ok = ref<boolean | null>(null)
const error = ref<ApiError | null>(null)

async function refresh(): Promise<void> {
  loading.value = true
  error.value = null
  const result = await getLogs({
    service: form.service,
    lines: form.lines,
    level: form.level || null,
  })
  loading.value = false
  if (!result.ok) {
    error.value = result.error
    return
  }
  ok.value = result.data.ok
  text.value = result.data.text || '(无日志)'
}

onMounted(() => {
  void refresh()
})
</script>

<template>
  <section>
    <div class="d2a-card">
      <div class="logs-toolbar">
        <el-select
          v-model="form.service"
          style="width: 160px"
          @change="refresh"
        >
          <el-option
            v-for="item in services"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
        <el-input-number
          v-model="form.lines"
          :min="1"
          :max="1000"
          :step="50"
          controls-position="right"
          @change="refresh"
        />
        <el-input
          v-model="form.level"
          placeholder="level 过滤,如 ERROR"
          style="width: 200px"
          clearable
          @keyup.enter="refresh"
          @clear="refresh"
        />
        <el-button
          type="primary"
          :loading="loading"
          @click="refresh"
        >
          刷新
        </el-button>
        <el-tag
          v-if="ok !== null"
          :type="ok ? 'success' : 'warning'"
        >
          {{ ok ? '已读取' : '日志不可用' }}
        </el-tag>
      </div>

      <ErrorState
        v-if="error"
        :error="error"
        @retry="refresh"
      />
      <pre
        v-else
        class="logs-output"
        data-testid="logs-output"
      >{{ loading && !text ? '加载中…' : text }}</pre>
    </div>
  </section>
</template>

<style scoped>
.logs-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.logs-output {
  min-height: 420px;
  max-height: calc(100vh - 180px);
  margin: 0;
  padding: 12px;
  overflow: auto;
  border: 1px solid var(--d2a-border);
  border-radius: 6px;
  background: #0d1117;
  color: #c9d1d9;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
}
</style>
