// 视频总结面板：打开、成功返回要点、422 无法验证、关闭。

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

test.describe("视频总结面板", () => {
  test.beforeEach(async ({ page }) => {
    await installAuthMocks(page, TEST_TOKEN, TEST_USER);
    await openChat(page);
  });

  test("打开面板并看到文件放置提示", async ({ page }) => {
    await page.getByRole("button", { name: "视频" }).click();

    const panel = page.getByTestId("video-panel");
    await expect(panel).toBeVisible();
    await expect(panel.getByText("data/videos/")).toBeVisible();
  });

  test("提交后展示要点与用量", async ({ page }) => {
    await page.route("**/api/video/summarize", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          video: "demo.mp4",
          duration_s: 60,
          language: "zh",
          speech_chars: 220,
          points: [
            { text: "第一个要点", start: 1.0, end: 3.0, segments: [0] },
            { text: "第二个要点", start: 20.0, end: 22.0, segments: [5] },
          ],
          usage: { prompt: 100, completion: 50, total: 150 },
          truncated: false,
          report_path: "report.md",
        }),
      }),
    );

    await page.getByRole("button", { name: "视频" }).click();
    await page.getByLabel("视频文件名").fill("demo.mp4");
    await page.getByRole("button", { name: "开始总结" }).click();

    const result = page.getByTestId("video-result");
    await expect(result).toContainText("第一个要点");
    await expect(result).toContainText("150 tokens");
  });

  test("422 显示无法验证而不是空结果", async ({ page }) => {
    await page.route("**/api/video/summarize", (route) =>
      route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ detail: "转写不足：只有 4 个非空白字符" }),
      }),
    );

    await page.getByRole("button", { name: "视频" }).click();
    await page.getByLabel("视频文件名").fill("silent.mp4");
    await page.getByRole("button", { name: "开始总结" }).click();

    await expect(page.getByText(/无法验证：转写不足/)).toBeVisible();
    await expect(page.getByTestId("video-result")).toHaveCount(0);
  });
});
