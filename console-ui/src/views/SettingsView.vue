<script setup lang="ts">
// 平台配置页:Vue Console 内编辑非敏感部署配置。
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import { getConfig, postConfig, validateConfig } from '@/api/services'
import type { components } from '@/types/api'
import type { ApiError } from '@/api/errors'

type ConfigViewResponse = components['schemas']['ConfigViewResponse']
type FieldError = components['schemas']['FieldError']

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
</script>

<template>
  <section>
    <div class="d2a-card">
      <div class="settings-head">
        <div>
          <h3 class="card-title">
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
  </section>
</template>

<style scoped>
.settings-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.card-title {
  margin: 0 0 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--d2a-text-primary);
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
</style>
