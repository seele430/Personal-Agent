import { test, expect, type Page, type Route } from "@playwright/test";

import {
  blockUnmockedApi,
  expectNoUnmockedApi,
  installArtifactMocks,
  installAuthMocks,
  installReminderMocks,
} from "./support/mockAuth";

const TEST_TOKEN = "playwright-sleep-token";
const TEST_USER = {
  id: "playwright-sleep-user",
  username: "sleep",
  role: "member",
  active: true,
};

type SleepRun = {
  id: number;
  created_at: string;
  mode: string;
  ab_variant: string;
  ops_count: number;
  applied_count: number;
  execution_status: string;
  gate_status: string;
  record_status: string;
  recall_before: number | null;
  recall_after: number | null;
  mrr_before: number | null;
  mrr_after: number | null;
};

function run(overrides: Partial<SleepRun> = {}): SleepRun {
  return {
    id: 1,
    created_at: "2026-11-11T04:00:00+00:00",
    mode: "plan",
    ab_variant: "none",
    ops_count: 2,
    applied_count: 0,
    execution_status: "succeeded",
    gate_status: "not_applicable",
    record_status: "complete",
    recall_before: 1.0,
    recall_after: 1.0,
    mrr_before: 1.0,
    mrr_after: 1.0,
    ...overrides,
  };
}

async function setupPage(page: Page): Promise<string[]> {
  const unexpected = await blockUnmockedApi(page);
  await installAuthMocks(page, TEST_TOKEN, TEST_USER);
  await installReminderMocks(page);
  await installArtifactMocks(page);

  return unexpected;
}

async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.locator("#pa-login-username").fill("sleep");
  await page.locator("#pa-login-password").fill("PlaywrightTestPassword2026!");
  await page.getByRole("button", { name: /登录工作空间/ }).click();
  await expect(page.getByRole("textbox", { name: "输入聊天消息" })).toBeVisible();
}

async function openSleep(page: Page): Promise<void> {
  await page.getByRole("button", { name: "睡眠" }).click();
  await expect(page.getByRole("heading", { name: "睡眠整理" })).toBeVisible();
}

test.describe("Sleep 面板", () => {
  test("空态", async ({ page }) => {
    const unexpected = await setupPage(page);
    await page.route("**/api/sleep/runs*", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], total: 0, limit: 20, offset: 0 }),
      });
    });

    await login(page);
    await openSleep(page);
    await expect(page.getByText("还没有睡眠记录")).toBeVisible();
    expectNoUnmockedApi(unexpected);
  });

  test("列表与详情", async ({ page }) => {
    const unexpected = await setupPage(page);
    await page.route("**/api/sleep/runs*", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [run()], total: 1, limit: 20, offset: 0 }),
      });
    });
    await page.route("**/api/sleep/runs/1", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...run(),
          base_snapshot_id: 3,
          candidate_id: "c1",
          error_code: "",
          record_error: "",
          ops: [
            {
              op: "expire",
              reason: "exact_dedupe",
              item_id: 12,
              item_key: "deadline",
              evidence_ids: [31],
              evidence_keys: ["deadline"],
            },
          ],
          reports: {},
          metrics: { recall_before: 1.0, recall_after: 1.0 },
        }),
      });
    });

    await login(page);
    await openSleep(page);
    await expect(page.locator(".sleep-table tbody tr")).toHaveCount(1);
    await page.locator(".sleep-table tbody tr").first().click();
    await expect(page.getByText("过期重复条目")).toBeVisible();
    await expect(page.getByText("deadline").first()).toBeVisible();
    expectNoUnmockedApi(unexpected);
  });

  test("过滤参数", async ({ page }) => {
    const unexpected = await setupPage(page);
    const requests: string[] = [];
    await page.route("**/api/sleep/runs*", async (route: Route) => {
      requests.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], total: 0, limit: 20, offset: 0 }),
      });
    });

    await login(page);
    await openSleep(page);
    await page.getByLabel("模式").selectOption("ab");
    await page.getByLabel("门槛").selectOption("failed");
    await page.getByRole("button", { name: "刷新" }).click();

    await expect.poll(() => requests.some((url) => url.includes("mode=ab") && url.includes("gate_status=failed"))).toBe(true);
    expectNoUnmockedApi(unexpected);
  });
});
