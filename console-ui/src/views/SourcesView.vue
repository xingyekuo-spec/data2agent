<script setup lang="ts">
// 数据源管理:平台已接入源的登记式清单 + 详情抽屉 + 添加引导。
// 边界:连接配置与凭据在中间机(安全拓扑),本页只读观测,不提供编辑。
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { Refresh, Plus, Key } from '@element-plus/icons-vue'
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import {
  getIngestConnectionInfo,
  postIngestTokenReveal,
  postSourceDisable,
  postSourceEnable,
  postSourceRegister,
  postSourceTokenReset,
} from '@/api/services'
import { useSourcesStore, type SourceCard } from '@/stores/sources'
import { formatDateTime } from '@/utils/time'
import { formatDuration } from '@/utils/format'
import type { components } from '@/types/api'
import type { HealthStatus } from '@/types/state'

type IngestConnectionInfo = components['schemas']['IngestConnectionInfo']
type SourceRegistered = components['schemas']['SourceRegistered']

const store = useSourcesStore()
const { cards, detail, detailSource } = storeToRefs(store)

const addDialogVisible = ref(false)

// ---- 中间机接入信息(供中间机 sink 配置填写)----
const connDialogVisible = ref(false)
const connInfo = ref<IngestConnectionInfo | null>(null)
const connLoading = ref(false)
const revealedToken = ref<string | null>(null)
const revealLoading = ref(false)

async function openConnectionInfo(): Promise<void> {
  connDialogVisible.value = true
  revealedToken.value = null
  if (connInfo.value) return
  connLoading.value = true
  const result = await getIngestConnectionInfo()
  connLoading.value = false
  if (!result.ok) {
    ElMessage.error(`接入信息加载失败:${result.error.message}`)
    return
  }
  connInfo.value = result.data
}

async function revealToken(): Promise<void> {
  revealLoading.value = true
  const result = await postIngestTokenReveal()
  revealLoading.value = false
  if (!result.ok) {
    ElMessage.error(result.error.message)
    return
  }
  revealedToken.value = result.data.token
}

async function copyText(text: string, label: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(`${label}已复制`)
  } catch {
    ElMessage.warning('复制失败,请手动选择复制')
  }
}

function sinkSnippet(): string {
  if (!connInfo.value) return ''
  const tokenLine = revealedToken.value
    ? `D2A_INGEST_TOKEN=${revealedToken.value}`
    : 'D2A_INGEST_TOKEN=<点「显示明文」填入>'
  return [
    '# 中间机 connect.yaml(数据源 sink 段):',
    'sink:',
    '  type: http',
    `  url: "${connInfo.value.endpoint}"`,
    '  token_env: D2A_INGEST_TOKEN',
    '',
    '# 中间机 config/secrets.env:',
    tokenLine,
  ].join('\n')
}

/** 签发成功后的专属配置片段(source 与 Token 已填好) */
function registerSnippet(r: SourceRegistered): string {
  return [
    '# 中间机 connect.yaml:',
    'sources:',
    `  ${r.source}:`,
    '    adapter: mssql_readonly',
    '    dsn_env: D2A_ERP_DSN',
    '    apply_after_sync: false',
    '    sink:',
    '      type: http',
    `      url: "${r.endpoint}"`,
    '      token_env: D2A_INGEST_TOKEN',
    '',
    '# 中间机 config/secrets.env:',
    `D2A_INGEST_TOKEN=${r.token}`,
  ].join('\n')
}

// ---- 添加数据源(平台登记签发)----
const registerForm = reactive({
  source: '',
  display_name: '',
  source_type: 'erp' as 'erp' | 'unknown',
  note: '',
})
const registering = ref(false)
const registerError = ref<string | null>(null)
const registered = ref<SourceRegistered | null>(null)

function openAddDialog(): void {
  registered.value = null
  registerError.value = null
  addDialogVisible.value = true
}

const SOURCE_IDENT = /^[a-z][a-z0-9_]{2,31}$/

