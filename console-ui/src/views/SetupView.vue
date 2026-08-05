<script setup lang="ts">
// 平台首次配置页。
// 成功后不立即跳转:展示常驻完成面板与重启指引(启动器只在启动时拉起
// ingest/apply/mcp,首配后需重启应用才会启动后台服务,否则管道页推送/
// MCP 节点报失败——05-console 设计缺口,由本页提示兜底)。
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
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
const doneMessage = ref('')

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
    // 不立即跳转:展示重启指引,由用户确认后进入控制台
    doneMessage.value = result.data.message
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
    <div
      v-if="doneMessage"
      class="d2a-card setup-card"
      data-testid="setup-done"
    >
      <h3 class="card-title card-title--compact">
        配置已保存
      </h3>
      <p class="card-subtitle">
        {{ doneMessage }}
      </p>
      <el-alert
        type="warning"
        :closable="false"
        class="restart-notice"
      >
        <p><strong>请重启应用以启动后台服务</strong>(退出后重新双击 data2agent.exe,或重启 Windows 服务)。</p>
        <p>
          数据接收(ingest)、物化(apply)、MCP 等后台服务在重启后才会启动;
          未重启前,管道状态页的「推送」「MCP 网关」节点显示失败属预期,重启后自动恢复。
        </p>
      </el-alert>
      <div class="setup-actions">
        <el-button
          type="primary"
          data-testid="setup-enter"
          @click="router.replace('/')"
        >
          已了解,进入控制台
        </el-button>
      </div>
    </div>
    <div
      v-else
      class="d2a-card setup-card"
    >
      <h3 class="card-title card-title--compact">
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

.restart-notice {
  margin: 0 0 12px;
}

.restart-notice p {
  margin: 0;
  line-height: 1.6;
}
</style>
