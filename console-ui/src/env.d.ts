/// <reference types="vite/client" />

interface ImportMetaEnv {
  // 无产品级模式开关:开发与生产均连真实 API。
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
