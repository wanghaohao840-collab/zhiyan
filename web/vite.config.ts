import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("react-dom") || id.includes("react-router") || id.includes("/react/")) {
            return "react-vendor";
          }
          if (id.includes("@tanstack/react-query")) {
            return "query-vendor";
          }
          if (id.includes("lucide-react")) {
            return "icons-vendor";
          }
          if (id.includes("katex")) {
            return "katex-vendor";
          }
          if (
            id.includes("highlight.js") ||
            id.includes("lowlight") ||
            id.includes("rehype-highlight")
          ) {
            return "highlight-vendor";
          }
          if (
            id.includes("react-markdown") ||
            id.includes("remark-") ||
            id.includes("rehype-") ||
            id.includes("unified") ||
            id.includes("hast-") ||
            id.includes("mdast-") ||
            id.includes("micromark")
          ) {
            return "markdown-vendor";
          }
          return undefined;
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:7860",
      "/legacy": {
        target: "http://127.0.0.1:7860",
        ws: true,
      },
    },
  },
  test: {
    css: true,
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    restoreMocks: true,
  },
});
