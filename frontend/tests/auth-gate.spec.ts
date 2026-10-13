import { test, expect } from "@playwright/test";

import {
  TEST_TOKEN,
  blockUnmockedApi,
  expectNoUnmockedApi,
  installAuthMocks,
} from "./support/mockAuth";

// 与 AuthPreview.tsx 中的 SESSION_KEY 保持一致
const SESSION_KEY = "personal-agent-auth-token";

test.describe("Personal Agent Login Gate", () => {

  test("直接访问根路径必须进入登录页面", async ({ page }) => {
    const unexpected = await blockUnmockedApi(page);

    await page.goto("/");

    await expect(page).toHaveURL(/\/login(?:[?#]|$)/);

    await expect(
      page.getByRole("heading", { name: "欢迎回来" }),
    ).toBeVisible();

    // 没有登录时，不能看到聊天输入框。
    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toHaveCount(0);

    await expect(
      page.getByRole("button", { name: /登录工作空间/ }),
    ).toBeVisible();

    // 没有 token 时不应发出任何 API 请求。
    expectNoUnmockedApi(unexpected);
  });

  test("直接访问登录地址正常显示登录界面", async ({ page }) => {
    const unexpected = await blockUnmockedApi(page);

    await page.goto("/login");

    await expect(page.locator("#pa-login-username")).toBeVisible();
    await expect(page.locator("#pa-login-password")).toBeVisible();

    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toHaveCount(0);

    expectNoUnmockedApi(unexpected);
  });

  test("已失效的 token 被清除并回到登录页", async ({ page }) => {
    const unexpected = await blockUnmockedApi(page);
    await installAuthMocks(page);

    // 写入一个与 mock 不匹配的 token，/me 会返回 401。
    await page.addInitScript(
      ([key, value]) => window.sessionStorage.setItem(key, value),
      [SESSION_KEY, "stale-token"] as const,
    );

    await page.goto("/");

    await expect(page).toHaveURL(/\/login(?:[?#]|$)/);
    await expect(page.getByRole("alert")).toContainText("登录已过期");

    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toHaveCount(0);

    const saved = await page.evaluate(
      (key) => window.sessionStorage.getItem(key),
      SESSION_KEY,
    );
    expect(saved).toBeNull();

    expectNoUnmockedApi(unexpected);
  });

  test("网络故障时保留 token 并提供重新验证", async ({ page }) => {
    const unexpected = await blockUnmockedApi(page);

    // 显式模拟 /me 连接失败（不算作漏 mock）。
    await page.route("**/api/auth/me", (route) =>
      route.abort("connectionrefused"),
    );

    await page.addInitScript(
      ([key, value]) => window.sessionStorage.setItem(key, value),
      [SESSION_KEY, TEST_TOKEN] as const,
    );

    await page.goto("/");

    await expect(page).toHaveURL(/\/login(?:[?#]|$)/);

    await expect(
      page.getByRole("button", { name: "重新验证上次登录" }),
    ).toBeVisible();

    // 网络故障不等于 token 失效，不能进入工作空间，也不能清除 token。
    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toHaveCount(0);

    const saved = await page.evaluate(
      (key) => window.sessionStorage.getItem(key),
      SESSION_KEY,
    );
    expect(saved).toBe(TEST_TOKEN);

    expectNoUnmockedApi(unexpected);
  });

});