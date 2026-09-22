import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// 构建产物直接落进 Controller 的静态资源目录：发行程序从包资源定位页面，
// 不依赖 CDN、远程字体或运行期 Node（LIGHT_EDITION_DESIGN §3.1、§15.1）。
export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: "../src/raricy_launcher/static",
    emptyOutDir: true,
    // 本地页面不需要 source map，也不做代码分割：一个页面、一份入口。
    sourcemap: false,
    assetsInlineLimit: 0,
  },
});
