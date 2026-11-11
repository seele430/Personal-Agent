import { test, expect, type Page, type Route } from "@playwright/test";

import {
  blockUnmockedApi,
  expectNoUnmockedApi,
  installArtifactMocks,
  installAuthMocks,
  installReminderMocks,
} from "./support/mockAuth";

const TEST_TOKEN = "playwright-memory-token";
const TEST_USER = {
  id: "playwright-memory-user",
  username: "memory",
  role: "member",
  active: true,
};

const ITEM = {
  id: 2,
  key: "drink",
  kind: "project_fact",
  layer: "project",
  text: "改为喜欢红茶",
  trust: "user_confirmed",
  valid_from: "2026-11-11T00:00:00+00:00",
  valid_to: null,
  supersedes_id: 1,
  created_at: "2026-11-11T00:00:00+00:00",
};

async function setupPage(page: Page): Promise<string[]> {
  const unexpected = await blockUnmockedApi(page);
  await installAuthMocks(page, TEST_TOKEN, TEST_USER);
  await installReminderMocks(page);
  await installArtifactMocks(page);

  return unexpected;
}

async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.locator("#pa-login-username").fill("memory");
  await page.locator("#pa-login-password").fill("PlaywrightTestPassword2026!");
  await page.getByRole("button", { name: /登录工作空间/ }).click();
  await expect(page.getByRole("textbox", { name: "输入聊天消息" })).toBeVisible();
}

async function openMemory(page: Page): Promise<void> {
  await page.getByRole("button", { name: "记忆" }).click();
  await expect(page.getByRole("heading", { name: "记忆" })).toBeVisible();
}

test.describe("记忆面板", () => {
  test("列表与空 key", async ({ page }) => {
    const unexpected = await setupPage(page);
    await page.route("**/api/memory/items*", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ mode: "list", items: [ITEM], total: 1, limit: 20, offset: 0, has_more: false }),
      });
    });

    await login(page);
    await openMemory(page);
    await expect(page.getByText("drink")).toBeVisible();
    await expect(page.getByText("改为喜欢红茶")).toBeVisible();
    expectNoUnmockedApi(unexpected);
  });

  test("搜索参数与清空", async ({ page }) => {
    const unexpected = await setupPage(page);
    const urls: string[] = [];
    await page.route("**/api/memory/items*", async (route: Route) => {
      urls.push(route.request().url());
      const search = route.request().url().includes("query=");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(search
          ? { mode: "search", items: [ITEM], count: 1, limit: 50, search_mode: "fts" }
          : { mode: "list", items: [ITEM], total: 1, limit: 20, offset: 0, has_more: false }),
      });
    });

    await login(page);
    await openMemory(page);
    await page.getByLabel("搜索记忆").fill("红茶");
    await page.getByRole("button", { name: "搜索" }).click();
    await expect.poll(() => urls.some((url) => url.includes("query="))).toBe(true);
    await page.getByRole("button", { name: "清除" }).click();
    await expect.poll(() => urls.some((url) => !url.includes("query="))).toBe(true);
    expectNoUnmockedApi(unexpected);
  });

  test("详情与历史链", async ({ page }) => {
    const unexpected = await setupPage(page);
    await page.route("**/api/memory/items?*", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ mode: "list", items: [ITEM], total: 1, limit: 20, offset: 0, has_more: false }),
      });
    });
    await page.route("**/api/memory/items/2", async (route: Route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ITEM) });
    });
    await page.route("**/api/memory/items/2/history", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          anchor_id: 2,
          current_id: 2,
          items: [
            { ...ITEM, id: 1, text: "喜欢绿茶", supersedes_id: null, deleted_at: null, is_current: false, version_status: "superseded" },
            { ...ITEM, deleted_at: null, is_current: true, version_status: "current" },
          ],
          count: 2,
          total_count: 2,
          truncated: false,
          window_has_current: true,
        }),
      });
    });

    await login(page);
    await openMemory(page);
    await page.locator(".memory-table tbody tr").first().click();
    await expect(page.locator(".memory-history").getByText(/已被取代/)).toBeVisible();
    await expect(page.locator(".memory-history").getByText(/当前/)).toBeVisible();
    await expect(page.getByText("喜欢绿茶")).toBeVisible();
    expectNoUnmockedApi(unexpected);
  });
});

