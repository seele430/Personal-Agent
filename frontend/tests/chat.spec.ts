
import {
  test,
  expect,
  type Page,
  type Route,
} from "@playwright/test";

const CHAT_API = "**/api/chat";
const LOGIN_API = "**/api/auth/login";

// 测试专用身份，不是真实账户
const TEST_USER_ID = "playwright-e2e-user";
const TEST_TOKEN = "playwright-test-only-token";

const CHAT_STORAGE_KEY =
  `personal-agent-user-${TEST_USER_ID}-chat`;


// ==========================================
// 模拟登录并进入聊天界面
// ==========================================

async function openAuthenticatedChat(
  page: Page
): Promise<void> {

  // 拦截登录请求，不使用真实密码或账户数据库
  await page.route(
    LOGIN_API,
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          token: TEST_TOKEN,
          token_type: "bearer",
          user: {
            id: TEST_USER_ID,
            username: "playwright",
            role: "member",
            active: true,
          },
        }),
      });
    }
  );

  await page.goto("/login");

  await page.locator(
    "#pa-login-username"
  ).fill("playwright");

  await page.locator(
    "#pa-login-password"
  ).fill("PlaywrightTestPassword2026!");

  await page.getByRole("button", {
    name: /登录工作空间/,
  }).click();

  await expect(
    page.getByRole("textbox", {
      name: "输入聊天消息",
    })
  ).toBeVisible();
}


// ==========================================
// 通用测试工具
// ==========================================

async function sendMessage(
  page: Page,
  message: string
): Promise<void> {
  const input = page.getByRole("textbox", {
    name: "输入聊天消息",
  });

  await input.fill(message);

  await page.getByRole("button", {
    name: "发送 ↑",
  }).click();
}


async function mockReply(
  route: Route,
  reply: string
): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      status: "completed",
      reply,
    }),
  });
}


async function expectUnconfirmed(
  page: Page
): Promise<void> {
  const messageStatus = page.locator(
    ".message-status.warning"
  );

  await expect(messageStatus).toBeVisible();

  await expect(messageStatus).toHaveText(
    "请求结果尚未确认"
  );

  const errorAlert = page.getByRole("alert");

  await expect(errorAlert).toBeVisible();

  await expect(errorAlert).toContainText(
    "请求结果尚未确认"
  );
}


// ==========================================
// Personal Agent 已登录聊天测试
// ==========================================

