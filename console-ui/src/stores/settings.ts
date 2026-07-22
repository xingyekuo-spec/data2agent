/** Settings 配置切片:/api/config 的非敏感配置读取。 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getConfig } from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type ConfigViewResponse = components['schemas']['ConfigViewResponse']

export const useSettingsStore = defineStore('settings', () => {
  const config = ref<RequestState<ConfigViewResponse>>({ status: 'idle' })

  async function refresh(): Promise<void> {
    config.value = { status: 'loading' }
    const result = await getConfig()
    config.value = result.ok
      ? { status: 'success', data: result.data }
      : { status: 'error', error: result.error }
  }

  return { config, refresh }
})
