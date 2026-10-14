import { test, expect, type Page, type Route } from "@playwright/test";

import {
  blockUnmockedApi,
  expectNoUnmockedApi,
  installAuthMocks,
  type MockUser,
} from "./support/mockAuth";

const CHAT_API = "**/api/chat";
const STATUS_API = "**/api/chat/requests/*";

// 全部使用模拟账户，不涉及真实密码
const TEST_TOKEN = "playwright-idempotency-token";

const TEST_USER: MockUser = {
  id: "playwright-idempotency-user",
  username: "playwright",
  role: "member",
  active: true,
};

const CHAT_STORAGE_KEY = `personal-agent-user-${TEST_USER.id}-chat`;

type ChatPayload = {
  message: string;
  request_id: string;
};

type StoredMessage = {
  role: string;
  requestId?: string;
};


// ==========================================
// 页面准备与登录
// ==========================================

/**
 * 顺序：先兜底拦截，再认证 mock。
 * 用例随后注册的 CHAT_API / STATUS_API mock 优先级最高。
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

async function openAuthenticatedChat(page: Page): Promise<void> {
  await page.goto("/login");
  await signIn(page);
}


// ==========================================
// 聊天测试工具
// ==========================================

async function send(page: Page, message: string): Promise<void> {
  await page.getByRole("textbox", { name: "输入聊天消息" }).fill(message);
  await page.getByRole("button", { name: "发送 ↑" }).click();
}

function getRequestId(url: string): string {
  return decodeURIComponent(new URL(url).pathname.split("/").pop() ?? "");
}

function checkAuth(route: Route): void {
  expect(route.request().headers()["authorization"]).toBe(
    `Bearer ${TEST_TOKEN}`,
  );
}


// ==========================================
// Chat Idempotency
// ==========================================

test.describe("Chat Idempotency", () => {

  // 1. 每条新消息有独立 request_id
  test("每条新消息都有独立 request_id", async ({ page }) => {
    const unexpected = await setupPage(page);

    const requests: ChatPayload[] = [];

    await page.route(CHAT_API, async (route) => {
      checkAuth(route);

      const data = route.request().postDataJSON() as ChatPayload;

      requests.push(data);

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: data.request_id,
          status: "completed",
          reply: `已回复：${data.message}`,
        }),
      });
    });

    await openAuthenticatedChat(page);

    await send(page, "第一条消息");
    await expect(page.getByText("已回复：第一条消息")).toBeVisible();

    await send(page, "第二条消息");
    await expect(page.getByText("已回复：第二条消息")).toBeVisible();

    expect(requests).toHaveLength(2);

    expect(requests[0].request_id).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
    expect(requests[1].request_id).toMatch(/^[A-Za-z0-9_-]{8,128}$/);

    expect(requests[0].request_id).not.toBe(requests[1].request_id);

    expectNoUnmockedApi(unexpected);
  });


  // 2. 断网后查询原 request_id
  test("断网后使用原 request_id 查询回复", async ({ page }) => {
    const unexpected = await setupPage(page);

    let postCount = 0;
    let originalId = "";

    const queriedIds: string[] = [];

    await page.route(CHAT_API, async (route) => {
      checkAuth(route);

      postCount += 1;

      const data = route.request().postDataJSON() as ChatPayload;

      originalId = data.request_id;

      // 模拟网络断开（显式模拟，不算作漏 mock）
      await route.abort("failed");
    });

    await page.route(STATUS_API, async (route) => {
      checkAuth(route);

      const requestId = getRequestId(route.request().url());

      queriedIds.push(requestId);

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: requestId,
          status: "completed",
          reply: "服务器已保存的原始回复",
        }),
      });
    });

    await openAuthenticatedChat(page);

    await send(page, "断网恢复测试");

    await expect(page.getByRole("alert")).toContainText("请求结果尚未确认");

    expect(postCount).toBe(1);
    expect(originalId).toBeTruthy();

    await page.getByRole("button", { name: "查询结果" }).click();

    await expect(page.getByText("服务器已保存的原始回复")).toBeVisible();

    expect(queriedIds).toEqual([originalId]);

    // 查询不能再次发送 POST
    expect(postCount).toBe(1);

    await expect(page.getByText("断网恢复测试")).toBeVisible();

    expectNoUnmockedApi(unexpected);
  });


  // 3. 刷新后通过 /api/auth/me 恢复会话，并继续查询处理中的请求
  test("刷新页面恢复处理中请求且不重复 POST", async ({ page }) => {
    const unexpected = await setupPage(page);

    let postCount = 0;
    let queryCount = 0;
    let requestId = "";

    let allowCompletion = false;

    await page.route(CHAT_API, async (route) => {
      checkAuth(route);

      postCount += 1;

      const data = route.request().postDataJSON() as ChatPayload;

      requestId = data.request_id;

      // 模拟服务器正在处理
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: requestId,
          status: "processing",
        }),
      });
    });

    await page.route(STATUS_API, async (route) => {
      checkAuth(route);

      queryCount += 1;

      const queriedId = getRequestId(route.request().url());

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: queriedId,
          status: allowCompletion ? "completed" : "processing",
          reply: allowCompletion ? "刷新后成功恢复的回复" : null,
        }),
      });
    });

    await openAuthenticatedChat(page);

    await send(page, "刷新恢复测试");

    await expect.poll(() => postCount).toBe(1);
    await expect.poll(() => queryCount).toBeGreaterThan(0);

    // request_id 必须保存到用户自己的缓存
    await expect
      .poll(async () => {
        return page.evaluate((key) => {
          const raw = sessionStorage.getItem(key);

          if (!raw) {
            return null;
          }

          const messages = JSON.parse(raw) as StoredMessage[];

          const userMessage = messages.find((item) => item.role === "user");

          return userMessage?.requestId ?? null;
        }, CHAT_STORAGE_KEY);
      })
      .toBe(requestId);

    await page.reload();

    // token 保存在 sessionStorage，刷新后应直接恢复会话，
    // 不应出现登录表单。
    await expect(
      page.getByRole("textbox", { name: "输入聊天消息" }),
    ).toBeVisible();

    await expect(page.locator("#pa-login-username")).toHaveCount(0);

    // 会话恢复之后才允许完成，确保回复来自刷新后的查询。
    const queriesBeforeCompletion = queryCount;
    allowCompletion = true;

    await expect(page.getByText("刷新后成功恢复的回复")).toBeVisible({
      timeout: 10000,
    });

    await expect(page.getByText("刷新恢复测试")).toBeVisible();

    // 刷新后又查询过，且没有重新 POST
    expect(queryCount).toBeGreaterThan(queriesBeforeCompletion);
    expect(postCount).toBe(1);

    expectNoUnmockedApi(unexpected);
  });

});