// 活动时间线面板：打开、空态、有数据、关闭。

import { expect, test, type Page } from "@playwright/test";

import {
  TEST_TOKEN,
  TEST_USER,
  installAuthMocks,
} from "./support/mockAuth";

async function openChat(page: Page): Promise<void> {
  await page.goto("/login");

  await page.locator("#pa-login-username").fill("playwright");
  await page.locator("#pa-login-password").fill("PlaywrightTestPassword2026!");
  await page.getByRole("button", { name: /登录工作空间/ }).click();

  await expect(
    page.getByRole("textbox", { name: "输入聊天消息" }),
  ).toBeVisible();
}

test.describe("活动时间线", () => {
  test.beforeEach(async ({ page }) => {
    await installAuthMocks(page, TEST_TOKEN, TEST_USER);
    await openChat(page);
  });

  test("点活动按钮打开面板，空态提示正确", async ({ page }) => {
    await page.getByRole("button", { name: "活动" }).click();

    const panel = page.getByTestId("activity-timeline");
    await expect(panel).toBeVisible();
    await expect(panel.getByText("暂无活动记录")).toBeVisible();
  });

  test("有事件时展示时间、来源与标题", async ({ page }) => {
    await page.route("**/api/activity/timeline*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          events: [
            {
              ts: "2026-11-08T07:30:00+00:00",
              source: "artifact",
              event_id: "a1",
              title: "验收报告",
              status: "markdown",
            },
            {
              ts: "2026-11-08T07:31:00+00:00",
              source: "exec",
              event_id: "e1",
              title: "exec run-01",
              status: "succeeded",
            },
          ],
          count: 2,
        }),
      }),
    );

    await page.getByRole("button", { name: "活动" }).click();

    const panel = page.getByTestId("activity-timeline");
    await expect(panel.getByText("验收报告")).toBeVisible();
    await expect(panel.getByText("产物")).toBeVisible();
    await expect(panel.getByText("exec run-01")).toBeVisible();
  });

  test("可以关闭面板", async ({ page }) => {
    await page.getByRole("button", { name: "活动" }).click();
    await expect(page.getByTestId("activity-timeline")).toBeVisible();

    await page.getByRole("button", { name: "关闭活动时间线" }).click();
    await expect(page.getByTestId("activity-timeline")).toHaveCount(0);
  });
});