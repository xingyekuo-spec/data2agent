import { createPinia } from 'pinia'
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import App from './App.vue'
import { IS_MOCK } from './config/mode'
import { router } from './router'
import './styles/tokens.css'

async function bootstrap(): Promise<void> {
  // Mock 必须在首次渲染前就绪:首批请求不能落到真实网络;
  // REAL 永远不执行本分支,MSW 不会进入其运行时。
  if (IS_MOCK) {
    const { startMockWorker } = await import('@/mocks/browser')
    await startMockWorker()
  }
  createApp(App).use(createPinia()).use(router).use(ElementPlus, { locale: zhCn }).mount('#app')
}

void bootstrap()
