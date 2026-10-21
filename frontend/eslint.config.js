import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

/**
 * ESLint 配置（flat config）。
 *
 * ## 只开「能抓真 bug」的规则
 *
 * 这个项目前端不做单元测试（架构 §十二 的主动取舍），
 * 那么 lint 就是唯一能自动拦住问题的关口。所以：
 *
 * - `no-unused-vars` 打开：未使用的变量往往是重构遗留
 * - `no-explicit-any` 打开：`any` 会绕过类型检查，等于放弃生成类型的价值
 * - 样式类规则（缩进、引号）**全关**：把注意力留给真问题
 */
export default tseslint.config(
  {
    // 生成文件不参与 lint
    ignores: ['dist/**', 'node_modules/**', 'src/api/generated.d.ts', 'src/components/ui/**'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      // 浏览器全局（AbortController / performance / sessionStorage 等）
      globals: { ...globals.browser },
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  {
    // src 下的 .ts 也是浏览器环境
    files: ['src/**/*.ts'],
    languageOptions: {
      globals: { ...globals.browser },
    },
  },
  {
    rules: {
      // 真问题
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],

      // 样式问题交给格式化工具，不在 lint 里吵
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/multiline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/attributes-order': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-newline': 'off',
    },
  },
  {
    // 脚本用 node 全局
    files: ['scripts/**/*.mjs'],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
)
