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
  role: "owner",
  active: true,
};

const MONITORS_API = "**/api/monitors";
const MONITOR_DELETE_API = "**/api/monitors/*";
const MONITOR_PAUSE_API = "**/api/monitors/*/pause";
const MONITOR_RESUME_API = "**/api/monitors/*/resume";

type MonitorTask = {
  id: string;
  owner_id: string;
  task_type: string;
  source_url: string;
  interval_minutes: number;
  status: string;
  last_checked_at: string | null;
  next_run_at: string | null;
  last_error: string | null;
  created_at: string;
  archived_at: string | null;
};

function task(
  overrides: Partial<MonitorTask> = {},
): MonitorTask {
  return {
    id: "rss_001",
    owner_id: "playwright-e2e-user",
    task_type: "rss",
    source_url: "https://blog.python.org/rss.xml",
    interval_minutes: 30,
    status: "active",
    last_checked_at: null,
    next_run_at: "2026-10-15T12:00:00+00:00",
    last_error: null,
    created_at: "2026-10-15T11:00:00+00:00",
    archived_at: null,
    ...overrides,
  };
}

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

async function openTaskCenter(page: Page): Promise<void> {
  await page
    .getByRole("button", { name: "任务中心" })
    .click();

  // exact：否则 "监控任务" 会同时命中
  // "刷新监控任务" 与 "创建监控任务"
  await expect(
    page.getByLabel("监控任务", { exact: true }),
  ).toBeVisible();
}

