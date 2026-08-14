<script setup lang="ts">
// 平台配置页:Vue Console 内编辑非敏感部署配置 + 便携包在线升级。
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import {
  getConfig,
  getUpdateStatus,
  postConfig,
  postUpdateCheck,
  postUpdateDownload,
  validateConfig,
} from '@/api/services'
import type { components } from '@/types/api'
import type { ApiError } from '@/api/errors'

type ConfigViewResponse = components['schemas']['ConfigViewResponse']
type FieldError = components['schemas']['FieldError']
type UpdateStatusResponse = components['schemas']['UpdateStatusResponse']
type UpdateCheckResponse = components['schemas']['UpdateCheckResponse']

const loading = ref(false)
const saving = ref(false)
const validating = ref(false)
const config = ref<ConfigViewResponse | null>(null)
const error = ref<ApiError | null>(null)
const fieldErrors = ref<FieldError[]>([])
const form = reactive({
  templates: '',
  landing: '',
})

const needsSetup = computed(() => config.value?.needs_setup === true)

function applyConfig(data: ConfigViewResponse): void {
  config.value = data
  form.templates = data.templates
  form.landing = data.landing
}

async function refresh(): Promise<void> {
  loading.value = true
  error.value = null
  const result = await getConfig()
  loading.value = false
  if (result.ok) {
    applyConfig(result.data)
  } else {
    error.value = result.error
  }
}

async function validateOnly(): Promise<boolean> {
  validating.value = true
  fieldErrors.value = []
  const result = await validateConfig({
    templates: form.templates,
    landing: form.landing,
  })
  validating.value = false
  if (!result.ok) {
    error.value = result.error
    return false
  }
  fieldErrors.value = result.data.errors ?? []
  if (result.data.ok) {
    ElMessage.success('配置校验通过')
  }
  return result.data.ok
}

async function save(): Promise<void> {
  saving.value = true
  fieldErrors.value = []
  const result = await postConfig({
    templates: form.templates,
    landing: form.landing,
  })
  saving.value = false
  if (!result.ok) {
    error.value = result.error
    return
  }
  fieldErrors.value = result.data.errors ?? []
  if (result.data.ok) {
    ElMessage.success(result.data.restart_required ? '已保存,请重启平台进程后生效' : '已保存')
    await refresh()
  }
}

onMounted(() => {
  void refresh()
})

// ---- 便携包在线升级 ----

const update = ref<UpdateStatusResponse | null>(null)
const updateChecking = ref(false)
const updateDownloading = ref(false)
const checkResult = ref<UpdateCheckResponse | null>(null)
let pollTimer: number | undefined

const updatePhase = computed(() => update.value?.phase ?? 'idle')
const downloadPercent = computed(() => {
  const done = update.value?.progress_done ?? 0
  const total = update.value?.progress_total ?? 0
  if (!done || !total) return 0
  return Math.min(100, Math.round((done / total) * 100))
})

async function refreshUpdateStatus(): Promise<void> {
  const result = await getUpdateStatus()
  if (result.ok) update.value = result.data
}

async function checkUpdate(): Promise<void> {
  updateChecking.value = true
  checkResult.value = null
  const result = await postUpdateCheck({})
  updateChecking.value = false
  if (!result.ok) {
    ElMessage.error(`检查更新失败:${result.error.message}`)
    return
  }
  checkResult.value = result.data
  await refreshUpdateStatus()
}

function stopPolling(): void {
  window.clearInterval(pollTimer)
  pollTimer = undefined
  updateDownloading.value = false
}

async function startDownload(): Promise<void> {
  const result = await postUpdateDownload()
  if (!result.ok) {
    ElMessage.error(`下载失败:${result.error.message}`)
    return
  }
  updateDownloading.value = true
  pollTimer = window.setInterval(() => {
    void refreshUpdateStatus().then(() => {
      if (updatePhase.value === 'ready' || updatePhase.value === 'failed') {
        stopPolling()
      }
    })
  }, 1000)
}

onMounted(() => {
  void refreshUpdateStatus()
})
onUnmounted(stopPolling)
</script>

