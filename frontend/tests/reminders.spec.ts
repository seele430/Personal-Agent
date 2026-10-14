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

const REMINDERS_API = "**/api/reminders";
const REMINDER_ONE_API = "**/api/reminders/*";
const REMINDER_ACK_API = "**/api/reminders/*/ack";
const DUE_API = "**/api/reminders/in-app/due";

type Reminder = {
  id: number;
  message: string;
  minutes: number;
  remind_at: string;
  delivery: string;
  status: string;
};

function reminder(
  overrides: Partial<Reminder> = {},
): Reminder {
  return {
    id: 1,
    message: "喝水",
    minutes: 5,
    remind_at: "2026-10-15T09:00:00+00:00",
    delivery: "in_app",
    status: "pending",
    ...overrides,
  };
}

// ==========================================
// 页面准备与登录
// ==========================================

/**
 * 顺序：先兜底拦截，再认证 mock。
 * 用例如果要覆盖提醒接口，在 login() 之前注册自己的 route。
 */
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

// ==========================================
// Personal Agent 站内提醒
// ==========================================

test.describe("Personal Agent Reminders", () => {

  // 1. 面板列出自己的提醒，并区分状态

  test("提醒面板列出自己的提醒", async ({ page }) => {
    const unexpected = await setupPage(page);

    await page.route(
      REMINDERS_API,
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            enabled: true,
            reminders: [
              reminder({
                id: 7,
                message: "喝水",
                status: "pending",
              }),
              reminder({
                id: 8,
                message: "交周报",
                status: "cancelled",
              }),
            ],
          }),
        });
      },
    );

    await login(page);

    // 侧栏入口显示待发数量
    await expect(
      page.getByRole("button", { name: "提醒" }),
    ).toContainText("1");

    await page
      .getByRole("button", { name: "提醒" })
      .click();

    await expect(
      page.getByLabel("我的提醒"),
    ).toBeVisible();

    await expect(
      page.getByText("喝水", { exact: true }),
    ).toBeVisible();

    await expect(
      page.getByText("交周报", { exact: true }),
    ).toBeVisible();

    await expect(page.getByText("待发送")).toBeVisible();
    await expect(page.getByText("已取消")).toBeVisible();

    // 已取消的提醒没有操作按钮
    await expect(
      page.getByRole("button", { name: "删除提醒 7" }),
    ).toHaveCount(1);

    await expect(
      page.getByRole("button", { name: "删除提醒 8" }),
    ).toHaveCount(0);

    expectNoUnmockedApi(unexpected);
  });

  // 2. 没有提醒时显示空状态

  test("没有提醒时显示空状态", async ({ page }) => {
    const unexpected = await setupPage(page);

    await login(page);

    await page
      .getByRole("button", { name: "提醒" })
      .click();

    await expect(
      page.getByTestId("reminder-empty"),
    ).toHaveText("还没有提醒");

    expectNoUnmockedApi(unexpected);
  });

  // 3. 删除提醒：调用后端并刷新列表

  test("删除提醒会调用后端并从列表移除", async ({
    page,
  }) => {
    const unexpected = await setupPage(page);

    let reminders: Reminder[] = [
      reminder({ id: 7, message: "喝水" }),
    ];

    const requests: string[] = [];

    await page.route(
      REMINDERS_API,
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            enabled: true,
            reminders,
          }),
        });
      },
    );

    await page.route(
      REMINDER_ONE_API,
      async (route) => {
        const request = route.request();

        requests.push(
          `${request.method()} ${new URL(request.url()).pathname}`,
        );

        // 提醒接口必须带当前身份
        expect(
          request.headers()["authorization"],
        ).toBe(`Bearer ${TEST_TOKEN}`);

        reminders = [];

        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 7,
            status: "cancelled",
            changed: true,
          }),
        });
      },
    );

    await login(page);

    await page
      .getByRole("button", { name: "提醒" })
      .click();

    await expect(
      page.getByText("喝水", { exact: true }),
    ).toBeVisible();

    await page
      .getByRole("button", { name: "删除提醒 7" })
      .click();

    await expect(
      page.getByTestId("reminder-empty"),
    ).toBeVisible();

    expect(requests).toEqual(["DELETE /api/reminders/7"]);

    expectNoUnmockedApi(unexpected);
  });

  // 4. 改时间：提交分钟数并刷新列表

  test("改时间会提交分钟数并刷新列表", async ({
    page,
  }) => {
    const unexpected = await setupPage(page);

    let reminders: Reminder[] = [
      reminder({ id: 7, message: "喝水" }),
    ];

    let listCalls = 0;
    let patched: unknown = null;

    await page.route(
      REMINDERS_API,
      async (route) => {
        listCalls += 1;

        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            enabled: true,
            reminders,
          }),
        });
      },
    );

    await page.route(
      REMINDER_ONE_API,
      async (route) => {
        patched = route.request().postDataJSON();

        reminders = [
          reminder({
            id: 7,
            message: "喝水",
            minutes: 30,
            remind_at: "2026-10-15T10:00:00+00:00",
          }),
        ];

        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 7,
            status: "pending",
            changed: true,
          }),
        });
      },
    );

    await login(page);

    await page
      .getByRole("button", { name: "提醒" })
      .click();

    await page
      .getByRole("spinbutton", {
        name: "新的提醒分钟数 7",
      })
      .fill("30");

    await page
      .getByRole("button", { name: "改时间 7" })
      .click();

    // 改动提交后必须重新拉取列表
    await expect.poll(() => listCalls).toBeGreaterThan(1);

    expect(patched).toEqual({ minutes: 30 });

    expectNoUnmockedApi(unexpected);
  });

  // 5. 到期提醒：提示条 + 确认后消失

  test("到期提醒显示提示条，确认后消失", async ({
    page,
  }) => {
    const unexpected = await setupPage(page);

    let due = [
      {
        id: 3,
        message: "该喝水了",
        remind_at: "2026-10-15T09:00:00+00:00",
      },
    ];

    const acks: string[] = [];

    // 提示条在进入工作空间时就会出现，所以必须在登录前注册
    await page.route(
      DUE_API,
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            enabled: true,
            reminders: due,
          }),
        });
      },
    );

    await page.route(
      REMINDER_ACK_API,
      async (route) => {
        acks.push(route.request().method());

        due = [];

        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 3,
            status: "sent",
            changed: true,
          }),
        });
      },
    );

    await login(page);

    await expect(
      page.getByLabel("到期的站内提醒"),
    ).toBeVisible();

    await expect(page.getByText("该喝水了")).toBeVisible();

    await page
      .getByRole("button", { name: "知道了 3" })
      .click();

    await expect(
      page.getByLabel("到期的站内提醒"),
    ).toHaveCount(0);

    expect(acks).toEqual(["POST"]);

    expectNoUnmockedApi(unexpected);
  });
});
