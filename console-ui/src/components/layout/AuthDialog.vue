<script setup lang="ts">
// 认证提示:401 或手动登录时弹出;Token 只写入 sessionStorage(当前标签页)。
import { ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const { authRequired } = storeToRefs(session)
const token = ref('')

watch(authRequired, (required) => {
  if (!required) {
    token.value = ''
  }
})

function submit(): void {
  const value = token.value.trim()
  if (value) {
    session.login(value)
  }
}
</script>

<template>
  <el-dialog
    :model-value="authRequired"
    title="需要管理界面登录密码"
    width="420px"
    :close-on-click-modal="false"
    data-testid="auth-dialog"
    @update:model-value="(v: boolean) => { if (!v) session.dismissAuth() }"
  >
    <p class="auth-dialog__hint">
      登录密码仅保存在当前标签页(sessionStorage),关闭标签页即清除。
    </p>
    <el-input
      v-model="token"
      type="password"
      placeholder="管理界面登录密码"
      show-password
      data-testid="auth-token-input"
      @keyup.enter="submit"
    />
    <template #footer>
      <el-button
        type="primary"
        :disabled="!token.trim()"
        data-testid="auth-submit"
        @click="submit"
      >
        登录
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.auth-dialog__hint {
  margin-top: 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
