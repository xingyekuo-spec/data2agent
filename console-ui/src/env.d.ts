/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 环境模式:mock | demo | real。见 src/config/mode.ts。 */
  readonly VITE_CONSOLE_MODE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
