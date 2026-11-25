import { lazy, type ComponentType } from "react";

type PageLoader = () => Promise<{ default: ComponentType }>;

const routeLoaderMap: Record<string, PageLoader> = {
  "/overview": () => import("./OverviewPage").then((mod) => ({ default: mod.OverviewPage })),
  "/documents": () => import("./DocumentsPage").then((mod) => ({ default: mod.DocumentsPage })),
  "/qa": () => import("./QaPage").then((mod) => ({ default: mod.QaPage })),
  "/search": () => import("./SearchPage").then((mod) => ({ default: mod.SearchPage })),
  "/notes": () => import("./NotesPage").then((mod) => ({ default: mod.NotesPage })),
  "/insights": () => import("./InsightsPage").then((mod) => ({ default: mod.InsightsPage })),
  "/learning": () => import("./LearningPage").then((mod) => ({ default: mod.LearningPage })),
};

const prefetchedPaths = new Set<string>();

/**
 * 鼠标悬停或键盘聚焦导航项时提前预取对应页面 chunk，兼顾首屏轻量与零延迟切页。
 */
export function prefetchRoute(path: string): void {
  const loader = routeLoaderMap[path];
  if (!loader || prefetchedPaths.has(path)) return;
  prefetchedPaths.add(path);
  void loader().catch(() => {
    prefetchedPaths.delete(path);
  });
}

export const LazyOverviewPage = lazy(routeLoaderMap["/overview"]);
export const LazyDocumentsPage = lazy(routeLoaderMap["/documents"]);
export const LazyQaPage = lazy(routeLoaderMap["/qa"]);
export const LazySearchPage = lazy(routeLoaderMap["/search"]);
export const LazyNotesPage = lazy(routeLoaderMap["/notes"]);
export const LazyInsightsPage = lazy(routeLoaderMap["/insights"]);
export const LazyLearningPage = lazy(routeLoaderMap["/learning"]);
