import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createMemoryRouter,
  MemoryRouter,
  Outlet,
  RouterProvider,
} from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { AuthProvider } from "../auth/AuthProvider";
import { ProtectedRoute } from "../auth/ProtectedRoute";
import { Button } from "../components/Button/Button";
import { TextField } from "../components/TextField/TextField";
import { MigrationPage } from "../pages/MigrationPage";
import { AppShell } from "./AppShell";
import { navigationItems } from "./navigation";

const fetchMock = vi.fn<typeof fetch>();

function getStyleRule(selector: string): CSSStyleDeclaration {
  for (const sheet of document.styleSheets) {
    for (const rule of sheet.cssRules) {
      if (rule instanceof CSSStyleRule && rule.selectorText === selector) {
        return rule.style;
      }
    }
  }
  throw new Error(`Missing CSS rule: ${selector}`);
}

function getMediaStyleRule(mediaText: string, selector: string): CSSStyleDeclaration {
  for (const sheet of document.styleSheets) {
    for (const rule of sheet.cssRules) {
      if (rule instanceof CSSMediaRule && rule.conditionText === mediaText) {
        for (const nestedRule of rule.cssRules) {
          if (
            nestedRule instanceof CSSStyleRule
            && nestedRule.selectorText === selector
          ) {
            return nestedRule.style;
          }
        }
      }
    }
  }
  throw new Error(`Missing CSS rule: ${mediaText} / ${selector}`);
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function mockAuthenticatedSession() {
  fetchMock.mockImplementation((input) => {
    if (input === "/api/v1/auth/session") {
      return Promise.resolve(
        jsonResponse({ username: "reader", csrf_token: "shell-csrf" }),
      );
    }
    if (input === "/api/v1/auth/logout") {
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (input === "/api/v1/qa/capabilities") {
      return Promise.resolve(jsonResponse({ enabled: true }));
    }
    if (input === "/api/v1/qa/conversations?limit=100") {
      return Promise.resolve(jsonResponse({ items: [], next_cursor: null }));
    }
    if (input === "/api/v1/documents") {
      return Promise.resolve(jsonResponse({ items: [] }));
    }
    return Promise.reject(new Error(`Unexpected request: ${String(input)}`));
  });
  vi.stubGlobal("fetch", fetchMock);
}

function mockAuthenticatedSessionWithLogoutFailure() {
  fetchMock.mockImplementation((input) => {
    if (input === "/api/v1/auth/session") {
      return Promise.resolve(
        jsonResponse({ username: "reader", csrf_token: "shell-csrf" }),
      );
    }
    if (input === "/api/v1/auth/logout") {
      return Promise.resolve(
        jsonResponse(
          {
            error: {
              code: "logout_unavailable",
              message: "退出服务暂不可用",
              retryable: true,
              field_errors: {},
            },
          },
          503,
        ),
      );
    }
    return Promise.reject(new Error(`Unexpected request: ${String(input)}`));
  });
  vi.stubGlobal("fetch", fetchMock);
}

function renderShell(initialEntry = "/overview") {
  const queryClient = createTestQueryClient();
  const router = createMemoryRouter(
    [
      {
        element: (
          <QueryClientProvider client={queryClient}>
            <AuthProvider>
              <Outlet />
            </AuthProvider>
          </QueryClientProvider>
        ),
        children: [
          { path: "/login", element: <h1>登录页</h1> },
          {
            element: <ProtectedRoute />,
            children: [
              {
                element: <AppShell />,
                children: navigationItems.map((item) => ({
                  path: item.path,
                  element: <MigrationPage heading={item.heading} />,
                })),
              },
            ],
          },
        ],
      },
    ],
    { initialEntries: [initialEntry] },
  );

  render(<RouterProvider router={router} />);
  return router;
}

function renderProductionApp(initialEntry: string) {
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  afterEach(() => {
    document.documentElement.style.fontSize = "";
    window.localStorage.removeItem("zhiyan.ui.sidebarCollapsed");
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("uses one exact seven-item source for desktop and mobile navigation", async () => {
    mockAuthenticatedSession();
    renderShell();

    expect(navigationItems.map(({ path, label }) => [path, label])).toEqual([
      ["/overview", "概览"],
      ["/documents", "文档库"],
      ["/qa", "智能问答"],
      ["/search", "文献检索"],
      ["/notes", "学习笔记"],
      ["/insights", "学习洞察"],
      ["/learning", "学习中心"],
    ]);

    const desktopNav = await screen.findByLabelText("主导航");
    expect(desktopNav).toHaveRole("navigation");
    expect(within(desktopNav).getAllByRole("link", { hidden: true })).toHaveLength(7);
    const mobileNav = screen.getByRole("navigation", { name: "移动导航" });
    expect(within(mobileNav).getAllByRole("link")).toHaveLength(4);
    expect(within(mobileNav).getByRole("button", { name: "更多" })).toBeVisible();
  });

  it("marks the current destination in the sidebar and More control", async () => {
    mockAuthenticatedSession();
    renderShell("/notes");

    const desktopNav = await screen.findByLabelText("主导航");
    expect(
      within(desktopNav).getByRole("link", { name: "学习笔记", hidden: true }),
    ).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "更多" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("moves focus from the skip link to main content", async () => {
    mockAuthenticatedSession();
    renderShell();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("link", { name: "跳到主要内容" }));

    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("opens More with its derived destinations and account actions", async () => {
    mockAuthenticatedSession();
    renderShell();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "更多" }));

    const drawer = screen.getByRole("dialog", { name: "更多" });
    expect(within(drawer).getByRole("link", { name: "学习笔记" })).toHaveAttribute(
      "href",
      "/notes",
    );
    expect(within(drawer).getByRole("link", { name: "学习洞察" })).toHaveAttribute(
      "href",
      "/insights",
    );
    expect(within(drawer).getByRole("button", { name: "账户" })).toBeVisible();
    expect(within(drawer).getByRole("link", { name: "学习中心" })).toHaveAttribute("href", "/learning");
    expect(within(drawer).getByRole("button", { name: "退出登录" })).toBeVisible();
  });

  it("traps drawer focus, closes on Escape, and returns focus to More", async () => {
    mockAuthenticatedSession();
    renderShell();
    const user = userEvent.setup();
    const trigger = await screen.findByRole("button", { name: "更多" });

    await user.click(trigger);
    const drawer = screen.getByRole("dialog", { name: "更多" });
    expect(within(drawer).getByRole("link", { name: "学习笔记" })).toHaveFocus();
    within(drawer).getByRole("button", { name: "关闭更多" }).focus();
    await user.tab({ shift: true });
    expect(within(drawer).getByRole("button", { name: "退出登录" })).toHaveFocus();
    await user.tab();
    expect(within(drawer).getByRole("button", { name: "关闭更多" })).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "更多" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("keeps a short-height drawer scrollable while keyboard users reach logout", async () => {
    mockAuthenticatedSession();
    vi.stubGlobal("innerHeight", 320);
    document.documentElement.style.fontSize = "32px";
    renderShell();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "更多" }));

    const drawer = screen.getByRole("dialog", { name: "更多" });
    expect(document.body).toHaveStyle({ overflow: "hidden" });
    const drawerStyles = getStyleRule(".more-drawer");
    expect(drawerStyles.overflowY).toBe("auto");
    expect(drawerStyles.overscrollBehavior).toBe("contain");

    await user.tab();
    await user.tab();
    expect(within(drawer).getByRole("link", { name: "学习中心" })).toHaveFocus();
    await user.tab();
    await user.tab();

    expect(within(drawer).getByRole("button", { name: "退出登录" })).toHaveFocus();
  });

  it("logs out through the Task 3 authentication contract", async () => {
    mockAuthenticatedSession();
    const router = renderShell();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "更多" }));
    const drawer = screen.getByRole("dialog", { name: "更多" });
    await user.click(within(drawer).getByRole("button", { name: "退出登录" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    const logoutCall = fetchMock.mock.calls.find(([input]) => input === "/api/v1/auth/logout");
    expect(logoutCall).toBeDefined();
    expect(new Headers(logoutCall?.[1]?.headers).get("X-CSRF-Token")).toBe(
      "shell-csrf",
    );
  });

  it("consumes a failed logout request while AuthProvider clears and redirects", async () => {
    mockAuthenticatedSessionWithLogoutFailure();
    const unhandledRejections: unknown[] = [];
    const recordUnhandled = (event: PromiseRejectionEvent) => {
      event.preventDefault();
      unhandledRejections.push(event.reason);
    };
    window.addEventListener("unhandledrejection", recordUnhandled);
    const router = renderShell();
    const user = userEvent.setup();

    try {
      await user.click(await screen.findByRole("button", { name: "更多" }));
      const drawer = screen.getByRole("dialog", { name: "更多" });
      await user.click(within(drawer).getByRole("button", { name: "退出登录" }));

      await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
      expect(router.state.historyAction).toBe("REPLACE");
      expect(screen.getByRole("heading", { name: "登录页" })).toBeVisible();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(unhandledRejections).toEqual([]);

      const logoutCall = fetchMock.mock.calls.find(
        ([input]) => input === "/api/v1/auth/logout",
      );
      expect(logoutCall).toBeDefined();
      expect(new Headers(logoutCall?.[1]?.headers).get("X-CSRF-Token")).toBe(
        "shell-csrf",
      );
    } finally {
      window.removeEventListener("unhandledrejection", recordUnhandled);
    }
  });

  it("offers only the exact migration copy and legacy action", async () => {
    mockAuthenticatedSession();
    renderShell("/documents");

    expect(await screen.findByRole("heading", { name: "文档库", level: 1 })).toBeVisible();
    expect(screen.getByRole("heading", { name: "能力迁移中", level: 2 })).toBeVisible();
    expect(
      screen.getByText("该能力正在迁移到新版界面，可暂时前往旧版使用。"),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "前往旧版" })).toHaveAttribute(
      "href",
      "/legacy/",
    );
  });

  it("stops the loading spinner animation for reduced motion", () => {
    render(<Button loading>导入中</Button>);

    expect(screen.getByRole("button", { name: "导入中" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
    expect(
      getMediaStyleRule("(prefers-reduced-motion: reduce)", ".button__spinner")
        .animation,
    ).toBe("none");
  });

  it("uses named shell variables for migration-state dimensions", async () => {
    mockAuthenticatedSession();
    renderShell("/documents");

    await screen.findByRole("heading", { name: "文档库", level: 1 });
    const rootStyles = getComputedStyle(document.documentElement);
    expect(rootStyles.getPropertyValue("--shell-migration-card-min-height").trim()).toBe(
      "236px",
    );
    expect(rootStyles.getPropertyValue("--shell-migration-copy-width").trim()).toBe(
      "32rem",
    );
    expect(rootStyles.getPropertyValue("--shell-migration-action-min-width").trim()).toBe(
      "140px",
    );
    expect(getStyleRule(".migration-state").minHeight).toBe(
      "var(--shell-migration-card-min-height)",
    );
    expect(getStyleRule(".migration-state p").maxWidth).toBe(
      "var(--shell-migration-copy-width)",
    );
    expect(getStyleRule(".migration-state__action").minWidth).toBe(
      "var(--shell-migration-action-min-width)",
    );
  });

  it.each([
    ["/overview", "学习概览"],
    ["/documents", "文档库"],
    ["/qa", "智能问答"],
    ["/search", "文献检索"],
    ["/notes", "学习笔记"],
    ["/insights", "学习洞察"],
  ])("wires production route %s to heading %s", async (path, heading) => {
    mockAuthenticatedSession();
    renderProductionApp(path);

    expect(await screen.findByRole("heading", { name: heading, level: 1 })).toBeVisible();
  });

  it("toggles desktop sidebar collapsed state without hiding navigation links", async () => {
    mockAuthenticatedSession();
    renderShell();
    const user = userEvent.setup();

    const collapseButton = await screen.findByRole("button", {
      name: "收起侧栏",
      hidden: true,
    });
    expect(collapseButton).toHaveAttribute("aria-expanded", "true");
    await user.click(collapseButton);
    expect(screen.getByRole("button", { name: "展开侧栏", hidden: true })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(document.querySelector(".app-shell")).toHaveClass("app-shell--collapsed");
    expect(
      within(screen.getByLabelText("主导航")).getAllByRole("link", { hidden: true }),
    ).toHaveLength(7);
    expect(window.localStorage.getItem("zhiyan.ui.sidebarCollapsed")).toBe("true");
  });

  it("restores persisted sidebar preference and falls back cleanly when storage throws", async () => {
    window.localStorage.setItem("zhiyan.ui.sidebarCollapsed", "true");
    mockAuthenticatedSession();
    const { unmount } = render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/overview"]}>
          <AuthProvider>
            <App />
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(
      await screen.findByRole("button", { name: "展开侧栏", hidden: true }),
    ).toHaveAttribute("aria-expanded", "false");
    unmount();

    const getItemSpy = vi
      .spyOn(Storage.prototype, "getItem")
      .mockImplementation(() => {
        throw new Error("storage blocked");
      });
    try {
      renderShell();
      expect(
        await screen.findByRole("button", { name: "收起侧栏", hidden: true }),
      ).toHaveAttribute("aria-expanded", "true");
    } finally {
      getItemSpy.mockRestore();
    }
  });

  it("keeps a single visible page heading", async () => {
    mockAuthenticatedSession();
    renderProductionApp("/overview");

    expect(await screen.findAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});

describe("shared Penpot component contracts", () => {
  it("forwards native Button props and exposes variant/loading state", () => {
    render(
      <Button hierarchy="danger" size="lg" loading name="remove-document">
        删除文档
      </Button>,
    );

    const button = screen.getByRole("button", { name: "删除文档" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("name", "remove-document");
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveClass("button--danger", "button--lg");
  });

  it("preserves caller aria-busy unless loading forces true", () => {
    render(
      <>
        <Button aria-busy="true">调用方忙碌</Button>
        <Button aria-busy="false">调用方空闲</Button>
        <Button>未指定状态</Button>
        <Button aria-busy="false" loading>
          正在加载
        </Button>
      </>,
    );

    expect(screen.getByRole("button", { name: "调用方忙碌" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
    expect(screen.getByRole("button", { name: "调用方空闲" })).toHaveAttribute(
      "aria-busy",
      "false",
    );
    expect(screen.getByRole("button", { name: "未指定状态" })).not.toHaveAttribute(
      "aria-busy",
    );
    expect(screen.getByRole("button", { name: "正在加载" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
  });

  it("connects TextField label, guidance, errors, and native input props", () => {
    render(
      <TextField
        label="文档名称"
        name="document-name"
        required
        helperText="请输入可辨识的名称"
        error="名称已存在"
        aria-describedby="external-guidance"
      />,
    );

    const input = screen.getByLabelText("文档名称");
    expect(input).toBeRequired();
    expect(input).toHaveAttribute("name", "document-name");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input.getAttribute("aria-describedby")).toContain("external-guidance");
    expect(input).toHaveAccessibleDescription("请输入可辨识的名称 名称已存在");
  });
});