test.describe("记忆编辑与删除", () => {
  test("编辑走同 key 新版本", async ({ page }) => {
    const unexpected = await setupPage(page);
    const posts: string[] = [];
    await page.route("**/api/memory/items?*", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ mode: "list", items: [ITEM], total: 1, limit: 20, offset: 0, has_more: false }),
      });
    });
    await page.route("**/api/memory/items/2", async (route: Route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ITEM) });
    });
    await page.route("**/api/memory/items/2/history", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ anchor_id: 2, current_id: 2, items: [], count: 0, total_count: 0, truncated: false, window_has_current: true }),
      });
    });
    await page.route("**/api/memory/items/3/history", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ anchor_id: 3, current_id: 3, items: [], count: 0, total_count: 0, truncated: false, window_has_current: true }),
      });
    });
    await page.route("**/api/memory/items", async (route: Route) => {
      if (route.request().method() !== "POST") {
        return route.continue();
      }

      posts.push(route.request().postData() ?? "");
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({ ...ITEM, id: 3 }),
      });
    });

    await login(page);
    await openMemory(page);
    await page.locator(".memory-table tbody tr").first().click();
    await page.getByRole("button", { name: "编辑" }).click();
    await page.getByLabel("编辑记忆内容").fill("改为喜欢乌龙");
    await page.getByRole("button", { name: "保存" }).click();
    await expect.poll(() => posts.length).toBe(1);
    expect(JSON.parse(posts[0])).toMatchObject({ text: "改为喜欢乌龙", key: "drink" });
    expectNoUnmockedApi(unexpected);
  });

  test("删除二次确认后才发 DELETE", async ({ page }) => {
    const unexpected = await setupPage(page);
    const deletes: string[] = [];
    let accepted = false;
    page.on("dialog", async (dialog) => {
      accepted = true;
      await dialog.accept();
    });
    await page.route("**/api/memory/items?*", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ mode: "list", items: [ITEM], total: 1, limit: 20, offset: 0, has_more: false }),
      });
    });
    await page.route("**/api/memory/items/2", async (route: Route) => {
      if (route.request().method() === "DELETE") {
        deletes.push(route.request().url());
        return route.fulfill({ status: 204, body: "" });
      }

      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ITEM) });
    });
    await page.route("**/api/memory/items/2/history", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ anchor_id: 2, current_id: 2, items: [], count: 0, total_count: 0, truncated: false, window_has_current: true }),
      });
    });

    await login(page);
    await openMemory(page);
    await page.locator(".memory-table tbody tr").first().click();
    await page.getByRole("button", { name: "删除" }).click();
    await expect.poll(() => deletes.length).toBe(1);
    expect(accepted).toBe(true);
    expectNoUnmockedApi(unexpected);
  });
});

test.describe("记忆边界", () => {
  test("无 key 条目禁止编辑", async ({ page }) => {
    const unexpected = await setupPage(page);
    const item = { ...ITEM, key: "" };
    await page.route("**/api/memory/items?*", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ mode: "list", items: [item], total: 1, limit: 20, offset: 0, has_more: false }),
      });
    });
    await page.route("**/api/memory/items/2", async (route: Route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(item) });
    });
    await page.route("**/api/memory/items/2/history", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ anchor_id: 2, current_id: 2, items: [], count: 0, total_count: 0, truncated: false, window_has_current: true }),
      });
    });

    await login(page);
    await openMemory(page);
    await page.locator(".memory-table tbody tr").first().click();
    await expect(page.getByRole("button", { name: "编辑" })).toBeDisabled();
    await expect(page.getByText("无 key 的记忆不能编辑版本")).toBeVisible();
    expectNoUnmockedApi(unexpected);
  });
});
