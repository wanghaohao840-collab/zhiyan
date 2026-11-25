import { Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { ProtectedRoute } from "./auth/ProtectedRoute";
import { ErrorBoundary, Skeleton } from "./components/ui";
import { AppShell } from "./layout/AppShell";
import { navigationItems } from "./layout/navigation";
import { LoginPage } from "./pages/LoginPage";
import { RegisterPage } from "./pages/RegisterPage";
import {
  LazyDocumentsPage,
  LazyInsightsPage,
  LazyLearningPage,
  LazyNotesPage,
  LazyOverviewPage,
  LazyQaPage,
  LazySearchPage,
} from "./pages/routeLoaders";

function RouteFallback() {
  return (
    <section className="route-fallback">
      <Skeleton label="正在加载页面…" lines={4} />
    </section>
  );
}

export function App() {
  return (
    <ErrorBoundary>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route element={<ProtectedRoute />}>
          <Route element={<AppShell />}>
            {navigationItems.map((item) => (
              <Route
                key={item.path}
                path={item.path}
                element={
                  <Suspense fallback={<RouteFallback />}>
                    {item.path === "/overview" ? (
                      <LazyOverviewPage />
                    ) : item.path === "/documents" ? (
                      <LazyDocumentsPage />
                    ) : item.path === "/search" ? (
                      <LazySearchPage />
                    ) : item.path === "/learning" ? (
                      <LazyLearningPage />
                    ) : item.path === "/qa" ? (
                      <LazyQaPage />
                    ) : item.path === "/notes" ? (
                      <LazyNotesPage />
                    ) : (
                      <LazyInsightsPage />
                    )}
                  </Suspense>
                }
              />
            ))}
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Routes>
    </ErrorBoundary>
  );
}