test.describe("Personal Agent Chat", () => {

  // ----------------------------------------
  // 1. 正常聊天及身份信息
  // ----------------------------------------

  test("正常聊天能够收到回复", async ({
    page,
  }) => {

    await page.route(
      CHAT_API,
      async (route) => {

        // 验证聊天请求确实携带当前身份
        expect(
          route.request().headers()["authorization"]
        ).toBe(`Bearer ${TEST_TOKEN}`);

        await mockReply(
          route,
          "你好，我是测试助手。"
        );
      }
    );

    await openAuthenticatedChat(page);

    await sendMessage(page, "你好");

    await expect(
      page.getByText("你好，我是测试助手。")
    ).toBeVisible();

    await expect(
      page.getByRole("textbox", {
        name: "输入聊天消息",
      })
    ).toBeEnabled();
  });


  // ----------------------------------------
  // 2. 连续点击防重复发送
  // ----------------------------------------

  test("连续点击不会重复发送请求", async ({
    page,
  }) => {

    let requestCount = 0;
    let pendingRoute: Route | undefined;

    await page.route(CHAT_API, (route) => {
      requestCount += 1;
      pendingRoute = route;
    });

    await openAuthenticatedChat(page);

    await sendMessage(
      page,
      "测试重复发送"
    );

    await expect
      .poll(() => requestCount)
      .toBe(1);

    const sendButton = page.getByRole(
      "button",
      {
        name: "发送中…",
      }
    );

    await expect(sendButton).toBeDisabled();

    const box = await sendButton.boundingBox();

    if (!box) {
      throw new Error("发送按钮不存在");
    }

    for (let index = 0; index < 5; index++) {
      await page.mouse.click(
        box.x + box.width / 2,
        box.y + box.height / 2
      );
    }

    await page.waitForTimeout(200);

    expect(requestCount).toBe(1);

    if (!pendingRoute) {
      throw new Error("聊天请求没有被拦截");
    }

    await mockReply(
      pendingRoute,
      "这条消息只处理了一次"
    );

    await expect(
      page.getByText("这条消息只处理了一次")
    ).toBeVisible();

    expect(requestCount).toBe(1);
  });


  // ----------------------------------------
  // 3. 慢响应
  // ----------------------------------------

  test("慢响应时显示等待状态", async ({
    page,
  }) => {

    await page.route(
      CHAT_API,
      async (route) => {

        await new Promise<void>((resolve) => {
          setTimeout(resolve, 1500);
        });

        await mockReply(
          route,
          "慢响应测试完成"
        );
      }
    );

    await openAuthenticatedChat(page);

    await sendMessage(
      page,
      "测试慢响应"
    );

    await expect(
      page.getByRole("status")
    ).toContainText(
      "助手正在处理，请稍候…"
    );

    await expect(
      page.getByRole("button", {
        name: "发送中…",
      })
    ).toBeDisabled();

    await expect(
      page.getByText("慢响应测试完成")
    ).toBeVisible();
  });


  // ----------------------------------------
  // 4. HTTP 500 错误
  // ----------------------------------------

  test("HTTP 500 不应丢失用户消息", async ({
    page,
  }) => {

    await page.route(
      CHAT_API,
      async (route) => {

        await route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "Test server error",
          }),
        });
      }
    );

    await openAuthenticatedChat(page);

    await sendMessage(
      page,
      "测试服务器故障"
    );

    await expect(
      page.getByText("测试服务器故障")
    ).toBeVisible();

    await expectUnconfirmed(page);

    await expect(
      page.getByRole("textbox", {
        name: "输入聊天消息",
      })
    ).toBeEnabled();
  });


  // ----------------------------------------
  // 5. 网络中断
  // ----------------------------------------

  test("网络中断时保留消息", async ({
    page,
  }) => {

    await page.route(
      CHAT_API,
      async (route) => {
        await route.abort("failed");
      }
    );

    await openAuthenticatedChat(page);

    await sendMessage(
      page,
      "测试网络中断"
    );

    await expect(
      page.getByText("测试网络中断")
    ).toBeVisible();

    await expectUnconfirmed(page);

    await expect(
      page.getByRole("button", {
        name: "查询结果",
      })
    ).toBeVisible();
  });


  // ----------------------------------------
  // 6. 刷新后重新登录，恢复本人聊天记录
  // ----------------------------------------

  test("刷新页面能够恢复聊天记录", async ({
    page,
  }) => {

    await page.route(
      CHAT_API,
      async (route) => {
        await mockReply(
          route,
          "这是需要保留的回复"
        );
      }
    );

    await openAuthenticatedChat(page);

    await sendMessage(
      page,
      "记住这条消息"
    );

    await expect(
      page.getByText("这是需要保留的回复")
    ).toBeVisible();

    // 现在使用用户专属的缓存名称
    await expect
      .poll(async () => {
        return page.evaluate((key) => {
          const raw = sessionStorage.getItem(key);

          if (!raw) return 0;

          const data: unknown = JSON.parse(raw);

          return Array.isArray(data)
            ? data.length
            : 0;
        }, CHAT_STORAGE_KEY);
      })
      .toBe(2);

    await page.reload();

    // 当前登录凭证只保存在内存中，
    // 刷新后必须重新登录。
    await expect(
      page.locator("#pa-login-username")
    ).toBeVisible();

    await openAuthenticatedChat(page);

    await expect(
      page.getByText("记住这条消息")
    ).toBeVisible();

    await expect(
      page.getByText("这是需要保留的回复")
    ).toBeVisible();
  });


  // ----------------------------------------
  // 7. 错误后继续聊天
  // ----------------------------------------

  test("请求失败后仍能继续聊天", async ({
    page,
  }) => {

    let attempts = 0;

    await page.route(
      CHAT_API,
      async (route) => {

        attempts += 1;

        if (attempts === 1) {
          await route.fulfill({
            status: 500,
            contentType: "application/json",
            body: JSON.stringify({
              detail: "Simulated error",
            }),
          });

          return;
        }

        await mockReply(
          route,
          "服务已恢复正常"
        );
      }
    );

    await openAuthenticatedChat(page);

    await sendMessage(
      page,
      "第一次请求"
    );

    await expectUnconfirmed(page);

    await sendMessage(
      page,
      "第二次请求"
    );

    await expect(
      page.getByText("服务已恢复正常")
    ).toBeVisible();

    expect(attempts).toBe(2);

    await expect(
      page.getByText("第一次请求")
    ).toBeVisible();

    await expect(
      page.getByText("第二次请求")
    ).toBeVisible();
  });

});