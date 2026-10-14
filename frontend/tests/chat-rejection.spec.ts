import { test, expect, type Page, type Route } from "@playwright/test";

import {
  blockUnmockedApi,
  expectNoUnmockedApi,
  installAuthMocks,
  type MockUser,
} from "./support/mockAuth";

const CHAT_API = "**/api/chat";

const TEST_TOKEN = "playwright-test-token";

// 保持原有 id，App 按 userId 保存的本地消息记录 key 不变。
const TEST_USER: MockUser = {
  id: "playwright-quota-user",
  username: "playwright",
  role: "member",
  active: true,
};

/**
 * 顺序：先兜底拦截，再认证 mock。
 * 用例随后注册的 CHAT_API mock 优先级最高。
 */
async function setupPage(page: Page): Promise<string[]> {
  const unexpected = await blockUnmockedApi(page);
  await installAuthMocks(page, TEST_TOKEN, TEST_USER);
  return unexpected;
}

async function signIn(page: Page): Promise<void> {
  await expect(page.locator("#pa-login-username")).toBeVisible();

  await page.locator("#pa-login-username").fill("playwright");
  await page.locator("#pa-login-password").fill("PlaywrightTestPassword2026!");

  await page.getByRole("button", { name: /登录工作空间/ }).click();

  await expect(
    page.getByRole("textbox", { name: "输入聊天消息" }),
  ).toBeVisible();
}

async function openAndSignIn(page: Page): Promise<void> {
  await page.goto("/login");
  await signIn(page);
}

async function send(page: Page, message: string): Promise<void> {
  await page.getByRole("textbox", { name: "输入聊天消息" }).fill(message);
  await page.getByRole("button", { name: "发送 ↑" }).click();
}

test.describe("Chat Quota Rejections", () => {

  test("每日额度拒绝后保留草稿，刷新不重新请求", async ({ page }) => {
    const unexpected = await setupPage(page);

    let requests = 0;

    await page.route(CHAT_API, async (route: Route) => {
      requests += 1;

      await route.fulfill({
        status: 429,
        contentType: "application/json",
        body: JSON.stringify({
          status: "rejected",
          code: "daily_quota_exceeded",
          detail: "过去24小时额度已用完",
        }),
      });
    });

    await openAndSignIn(page);

    await send(page, "等额度恢复再发");

    await expect(page.getByRole("alert")).toContainText("过去 24 小时");

    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toHaveValue("等额度恢复再发");

    await expect(
      page.locator(".message-status.warning"),
    ).toHaveText("未发送成功 · AI 未执行");

    await expect(
      page.getByRole("button", { name: "查询结果" }),
    ).toHaveCount(0);

    expect(requests).toBe(1);

    // sessionStorage 在刷新后保留，应通过 /api/auth/me 恢复会话，
    // 不应要求重新登录，也不应自动重发。
    await page.reload();

    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toBeVisible();

    await expect(page.locator("#pa-login-username")).toHaveCount(0);

    await expect(
      page.locator(".message-status.warning"),
    ).toHaveText("未发送成功 · AI 未执行");

    await expect(
      page.getByRole("button", { name: "查询结果" }),
    ).toHaveCount(0);

    expect(requests).toBe(1);

    expectNoUnmockedApi(unexpected);
  });

  test("并发限制不会被误认为模型结果未知", async ({ page }) => {
    const unexpected = await setupPage(page);

    await page.route(CHAT_API, async (route: Route) => {
      await route.fulfill({
        status: 429,
        contentType: "application/json",
        body: JSON.stringify({
          status: "rejected",
          code: "concurrency_limit_exceeded",
        }),
      });
    });

    await openAndSignIn(page);

    await send(page, "稍后再试");

    await expect(page.getByRole("alert")).toContainText("当前正在处理的请求过多");

    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toHaveValue("稍后再试");

    await expect(
      page.locator(".message-status.warning"),
    ).toHaveText("未发送成功 · AI 未执行");

    await expect(
      page.getByRole("button", { name: "查询结果" }),
    ).toHaveCount(0);

    expectNoUnmockedApi(unexpected);
  });

  test("模型结果未知仍保留原请求查询入口", async ({ page }) => {
    const unexpected = await setupPage(page);

    await page.route(CHAT_API, async (route: Route) => {
      await route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({
          status: "unknown",
          detail: "模型调用结果无法确认",
        }),
      });
    });

    await openAndSignIn(page);

    await send(page, "不允许自动重发");

    await expect(page.getByRole("alert")).toContainText("请求结果尚未确认");

    await expect(
      page.locator(".message-status.warning"),
    ).toHaveText("请求结果尚未确认");

    await expect(
      page.getByRole("button", { name: "查询结果" }),
    ).toBeVisible();

    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toHaveValue("");

    expectNoUnmockedApi(unexpected);
  });

});