async function submitRegister(): Promise<void> {
  const src = registerForm.source.trim()
  // 前端预检:不合规直接给人话提示,不让用户吃到原始 422
  if (!SOURCE_IDENT.test(src)) {
    registerError.value =
      `「${src}」不是合法的源标识:须以小写字母开头,只用小写字母/数字/下划线,`
      + '3~32 位(如 kunshan_e10);不支持中文、大写、空格和横线'
    return
  }
  registering.value = true
  registerError.value = null
  const result = await postSourceRegister({
    source: src,
    display_name: registerForm.display_name.trim() || null,
    source_type: registerForm.source_type,
    note: registerForm.note.trim() || null,
  })
  registering.value = false
  if (!result.ok) {
    registerError.value = result.error.message
    return
  }
  registered.value = result.data
  void store.refresh()
}

// ---- 登记源管理动作(停用/启用/重置 Token)----
const actionLoading = ref(false)
const resetTokenResult = ref<{ source: string; token: string } | null>(null)

async function toggleStatus(card: SourceCard): Promise<void> {
  const disabling = card.registry_status === 'active'
  if (disabling) {
    try {
      await ElMessageBox.confirm(
        `停用后 ${card.source} 的推送将被平台拒绝(历史数据保留)。确认停用?`,
        '停用数据源',
        { type: 'warning', confirmButtonText: '停用', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  actionLoading.value = true
  const result = disabling
    ? await postSourceDisable(card.source)
    : await postSourceEnable(card.source)
  actionLoading.value = false
  if (!result.ok) {
    ElMessage.error(result.error.message)
    return
  }
  ElMessage.success(disabling ? '已停用' : '已启用')
  void store.refresh()
  void store.openDetail(card.source)
}

async function resetToken(card: SourceCard): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `重置后旧 Token 立即失效,中间机必须换用新 Token 才能继续推送。确认重置?`,
      '重置推送 Token',
      { type: 'warning', confirmButtonText: '重置', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  actionLoading.value = true
  const result = await postSourceTokenReset(card.source)
  actionLoading.value = false
  if (!result.ok) {
    ElMessage.error(result.error.message)
    return
  }
  resetTokenResult.value = result.data
}

const ACCESS_MODE_LABELS: Record<SourceCard['access_mode'], string> = {
  push: '中间机推送',
  local: '本地直连',
  unknown: '未知',
}

const SOURCE_TYPE_LABELS: Record<SourceCard['source_type'], string> = {
  erp: 'ERP',
  unknown: '未登记',
}

function statusOf(card: SourceCard): HealthStatus {
  return card.status as HealthStatus
}

/** 当前详情对应的卡片(管理动作用) */
function detailCard(): SourceCard | null {
  if (!detailSource.value || cards.value.status !== 'success') return null
  return cards.value.data.find((c) => c.source === detailSource.value) ?? null
}

onMounted(() => {
  void store.refresh()
})
</script>

<template>
  <section>
    <div class="d2a-card">
      <header class="page-header">
        <div>
          <h3 class="card-title card-title--compact">
            数据源管理
          </h3>
          <p class="page-desc">
            平台已接入的数据源。连接与凭据在中间机维护,平台只读观测;
            新增源请在中间机管理页完成配置后自动出现在此清单。
          </p>
        </div>
        <div class="page-actions">
          <el-button
            :icon="Key"
            data-testid="connection-info"
            @click="openConnectionInfo"
          >
            接入信息
          </el-button>
          <el-button
            :icon="Plus"
            data-testid="source-add"
            @click="openAddDialog"
          >
            添加数据源
          </el-button>
          <el-button
            :icon="Refresh"
            data-testid="sources-refresh"
            @click="store.refresh()"
          >
            刷新
          </el-button>
        </div>
      </header>

      <LoadingState v-if="cards.status === 'idle' || cards.status === 'loading'" />
      <ErrorState
        v-else-if="cards.status === 'error'"
        :error="cards.error"
        @retry="store.refresh()"
      />
      <EmptyState
        v-else-if="cards.data.length === 0"
        title="尚未接入数据源"
        hint="在中间机管理页配置连接并选择要同步的表后,源会出现在这里"
        data-testid="sources-empty"
      />
      <div
        v-else
        class="source-grid"
        data-testid="source-grid"
      >
        <button
          v-for="card in cards.data"
          :key="card.source"
          type="button"
          class="source-card"
          :data-testid="`source-card-${card.source}`"
          @click="store.openDetail(card.source)"
        >
          <div class="source-card__head">
            <span class="source-card__name">{{ card.display_name }}</span>
            <StatusBadge :status="statusOf(card)" />
          </div>
          <div class="source-card__tags">
            <el-tag size="small">
              {{ SOURCE_TYPE_LABELS[card.source_type] }}
            </el-tag>
            <el-tag
              size="small"
              :type="card.access_mode === 'push' ? 'success' : 'info'"
            >
              {{ ACCESS_MODE_LABELS[card.access_mode] }}
            </el-tag>
            <el-tag
              v-if="card.registry_status === 'disabled'"
              size="small"
              type="danger"
            >
              已停用
            </el-tag>
            <el-tag
              v-else-if="card.registered"
              size="small"
              type="success"
            >
              已签发
            </el-tag>
            <el-tag
              v-else
              size="small"
              type="info"
            >
              未登记
            </el-tag>
            <el-tag
              v-if="card.quarantined > 0"
              size="small"
              type="warning"
            >
              待确认 {{ card.quarantined }}
            </el-tag>
          </div>
          <div class="source-card__meta">
            <span>{{ card.tables }} 张表</span>
            <span v-if="card.last_run_at">最近接入 {{ formatDateTime(card.last_run_at) }}</span>
            <span v-else>从未接入</span>
          </div>
          <p
            v-if="card.status_reason"
            class="source-card__reason"
          >
            {{ card.status_reason }}
          </p>
        </button>
      </div>
    </div>

    <!-- 详情抽屉:表级水位 + 最近运行 -->
    <el-drawer
      :model-value="detail !== null"
      :title="detailSource ? `数据源详情:${detailSource}` : '数据源详情'"
      size="640px"
      data-testid="source-detail-drawer"
      @close="store.closeDetail()"
    >
      <LoadingState v-if="detail?.status === 'loading'" />
      <ErrorState
        v-else-if="detail?.status === 'error'"
        :error="detail.error"
        @retry="detailSource && store.openDetail(detailSource)"
      />
      <template v-else-if="detail?.status === 'success'">
        <h4 class="drawer-section-title">
          接入表({{ detail.data.table_states.length }})
        </h4>
        <el-table
          :data="detail.data.table_states"
          size="small"
          data-testid="source-tables"
        >
          <el-table-column
            prop="table_name"
            label="表名"
            min-width="160"
          />
          <el-table-column
            prop="watermark_col"
            label="水位"
            min-width="150"
          >
            <template #default="{ row }">
              {{ row.high_water ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="行数"
            width="90"
            align="right"
          >
            <template #default="{ row }">
              {{ row.rows ?? '不可检测' }}
            </template>
          </el-table-column>
          <el-table-column
            label="最近接入"
            min-width="130"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.last_run_at) || '—' }}
            </template>
          </el-table-column>
        </el-table>

        <h4 class="drawer-section-title">
          最近接入记录
        </h4>
        <el-table
          :data="detail.data.recent_runs"
          size="small"
          data-testid="source-runs"
        >
          <el-table-column
            prop="id"
            label="#"
            width="60"
          />
          <el-table-column
            label="状态"
            width="90"
          >
            <template #default="{ row }">
              <StatusBadge :status="row.status ?? 'unknown'" />
            </template>
          </el-table-column>
          <el-table-column
            label="行数"
            width="90"
            align="right"
          >
            <template #default="{ row }">
              {{ row.rows ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="耗时"
            width="90"
            align="right"
          >
            <template #default="{ row }">
              {{ row.duration_ms != null ? formatDuration(Math.round(row.duration_ms / 1000)) : '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="开始时间"
            min-width="130"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.started_at) }}
            </template>
          </el-table-column>
        </el-table>

        <template v-if="detailCard()?.registered">
          <h4 class="drawer-section-title">
            登记管理
          </h4>
          <div class="drawer-actions">
            <el-button
              size="small"
              :type="detailCard()?.registry_status === 'active' ? 'danger' : 'success'"
              :loading="actionLoading"
              data-testid="source-toggle-status"
              @click="detailCard() && toggleStatus(detailCard()!)"
            >
              {{ detailCard()?.registry_status === 'active' ? '停用' : '启用' }}
            </el-button>
            <el-button
              size="small"
              :loading="actionLoading"
              data-testid="source-token-reset"
              @click="detailCard() && resetToken(detailCard()!)"
            >
              重置 Token
            </el-button>
          </div>
          <el-alert
            v-if="resetTokenResult"
            class="reset-result"
            type="success"
            :closable="false"
            data-testid="reset-token-result"
          >
            <template #title>
              新 Token(仅此一次):
              <code>{{ resetTokenResult.token }}</code>
              <el-button
                link
                type="primary"
                @click="copyText(resetTokenResult!.token, 'Token')"
              >
                复制
              </el-button>
            </template>
          </el-alert>
        </template>
      </template>
    </el-drawer>

    <!-- 中间机接入信息:端点 / Token / 协议版本,供中间机 sink 配置 -->
    <el-dialog
      v-model="connDialogVisible"
      title="中间机接入信息"
      width="560px"
      data-testid="connection-info-dialog"
    >
      <LoadingState v-if="connLoading" />
      <template v-else-if="connInfo">
        <el-descriptions
          :column="1"
          border
          size="small"
          data-testid="connection-info-table"
        >
          <el-descriptions-item label="接收端点">
            <code>{{ connInfo.endpoint }}</code>
            <el-button
              link
              type="primary"
              @click="copyText(connInfo!.endpoint, '端点')"
            >
              复制
            </el-button>
          </el-descriptions-item>
          <el-descriptions-item label="推送 Token">
            <template v-if="connInfo.token_configured">
              <code>{{ revealedToken ?? connInfo.token_masked }}</code>
              <el-button
                v-if="!revealedToken"
                link
                type="primary"
                :loading="revealLoading"
                data-testid="token-reveal"
                @click="revealToken"
              >
                显示明文
              </el-button>
              <el-button
                v-else
                link
                type="primary"
                @click="copyText(revealedToken, 'Token')"
              >
                复制
              </el-button>
            </template>
            <span v-else>未配置(平台 config/secrets.env 设 D2A_INGEST_TOKEN)</span>
          </el-descriptions-item>
          <el-descriptions-item label="协议版本">
            首选 {{ connInfo.active_protocol_version }},接受
            {{ connInfo.supported_protocol_versions.join(' / ') }}
          </el-descriptions-item>
        </el-descriptions>

        <h4 class="drawer-section-title">
          中间机配置片段
        </h4>
        <pre
          class="conn-snippet"
          data-testid="connection-snippet"
        >{{ sinkSnippet() }}</pre>
        <p class="page-desc">
          将以上片段填入中间机 connect.yaml 与 config/secrets.env;
          出示明文会记入平台访问审计。
        </p>
      </template>
    </el-dialog>

    <!-- 添加数据源:平台登记签发 → 中间机照抄配置 -->
    <el-dialog
      v-model="addDialogVisible"
      :title="registered ? '数据源已登记' : '添加数据源'"
      width="560px"
      data-testid="source-add-dialog"
    >
      <template v-if="!registered">
        <p class="page-desc">
          平台登记并签发专属推送 Token;把生成的配置片段抄到中间机即可开推。
          当前支持 ERP(鼎捷 E10 / 易飞);MES / SRM / Excel / API 规划中。
        </p>
        <el-form
          label-width="110px"
          @submit.prevent="submitRegister"
        >
          <el-form-item
            label="源标识"
            required
          >
            <el-input
              v-model="registerForm.source"
              placeholder="如 kunshan_e10(厂区_系统)"
              data-testid="register-source"
            />
            <span class="form-hint">小写字母开头,只用小写字母/数字/下划线,3~32 位;不支持中文/大写/横线。登记后不可改</span>
          </el-form-item>
          <el-form-item label="展示名">
            <el-input
              v-model="registerForm.display_name"
              placeholder="如 昆山厂 E10(可选)"
              data-testid="register-display-name"
            />
          </el-form-item>
          <el-form-item label="类型">
            <el-select
              v-model="registerForm.source_type"
              data-testid="register-type"
            >
              <el-option
                label="ERP"
                value="erp"
              />
              <el-option
                label="其他/未登记"
                value="unknown"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="备注">
            <el-input
              v-model="registerForm.note"
              placeholder="可选"
            />
          </el-form-item>
          <el-alert
            v-if="registerError"
            class="form-error"
            type="error"
            :closable="false"
            :title="registerError"
            data-testid="register-error"
          />
          <div class="dialog-actions">
            <el-button
              type="primary"
              native-type="submit"
              :loading="registering"
              :disabled="!registerForm.source.trim()"
              data-testid="register-submit"
            >
              登记并签发 Token
            </el-button>
          </div>
        </el-form>
      </template>
      <template v-else>
        <el-alert
          type="warning"
          :closable="false"
          title="Token 仅此显示一次,平台只存哈希;丢失只能重置。"
        />
        <h4 class="drawer-section-title">
          中间机配置片段
        </h4>
        <pre
          class="conn-snippet"
          data-testid="register-snippet"
        >{{ registerSnippet(registered) }}</pre>
        <div class="dialog-actions">
          <el-button
            type="primary"
            data-testid="register-copy"
            @click="copyText(registerSnippet(registered!), '配置片段')"
          >
            复制配置片段
          </el-button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.page-header {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.page-desc {
  margin: 4px 0 0;
  font-size: var(--d2a-font-sm);
  color: var(--d2a-text-secondary);
}

.source-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
}

.source-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px 16px;
  border: 1px solid var(--d2a-border);
  border-radius: 8px;
  background: #fff;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.source-card:hover {
  border-color: var(--d2a-primary);
  box-shadow: 0 2px 8px rgb(22 119 255 / 12%);
}

.source-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.source-card__name {
  font-size: var(--d2a-font-lg);
  font-weight: 600;
  color: var(--d2a-text-primary);
}

.source-card__tags {
  display: flex;
  gap: 6px;
}

.source-card__meta {
  display: flex;
  gap: 14px;
  font-size: var(--d2a-font-sm);
  color: var(--d2a-text-secondary);
}

.source-card__reason {
  margin: 0;
  font-size: var(--d2a-font-xs);
  color: var(--d2a-status-stale);
}

.drawer-section-title {
  margin: 0 0 8px;
  font-size: var(--d2a-font-md);
  font-weight: 600;
}

.drawer-section-title + .el-table {
  margin-bottom: 20px;
}

.form-hint {
  font-size: var(--d2a-font-xs);
  color: var(--d2a-text-secondary);
}

.form-error {
  margin-bottom: 12px;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
}

.drawer-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.reset-result {
  word-break: break-all;
}

.conn-snippet {
  margin: 0 0 8px;
  padding: 10px 12px;
  border: 1px solid var(--d2a-border);
  border-radius: 6px;
  background: #0d1117;
  color: #c9d1d9;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

.type-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
}

.type-item {
  padding: 10px 12px;
  border: 1px solid var(--d2a-border);
  border-radius: 6px;
}

.type-item--disabled {
  opacity: 0.55;
}

.type-item__head {
  display: flex;
  gap: 8px;
  align-items: center;
}

.type-item__name {
  font-weight: 600;
}

.type-item__examples {
  flex: 1;
  font-size: var(--d2a-font-sm);
  color: var(--d2a-text-secondary);
}

.type-item__hint {
  margin: 6px 0 0;
  font-size: var(--d2a-font-sm);
  color: var(--d2a-text-secondary);
}

@media (max-width: 900px) {
  .page-header {
    flex-direction: column;
  }

  .source-grid {
    grid-template-columns: 1fr;
  }
}
</style>
