
import {
  test,
  expect,
  type Page,
} from "@playwright/test";

const CHAT_API = "**/api/chat";
const STATUS_API = "**/api/chat/requests/*";

type ChatPayload = {
  message: string;
  request_id: string;
};

async function send(
  page: Page,
  message: string
): Promise<void> {
  await page
    .getByRole("textbox", {
      name: "输入聊天消息",
    })
    .fill(message);

  await page
    .getByRole("button", {
      name: "发送 ↑",
    })
    .click();
}

function getRequestId(url: string): string {
  return decodeURIComponent(
    new URL(url).pathname.split("/").pop() ?? ""
  );
}

test.describe("Chat Idempotency", () => {
  test(
    "每条新消息都有独立 request_id",
    async ({ page }) => {
      const requests: ChatPayload[] = [];

      await page.route(CHAT_API, async (route) => {
        const data =
          route.request().postDataJSON() as ChatPayload;

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

      await page.goto("/");

      await send(page, "第一条消息");

      await expect(
        page.getByText("已回复：第一条消息")
      ).toBeVisible();

      await send(page, "第二条消息");

      await expect(
        page.getByText("已回复：第二条消息")
      ).toBeVisible();

      expect(requests).toHaveLength(2);

      expect(requests[0].request_id).toMatch(
        /^[A-Za-z0-9_-]{8,128}$/
      );

      expect(requests[1].request_id).toMatch(
        /^[A-Za-z0-9_-]{8,128}$/
      );

      expect(requests[0].request_id).not.toBe(
        requests[1].request_id
      );
    }
  );

  test(
    "断网后使用原 request_id 查询回复",
    async ({ page }) => {
      let postCount = 0;
      let originalId = "";

      const queriedIds: string[] = [];

      await page.route(CHAT_API, async (route) => {
        postCount += 1;

        const data =
          route.request().postDataJSON() as ChatPayload;

        originalId = data.request_id;

        // 模拟请求没有收到响应
        await route.abort("failed");
      });

      await page.route(STATUS_API, async (route) => {
        const requestId = getRequestId(
          route.request().url()
        );

        queriedIds.push(requestId);

        // 模拟服务器已经完成请求
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

      await page.goto("/");

      await send(page, "断网恢复测试");

      await expect(
        page.getByRole("alert")
      ).toContainText("请求结果尚未确认");

      expect(postCount).toBe(1);
      expect(originalId).toBeTruthy();

      await page
        .getByRole("button", {
          name: "查询结果",
        })
        .click();

      await expect(
        page.getByText("服务器已保存的原始回复")
      ).toBeVisible();

      expect(queriedIds).toEqual([originalId]);

      // 查询不能重新发送 POST
      expect(postCount).toBe(1);

      await expect(
        page.getByText("断网恢复测试")
      ).toBeVisible();
    }
  );

  test(
    "刷新页面恢复处理中请求且不重复 POST",
    async ({ page }) => {
      let postCount = 0;
      let queryCount = 0;
      let requestId = "";

      let allowCompletion = false;

      await page.route(CHAT_API, async (route) => {
        postCount += 1;

        const data =
          route.request().postDataJSON() as ChatPayload;

        requestId = data.request_id;

        // 模拟服务端正在处理请求
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
        queryCount += 1;

        const queriedId = getRequestId(
          route.request().url()
        );

        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            request_id: queriedId,
            status: allowCompletion
              ? "completed"
              : "processing",
            reply: allowCompletion
              ? "刷新后成功恢复的回复"
              : null,
          }),
        });
      });

      await page.goto("/");

      await send(page, "刷新恢复测试");

      await expect
        .poll(() => postCount)
        .toBe(1);

      // 确认前端已经开始查询原请求
      await expect
        .poll(() => queryCount)
        .toBeGreaterThan(0);

      // 检查请求 ID 已保存到浏览器
      const storedId = await page.evaluate(() => {
        const raw = sessionStorage.getItem(
          "personal-agent-dev-chat"
        );

        if (!raw) return null;

        const messages = JSON.parse(raw) as Array<{
          role: string;
          requestId?: string;
        }>;

        return messages.find(
          (item) => item.role === "user"
        )?.requestId ?? null;
      });

      expect(storedId).toBe(requestId);

      // 模拟用户刷新浏览器
      await page.reload();

      // 模拟服务器后来完成了原请求
      allowCompletion = true;

      await expect(
        page.getByText("刷新后成功恢复的回复")
      ).toBeVisible({
        timeout: 10000,
      });

      await expect(
        page.getByText("刷新恢复测试")
      ).toBeVisible();

      // 刷新后不应重新执行模型请求
      expect(postCount).toBe(1);

      expect(queryCount).toBeGreaterThan(1);
    }
  );
});