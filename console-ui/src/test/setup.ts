import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll } from 'vitest'
import { buildHandlers, strictUnhandledRequest } from '@/mocks/handlers'
import { setScenario } from '@/mocks/scenario'

const server = setupServer(...buildHandlers())

export { server }

beforeAll(() => {
  server.listen({ onUnhandledRequest: strictUnhandledRequest })
})

afterEach(() => {
  server.resetHandlers()
  setScenario('healthy')
  sessionStorage.clear()
  localStorage.clear()
})

afterAll(() => {
  server.close()
})
