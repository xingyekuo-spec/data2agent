<script setup lang="ts">
// 仅 Mock 模式可见的开发面板:切换 fixture 场景(不切换环境模式)。
import { ref } from 'vue'
import { SCENARIOS, getScenario, setScenario, type ScenarioId } from '@/mocks/scenario'

const current = ref<ScenarioId>(getScenario())

function onChange(event: Event): void {
  const id = (event.target as HTMLSelectElement).value as ScenarioId
  setScenario(id)
  current.value = id
}
</script>

<template>
  <div
    class="scenario-switcher"
    data-testid="scenario-switcher"
  >
    <span class="scenario-switcher__tag">MOCK 场景</span>
    <select
      :value="current"
      aria-label="Mock 场景切换"
      @change="onChange"
    >
      <option
        v-for="s in SCENARIOS"
        :key="s.id"
        :value="s.id"
        :title="s.description"
      >
        {{ s.label }}
      </option>
    </select>
  </div>
</template>

<style scoped>
.scenario-switcher {
  position: fixed;
  right: 12px;
  bottom: 12px;
  z-index: 3000;
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  background: var(--d2a-env-mock);
  color: #fff;
  border-radius: 6px;
  font-size: 12px;
  box-shadow: 0 2px 8px rgb(0 0 0 / 25%);
}

.scenario-switcher__tag {
  font-weight: 700;
}

.scenario-switcher select {
  border: none;
  border-radius: 4px;
  padding: 2px 4px;
}
</style>
