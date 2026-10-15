import { expect, type Page, type Route } from "@playwright/test";

/**
 * 前端测试的认证 mock 与网络兜底。
 *
 * 用法（顺序很重要）：
 *   const unexpected = await blockUnmockedApi(page);   // 1. 先注册兜底
 *   await installAuthMocks(page, TEST_TOKEN, TEST_USER); // 2. 再注册具体 mock
 *   await page.route(CHAT_API, ...);                     // 3. 用例自己的 mock
 *   ...
 *   expectNoUnmockedApi(unexpected);                     // 4. 结尾断言
 *
 * Playwright 按注册的逆序匹配路由：后注册的具体 mock 优先，
 * 没被任何 mock 处理的 /api/* 请求落到兜底，被中止并记录。
 * 这样请求不会经过 Vite 代理到达 127.0.0.1:8000 的真实后端。
 */

export type MockUser = {
  id: string;
  username: string;
  role: string;
  active: boolean;
};

export const TEST_TOKEN = "personal-agent-test-token";

export const TEST_USER: MockUser = {
  id: "test-user-1",
  username: "tester",
  role: "member",
  active: true,
};

export const REMINDERS_API = "**/api/reminders";
export const DUE_REMINDERS_API = "**/api/reminders/in-app/due";

/**
 * 站内提醒的默认 mock：空列表。
 *
 * App 一打开就会取一次到期提醒（之后每 60 秒轮询），侧栏也会取
 * 提醒列表，所以所有用例都需要这个兜底，否则会被 blockUnmockedApi
 * 记为"漏 mock"。要断言真实数据的用例，在 setupPage 之后再注册
 * 自己的 route 即可覆盖（Playwright 后注册的优先）。
 */
export async function installReminderMocks(page: Page): Promise<void> {
  const empty = JSON.stringify({
    enabled: true,
    reminders: [],
  });

  await page.route(DUE_REMINDERS_API, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: empty,
    }),
  );

  await page.route(REMINDERS_API, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: empty,
    }),
  );
}

export const INBOX_API = "**/api/inbox";

/**
 * 收件箱的默认 mock：空列表。
 *
 * 任务中心打开时会取一次自己的收件箱（任何角色都能取），
 * 所以与提醒同理需要全局兜底。注意这个 pattern 不会命中
 * `/api/inbox/{kind}/{id}/ack`，确认接口由用例自己 mock。
 */
export async function installInboxMocks(
  page: Page,
): Promise<void> {
  await page.route(INBOX_API, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
}

export async function blockUnmockedApi(page: Page): Promise<string[]> {
  const unexpected: string[] = [];

  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    unexpected.push(`${request.method()} ${request.url()}`);
    await route.abort("connectionrefused");
  });

  return unexpected;
}

export async function installAuthMocks(
  page: Page,
  token: string = TEST_TOKEN,
  user: MockUser = TEST_USER,
): Promise<void> {
  // 与后端一致：登录返回 { token, token_type, user }
  await page.route("**/api/auth/login", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ token, token_type: "bearer", user }),
    }),
  );

  // 与后端一致：/auth/me 返回 { user }；token 不匹配返回 401
  await page.route("**/api/auth/me", (route) => {
    // Playwright 返回的 header 名是小写
    const ok =
      route.request().headers()["authorization"] === `Bearer ${token}`;

    return route.fulfill({
      status: ok ? 200 : 401,
      contentType: "application/json",
      body: JSON.stringify(ok ? { user } : { detail: "请先登录" }),
    });
  });

  await page.route("**/api/auth/logout", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok" }),
    }),
  );

  // 已登录就一定会轮询站内提醒，这里一并兜底。
  await installReminderMocks(page);

  // 任务中心打开时会取收件箱；没打开的用例不会命中，
  // 但兜底放着可以让所有用例共用同一套 setup。
  await installInboxMocks(page);
}

export function expectNoUnmockedApi(unexpected: string[]): void {
  expect(unexpected, "存在未 mock 的 API 请求").toEqual([]);
}