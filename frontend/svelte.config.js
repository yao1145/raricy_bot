import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// 供 svelte-check 使用的最小配置；构建由 vite.config.ts 驱动。
export default {
  preprocess: vitePreprocess(),
};