test.describe("Personal Agent Monitors", () => {

  // 1. 列出自己的监控任务

  test("任务中心列出监控任务与状态", async ({ page }) => {
    const unexpected = await setupPage(page);

    await page.route(MONITORS_API, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          monitors: [
            task({ id: "rss_001" }),
            task({
              id: "rss_002",
              source_url: "https://example.com/feed.xml",
              status: "paused",
              last_error: "HTTP 503",
            }),
          ],
        }),
      }),
    );

    await login(page);
    await openTaskCenter(page);

    await expect(
      page.getByText("https://blog.python.org/rss.xml"),
    ).toBeVisible();

    await expect(
      page.getByText("https://example.com/feed.xml"),
    ).toBeVisible();

    await expect(page.getByText("运行中")).toBeVisible();
    await expect(page.getByText("已暂停")).toBeVisible();

    // 每行标出数据源类型。
    // 注意：下拉框里也有同样的字，且 <option> 永远"不可见"，
    // 所以必须限定在列表行内断言。
    await expect(
      page.locator(".monitor-row").first(),
    ).toContainText("RSS 订阅");
    await expect(page.getByText("HTTP 503")).toBeVisible();

    // 运行中显示"暂停"，已暂停显示"恢复"
    await expect(
      page.getByRole("button", { name: "暂停 rss_001" }),
    ).toHaveCount(1);

    await expect(
      page.getByRole("button", { name: "恢复 rss_002" }),
    ).toHaveCount(1);

    expectNoUnmockedApi(unexpected);
  });

  // 2. 创建监控任务

  test("创建监控任务会提交地址与间隔", async ({ page }) => {
    const unexpected = await setupPage(page);

    let created: unknown = null;
    let monitors: MonitorTask[] = [];

    await page.route(MONITORS_API, async (route) => {
      const request = route.request();

      if (request.method() === "POST") {
        created = request.postDataJSON();

        monitors = [
          task({
            id: "rss_new",
            source_url: "https://example.com/feed.xml",
            interval_minutes: 60,
          }),
        ];

        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({ id: "rss_new" }),
        });

        return;
      }

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ monitors }),
      });
    });

    await login(page);
    await openTaskCenter(page);

    await expect(
      page.getByTestId("monitor-empty"),
    ).toBeVisible();

    await page
      .getByRole("textbox", { name: "订阅地址" })
      .fill("https://example.com/feed.xml");

    await page
      .getByRole("spinbutton", {
        name: "检查间隔分钟数",
      })
      .fill("60");

    await page
      .getByRole("button", { name: "创建监控任务" })
      .click();

    await expect(
      page.getByTestId("monitor-notice"),
    ).toContainText("已创建");

    expect(created).toEqual({
      source_url: "https://example.com/feed.xml",
      interval_minutes: 60,
      source_type: "rss",
    });

    await expect(
      page.getByText("https://example.com/feed.xml"),
    ).toBeVisible();

    expectNoUnmockedApi(unexpected);
  });

  // 3. 暂停与恢复

  test("暂停后状态变为已暂停", async ({ page }) => {
    const unexpected = await setupPage(page);

    let monitors = [task({ id: "rss_001" })];

    await page.route(MONITORS_API, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ monitors }),
      }),
    );

    const paused: string[] = [];

    await page.route(MONITOR_PAUSE_API, async (route) => {
      paused.push(route.request().url());

      monitors = [
        task({ id: "rss_001", status: "paused" }),
      ];

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "rss_001",
          status: "paused",
        }),
      });
    });

    await login(page);
    await openTaskCenter(page);

    await page
      .getByRole("button", { name: "暂停 rss_001" })
      .click();

    await expect(
      page.getByRole("button", { name: "恢复 rss_001" }),
    ).toHaveCount(1);

    await expect(page.getByText("已暂停")).toBeVisible();

    expect(paused.length).toBe(1);

    expectNoUnmockedApi(unexpected);
  });

  // 4. 归档

  test("归档后从列表消失", async ({ page }) => {
    const unexpected = await setupPage(page);

    let monitors = [task({ id: "rss_001" })];

    const deleted: string[] = [];

    await page.route(MONITORS_API, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ monitors }),
      }),
    );

    await page.route(MONITOR_DELETE_API, async (route) => {
      deleted.push(`${route.request().method()} ${route.request().url()}`);

      monitors = [];

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "rss_001",
          status: "archived",
        }),
      });
    });

    await login(page);
    await openTaskCenter(page);

    await page
      .getByRole("button", { name: "归档 rss_001" })
      .click();

    await expect(
      page.getByTestId("monitor-empty"),
    ).toBeVisible();

    await expect(
      page.getByTestId("monitor-notice"),
    ).toContainText("已归档");

    expect(deleted.length).toBe(1);
    expect(deleted[0]).toContain("DELETE");

    expectNoUnmockedApi(unexpected);
  });

  // 5. 没有权限时明确提示，且不给创建入口

  test("无权限时显示提示并隐藏创建表单", async ({ page }) => {
    const unexpected = await setupPage(page);

    await page.route(MONITORS_API, (route) =>
      route.fulfill({
        status: 403,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "没有管理监控任务的权限",
        }),
      }),
    );

    await login(page);
    await openTaskCenter(page);

    await expect(
      page.getByTestId("monitor-forbidden"),
    ).toBeVisible();

    await expect(
      page.getByRole("button", { name: "创建监控任务" }),
    ).toHaveCount(0);

    await expect(
      page.getByTestId("monitor-error"),
    ).toHaveCount(0);

    expectNoUnmockedApi(unexpected);
  });

  // 6. 没打开任务中心就不应该请求监控接口

  test("未打开任务中心时不请求监控接口", async ({
    page,
  }) => {
    const unexpected = await setupPage(page);

    const calls: string[] = [];

    await page.route(MONITORS_API, (route) => {
      calls.push(route.request().url());

      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ monitors: [] }),
      });
    });

    await login(page);

    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toBeVisible();

    expect(calls).toEqual([]);

    await openTaskCenter(page);

    await expect.poll(() => calls.length).toBeGreaterThan(0);

    expectNoUnmockedApi(unexpected);
  });

  // 7. 选择第二种数据源类型

  test("可以选择第二种数据源类型", async ({ page }) => {
    const unexpected = await setupPage(page);

    let created: unknown = null;

    await page.route(MONITORS_API, async (route) => {
      const request = route.request();

      if (request.method() === "POST") {
        created = request.postDataJSON();

        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({ id: "http_text_new" }),
        });

        return;
      }

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          monitors: [],
          source_types: ["rss", "http_text"],
        }),
      });
    });

    await login(page);
    await openTaskCenter(page);

    // 下拉选项由后端声明的支持列表决定
    await page
      .getByLabel("数据源类型")
      .selectOption("http_text");

    await page
      .getByRole("textbox", { name: "订阅地址" })
      .fill("https://example.com/page");

    await page
      .getByRole("button", { name: "创建监控任务" })
      .click();

    await expect.poll(() => created).toEqual({
      source_url: "https://example.com/page",
      interval_minutes: 30,
      source_type: "http_text",
    });

    expectNoUnmockedApi(unexpected);
  });
});
