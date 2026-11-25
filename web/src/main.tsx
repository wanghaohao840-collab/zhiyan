import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import interLatinWoff2Url from "@fontsource-variable/inter/files/inter-latin-wght-normal.woff2?url";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { AuthProvider } from "./auth/AuthProvider";
import { ThemeProvider } from "./theme/ThemeProvider";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/global.css";
import "./styles/documents.css";
import "./styles/qa.css";
import "./styles/insights.css";
import "./styles/search.css";
import "./styles/learning.css";

// NOTE: 仅预加载单一拉丁子集变量 WOFF2（约 47KB），消除字体发现延迟且零外部 CDN 依赖
if (typeof document !== "undefined" && !document.querySelector(`link[href="${interLatinWoff2Url}"]`)) {
  const preload = document.createElement("link");
  preload.rel = "preload";
  preload.as = "font";
  preload.type = "font/woff2";
  preload.href = interLatinWoff2Url;
  preload.crossOrigin = "anonymous";
  document.head.appendChild(preload);
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false },
    mutations: { retry: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AuthProvider>
            <App />
          </AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
);
