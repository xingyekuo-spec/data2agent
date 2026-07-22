<script setup lang="ts">
// 平台首次配置页。
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import { getSetupStatus, postSetup } from '@/api/services'
import { useSessionStore } from '@/stores/session'
import type { ApiError } from '@/api/errors'
import type { components } from '@/types/api'

type FieldError = components['schemas']['FieldError']

const router = useRouter()
const session = useSessionStore()
const loading = ref(true)
const saving = ref(false)
const error = ref<ApiError | null>(null)
const fieldErrors = ref<FieldError[]>([])
const statusText = ref('')

const form = reactive({
  ingest_token: '',
  console_token: '',
  mcp_token: '',
})

async function refreshStatus(): Promise<void> {
  loading.value = true
  error.value = null
  const result = await getSetupStatus()
  loading.value = false
  if (!result.ok) {
    error.value = result.error
    return
  }
  statusText.value = result.data.home ? `安装目录:${result.data.home}` : ''
  if (!result.data.needs_setup) {
    await router.replace('/')
  }
}

async function submit(): Promise<void> {
  saving.value = true
  fieldErrors.value = []
  error.value = null
  const result = await postSetup({
    ingest_token: form.ingest_token,
    console_token: form.console_token,
    mcp_token: form.mcp_token || null,
  })
  saving.value = false
  if (!result.ok) {
    error.value = result.error
    return
  }
  if (result.data.ok) {
    session.login(form.console_token)
    ElMessage.success(result.data.message)
    await router.replace('/')
  } else {
    fieldErrors.value = result.data.errors
  }
}

onMounted(() => {
  void refreshStatus()
})
</script>

<template>
  <section>
    <div class="d2a-card setup-card">
      <h3 class="card-title">
        平台首次配置
      </h3>
      <p class="card-subtitle">
        配置保存到 <code>config/platform.yaml</code>,Token 写入 <code>config/secrets.env</code>。
      </p>
      <p
        v-if="statusText"
        class="setup-status"
      >
        {{ statusText }}
      </p>

      <LoadingState v-if="loading" />
      <ErrorState
        v-else-if="error"
        :error="error"
        @retry="refreshStatus"
      />
      <el-form
        v-else
        label-width="130px"
        @submit.prevent="submit"
      >
        <el-form-item label="ingest Token">
          <el-input
            v-model="form.ingest_token"
            type="password"
            show-password
            autocomplete="new-password"
            data-testid="setup-ingest-token"
          />
        </el-form-item>
        <el-form-item label="管理 Token">
          <el-input
            v-model="form.console_token"
            type="password"
            show-password
            autocomplete="new-password"
            data-testid="setup-console-token"
          />
        </el-form-item>
        <el-form-item label="MCP Token">
          <el-input
            v-model="form.mcp_token"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="留空则自动生成"
            data-testid="setup-mcp-token"
          />
        </el-form-item>
        <el-alert
          v-if="fieldErrors.length"
          class="setup-errors"
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
        <div class="setup-actions">
          <el-button
            type="primary"
            native-type="submit"
            :loading="saving"
          >
            保存并进入控制台
          </el-button>
        </div>
      </el-form>
    </div>
  </section>
</template>

<style scoped>
.setup-card {
  max-width: 760px;
}

.card-title {
  margin: 0 0 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--d2a-text-primary);
}

.card-subtitle,
.setup-status {
  margin: 0 0 16px;
  color: var(--d2a-text-secondary);
  font-size: 12px;
}

.setup-errors {
  margin: 8px 0 12px;
}

.setup-errors ul {
  margin: 0;
  padding-left: 18px;
}

.setup-actions {
  display: flex;
  justify-content: flex-end;
}
</style>
