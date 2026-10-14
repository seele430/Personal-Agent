import {
  test,
  expect,
  type Page,
} from "@playwright/test";

import {
  blockUnmockedApi,
  expectNoUnmockedApi,
  installAuthMocks,
  type MockUser,
} from "./support/mockAuth";

// 测试专用身份，不是真实账户
const TEST_TOKEN = "playwright-test-only-token";

const TEST_USER: MockUser = {
  id: "playwright-e2e-user",
  username: "playwright",
  role: "member",
  active: true,
};

const USAGE_API = "**/api/usage/summary";

const SAMPLE_REPORT = {
  window_hours: 24,
  role: "member",
  requests: {
    used: 3,
    limit: 10,
    remaining: 7,
  },
  budget: {
    used_usd: "0.03",
    limit_usd: "0.075",
    remaining_usd: "0.045",
    unresolved_reservations: 0,
  },
  note: "本地账本：只统计本机记录，不等于服务商账单。",
};

async function setupPage(page: Page): Promise<string[]> {
  const unexpected = await blockUnmockedApi(page);
  await installAuthMocks(page, TEST_TOKEN, TEST_USER);
  return unexpected;
}

async function login(page: Page): Promise<void> {
  await page.goto("/login");

  await page
    .locator("#pa-login-username")
    .fill("playwright");

  await page
    .locator("#pa-login-password")
    .fill("PlaywrightTestPassword2026!");

  await page
    .getByRole("button", { name: /登录工作空间/ })
    .click();

  await expect(
    page.getByRole("textbox", { name: "输入聊天消息" }),
  ).toBeVisible();
}

test.describe("Personal Agent Quota", () => {

  // 1. 额度面板展示请求次数与预算

  test("额度面板显示用量与上限", async ({ page }) => {
    const unexpected = await setupPage(page);

    await page.route(USAGE_API, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(SAMPLE_REPORT),
      }),
    );

    await login(page);

    await page
      .getByRole("button", { name: "额度" })
      .click();

    await expect(
      page.getByLabel("我的额度"),
    ).toBeVisible();

    await expect(
      page.getByTestId("quota-cards"),
    ).toBeVisible();

    await expect(page.getByText("请求次数")).toBeVisible();
    await expect(page.getByText("3 / 10")).toBeVisible();
    await expect(page.getByText("剩余 7 次")).toBeVisible();

    await expect(page.getByText("AI 预算")).toBeVisible();

    await expect(
      page.getByText("US$0.0300 / US$0.0750"),
    ).toBeVisible();

    await expect(
      page.getByText("剩余 US$0.0450"),
    ).toBeVisible();

    // 额度面板必须说明这是本地账本
    await expect(
      page.getByTestId("quota-note"),
    ).toContainText("本地账本");

    expectNoUnmockedApi(unexpected);
  });

  // 2. 额度接口失败时明确报错

  test("额度接口失败时显示错误", async ({ page }) => {
    const unexpected = await setupPage(page);

    await page.route(USAGE_API, (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "服务器错误" }),
      }),
    );

    await login(page);

    await page
      .getByRole("button", { name: "额度" })
      .click();

    await expect(
      page.getByTestId("quota-error"),
    ).toContainText("读取额度失败");

    expectNoUnmockedApi(unexpected);
  });

  // 3. 没打开额度面板就不应该请求额度接口

  test("未打开面板时不请求额度接口", async ({ page }) => {
    const unexpected = await setupPage(page);

    const calls: string[] = [];

    await page.route(USAGE_API, (route) => {
      calls.push(route.request().url());

      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(SAMPLE_REPORT),
      });
    });

    await login(page);

    // 进入工作空间后只看聊天，不该顺带拉额度
    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toBeVisible();

    expect(calls).toEqual([]);

    await page
      .getByRole("button", { name: "额度" })
      .click();

    await expect.poll(() => calls.length).toBeGreaterThan(0);

    expectNoUnmockedApi(unexpected);
  });
});
