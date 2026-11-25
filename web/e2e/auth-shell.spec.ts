import { expect, openMore, registerUser, test, uniqueUsername } from "./fixtures";

const destinations = [
  { path: "/overview", desktop: "概览", mobile: "概览", heading: /^(早上好|下午好|晚上好)，/ },
  { path: "/documents", desktop: "文档库", mobile: "文档", heading: "文档库" },
  { path: "/qa", desktop: "智能问答", mobile: "问答", heading: "智能问答" },
  { path: "/search", desktop: "文献检索", mobile: "检索", heading: "文献检索" },
  { path: "/notes", desktop: "学习笔记", mobile: "学习笔记", heading: "学习笔记" },
  { path: "/insights", desktop: "学习洞察", mobile: "学习洞察", heading: "学习洞察" },
  { path: "/learning", desktop: "学习中心", mobile: "学习中心", heading: "学习中心" },
] as const;

test("registers with a cookie, navigates the shell, and logs out with CSRF", async ({
  appUrl,
  context,
  page,
}, testInfo) => {
  await registerUser(page, appUrl, uniqueUsername(`flow_${testInfo.project.name}`));

  const sessionCookie = (await context.cookies(appUrl)).find(
    (cookie) => cookie.name === "zhiyan_session",
  );
  expect(sessionCookie).toBeDefined();
  expect(sessionCookie?.httpOnly).toBe(true);

  for (const destination of destinations) {
    if (testInfo.project.name === "mobile" && ["/notes", "/insights", "/learning"].includes(destination.path)) {
      await openMore(page, testInfo.project.name);
      await page.getByRole("dialog", { name: "更多" }).getByRole("link", {
        name: destination.mobile,
      }).click();
    } else {
      const navigationName = testInfo.project.name === "mobile" ? "移动导航" : "主导航";
      const label = testInfo.project.name === "mobile" ? destination.mobile : destination.desktop;
      await page.getByRole("navigation", { name: navigationName }).getByRole("link", {
        name: label,
        exact: true,
      }).click();
    }
    await expect(page).toHaveURL(`${appUrl}${destination.path}`);
    await expect(page.getByRole("heading", { level: 1, name: destination.heading })).toBeVisible();
  }

  if (testInfo.project.name === "mobile") {
    await openMore(page, testInfo.project.name);
  }
  const logoutRequestPromise = page.waitForRequest(
    (request) => request.url() === `${appUrl}/api/v1/auth/logout`,
  );
  const logoutScope =
    testInfo.project.name === "mobile"
      ? page.getByRole("dialog", { name: "更多" })
      : page.getByRole("complementary", { name: "应用侧栏" });
  await logoutScope.getByRole("button", { name: "退出登录" }).click();
  const logoutRequest = await logoutRequestPromise;
  expect(logoutRequest.method()).toBe("POST");
  expect(logoutRequest.headers()["x-csrf-token"]).toBeTruthy();
  await expect(page).toHaveURL(`${appUrl}/login`);
  expect((await context.cookies(appUrl)).some((cookie) => cookie.name === "zhiyan_session")).toBe(false);

  await page.goto(`${appUrl}/documents`);
  await expect(page).toHaveURL(`${appUrl}/login`);
  await expect(page.getByRole("heading", { level: 1, name: "登录" })).toBeVisible();
});

test("redirects the exact legacy path and keeps all product routes live", async ({
  appUrl,
  page,
}, testInfo) => {
  const redirectResponse = page.waitForResponse(
    (response) => response.url() === `${appUrl}/legacy` && response.status() === 307,
  );
  const legacyResponse = await page.goto(`${appUrl}/legacy`);

  expect((await redirectResponse).headers().location).toBe("/legacy/");
  expect(legacyResponse?.status()).toBe(200);
  await expect(page).toHaveURL(`${appUrl}/legacy/`);
  await expect(page.locator("gradio-app")).toBeVisible();

  await registerUser(page, appUrl, uniqueUsername(`legacy_${testInfo.project.name}`));
  await page.goto(`${appUrl}/search`);
  await expect(page.getByRole("heading", { level: 1, name: "文献检索" })).toBeVisible();
  await page.goto(`${appUrl}/learning`);
  await expect(page.getByRole("heading", { level: 1, name: "学习中心" })).toBeVisible();
});
