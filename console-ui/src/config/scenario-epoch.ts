/**
 * Mock 场景切换的视图重挂载信号。
 *
 * 放在 mocks/ 之外的原因:AppLayout 需要它做 router-view 的 key,而它不能
 * 静态依赖 mocks/(否则 fixtures 会进入生产主包)。生产构建中 mocks 被别名
 * 排除时,它恒为 0,无副作用。
 */
import { ref } from 'vue'

export const scenarioEpoch = ref(0)
