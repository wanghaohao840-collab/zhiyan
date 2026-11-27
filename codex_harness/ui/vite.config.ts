import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { viteSingleFile } from 'vite-plugin-singlefile';

/**
 * Antigravity MCP App 要求单文件自包含 HTML（默认 CSP 禁止外链脚本与样式），
 * 因此使用 vite-plugin-singlefile 将 React、ext-apps SDK 与样式全部内联打包。
 */
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  build: {
    target: 'es2022',
    assetsInlineLimit: 100_000_000,
    chunkSizeWarningLimit: 2000,
    cssCodeSplit: false,
    outDir: 'dist',
    emptyOutDir: true,
  },
});
