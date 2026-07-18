import js from '@eslint/js'
import ts from 'typescript-eslint'
import vue from 'eslint-plugin-vue'
import globals from 'globals'

export default ts.config(
  {
    ignores: [
      'dist/**',
      'coverage/**',
      'node_modules/**',
      // 生成物:openapi-typescript 与 MSW worker,不纳入 lint
      'src/types/api.ts',
      'public/mockServiceWorker.js',
    ],
  },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...vue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: { parser: ts.parser },
    },
  },
  {
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
  {
    rules: {
      // 核心 API 与 fixture 禁止显式 any 逃逸(生成物已在上面排除)
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
)