<template>
  <section>
    <div class="d2a-card">
      <div class="settings-head">
        <div>
          <h3 class="card-title card-title--compact">
            平台配置
          </h3>
          <p class="card-subtitle">
            敏感 Token 存放在 <code>config/secrets.env</code>;本页只编辑非敏感路径配置。
          </p>
          <p
            v-if="config"
            class="app-version"
            data-testid="settings-app-version"
          >
            当前应用版本：v{{ config.app_version }}<span v-if="config.build_version">（构建 {{ config.build_version }}）</span>
          </p>
        </div>
        <el-button
          :loading="loading"
          @click="refresh"
        >
          刷新
        </el-button>
      </div>

      <LoadingState v-if="loading && !config" />
      <ErrorState
        v-else-if="error"
        :error="error"
        @retry="refresh"
      />
      <template v-else>
        <EmptyState
          v-if="needsSetup"
          title="尚未完成首次配置"
          hint="请打开 /setup 完成平台首次配置"
        />
        <el-form
          v-else
          class="settings-form"
          label-width="110px"
          @submit.prevent
        >
          <el-form-item label="templates">
            <el-input
              v-model="form.templates"
              data-testid="settings-templates"
            />
          </el-form-item>
          <el-form-item label="landing">
            <el-input
              v-model="form.landing"
              data-testid="settings-landing"
            />
          </el-form-item>
          <el-alert
            v-if="fieldErrors.length"
            class="settings-errors"
            type="error"
            :closable="false"
          >
            <ul>
              <li
                v-for="item in fieldErrors"
                :key="`${item.field}:${item.message}`"
              >
                {{ item.field || '配置' }}:{{ item.message }}
              </li>
            </ul>
          </el-alert>
          <div class="settings-actions">
            <el-button
              :loading="validating"
              @click="validateOnly"
            >
              校验
            </el-button>
            <el-button
              type="primary"
              :loading="saving"
              @click="save"
            >
              保存配置
            </el-button>
          </div>
        </el-form>
      </template>
    </div>

    <div
      v-if="update?.available"
      class="d2a-card update-card"
      data-testid="update-card"
    >
      <h3 class="card-title card-title--compact">
        版本升级
      </h3>
      <p class="card-subtitle">
        升级不触碰 config/ 配置与 data/ 数据;失败自动回滚。请在平台空闲时段升级。
      </p>

      <el-alert
        v-if="updatePhase === 'ready'"
        type="success"
        :closable="false"
        data-testid="update-ready"
      >
        <template #title>
          更新包 {{ update?.target_version }} 已就绪
        </template>
        <p>
          请从托盘退出程序(右键托盘图标 →「退出」),然后双击便携包目录中的「升级.bat」完成安装。
        </p>
        <p
          v-if="update?.bat_path"
          class="update-bat-path"
        >
          {{ update.bat_path }}
        </p>
      </el-alert>

      <el-alert
        v-else-if="updatePhase === 'applied'"
        type="success"
        :closable="false"
        data-testid="update-applied"
      >
        <template #title>
          已升级到 {{ update?.current_version }}
        </template>
        <p>
          更新已应用并生效。确认运行正常后,可手动删除便携包目录中的
          runtime.old / app.old / data2agent.exe.old。
        </p>
      </el-alert>

      <template v-else>
        <div
          v-if="updateDownloading || updatePhase === 'downloading'"
          class="update-progress"
          data-testid="update-progress"
        >
          <el-progress :percentage="downloadPercent" />
          <p class="update-progress-text">
            正在下载更新包 {{ update?.target_version }}…下载完成后会提示下一步。
          </p>
        </div>

        <el-alert
          v-if="updatePhase === 'failed' && update?.error"
          class="update-alert"
          type="error"
          :title="update.error"
          :closable="false"
          data-testid="update-failed"
        />
        <el-alert
          v-if="checkResult && !checkResult.ok"
          class="update-alert"
          type="error"
          :title="`检查更新失败:${checkResult.error ?? ''}`"
          :closable="false"
          data-testid="update-check-error"
        />
        <el-alert
          v-else-if="checkResult?.ok && !checkResult.update_available"
          class="update-alert"
          type="info"
          title="当前已是最新版本"
          :closable="false"
          data-testid="update-latest"
        />
        <el-alert
          v-else-if="checkResult?.blocked_reason"
          class="update-alert"
          type="error"
          :title="checkResult.blocked_reason"
          :closable="false"
          data-testid="update-blocked"
        />
        <p
          v-if="checkResult?.notes"
          class="update-notes"
        >
          {{ checkResult.notes }}
        </p>

        <div class="settings-actions">
          <el-button
            :loading="updateChecking"
            data-testid="update-check"
            @click="checkUpdate"
          >
            检查更新
          </el-button>
          <el-button
            v-if="checkResult?.ok && checkResult.update_available && checkResult.protocol_ok"
            type="primary"
            :loading="updateDownloading"
            data-testid="update-download"
            @click="startDownload"
          >
            下载更新 v{{ checkResult.latest_version }}
          </el-button>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.settings-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.card-subtitle {
  margin: 0 0 16px;
  color: var(--d2a-text-secondary);
  font-size: 12px;
}

.app-version {
  margin: -8px 0 16px;
  color: var(--d2a-text-secondary);
  font-size: 12px;
}

.settings-form {
  max-width: 720px;
}

.settings-errors {
  margin: 8px 0 12px;
}

.settings-errors ul {
  margin: 0;
  padding-left: 18px;
}

.settings-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.update-card {
  margin-top: 16px;
}

.update-alert {
  margin: 8px 0 12px;
}

.update-progress {
  max-width: 480px;
  margin: 8px 0 12px;
}

.update-progress-text {
  margin: 6px 0 0;
  color: var(--d2a-text-secondary);
  font-size: 12px;
}

.update-bat-path {
  margin: 6px 0 0;
  font-family: monospace;
  font-size: 12px;
}

.update-notes {
  margin: 8px 0;
  color: var(--d2a-text-secondary);
  font-size: 12px;
  white-space: pre-wrap;
}
</style>
