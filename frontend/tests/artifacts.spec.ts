import { test, expect, type Page } from "@playwright/test";

import {
  blockUnmockedApi,
  expectNoUnmockedApi,
  installAuthMocks,
  TEST_TOKEN,
  TEST_USER,
} from "./support/mockAuth";

/**
 * W4：产物卡片 → 预览侧栏 → 关闭 → 刷新后深链恢复。
 *
 * 全部 API 走 mock，不碰真实后端（与既有 spec 一致）。
 */

const ARTIFACTS_API = "**/api/artifacts";
const PREVIEW_API = "**/api/artifacts/*/preview";
const ACTIVITY_API = "**/api/activity/*/summary";

const ARTIFACT_ID = "art_demo1";

const ARTIFACT = {
  id: ARTIFACT_ID,
  title: "研究报告.md",
  kind: "markdown",
  media_type: "text/markdown",
  size_bytes: 2048,
  sha256: "a".repeat(64),
  run_id: "trace-demo",
  exec_id: "",
  created_at: "2026-11-07T10:00:00+00:00",
};

const PREVIEW = {
  ...ARTIFACT,
  previewable: true,
  mode: "markdown",
  text: "# 报告正文\n\n这是产物预览里的正文。",
  truncated: false,
};

async function setupPage(page: Page, artifacts: unknown[] = [ARTIFACT]) {
  const unexpected = await blockUnmockedApi(page);
  await installAuthMocks(page, TEST_TOKEN, TEST_USER);

  await page.route(ACTIVITY_API, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        trace_id: "trace-demo",
        span_count: 3,
        llm_calls: 2,
        tool_calls: 1,
        command_calls: 0,
        tts_calls: 0,
        other_calls: 0,
        error_count: 0,
        duration_ms: 1500,
        text: "共 3 步：2 次模型调用、1 次工具调用，耗时 1.5 秒，全部成功",
      }),
    }),
  );

  await page.route(PREVIEW_API, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PREVIEW),
    }),
  );

  await page.route(ARTIFACTS_API, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: artifacts }),
    }),
  );

  return unexpected;
}

async function openChat(page: Page): Promise<void> {
  await page.goto("/login");

  await page.locator("#pa-login-username").fill("playwright");
  await page.locator("#pa-login-password").fill("PlaywrightTestPassword2026!");
  await page.getByRole("button", { name: /登录工作空间/ }).click();

  await expect(
    page.getByRole("textbox", { name: "输入聊天消息" }),
  ).toBeVisible();
}

test("产物卡片可打开预览并在关闭后收起", async ({ page }) => {
  const unexpected = await setupPage(page);
  await openChat(page);

  const strip = page.getByTestId("artifact-strip");

  await expect(strip).toBeVisible();
  await expect(page.getByRole("button", { name: /打开产物 研究报告.md/ })).toBeVisible();

  await page.getByRole("button", { name: /打开产物 研究报告.md/ }).click();

  const sidebar = page.getByTestId("artifact-sidebar");

  await expect(sidebar).toBeVisible();
  await expect(page.getByTestId("artifact-text")).toContainText("报告正文");

  // 关闭：侧栏消失，hash 清空
  await page.getByRole("button", { name: "关闭预览" }).click();

  await expect(sidebar).toBeHidden();
  expect(new URL(page.url()).hash).toBe("");

  expectNoUnmockedApi(unexpected);
});

test("深链刷新后自动恢复预览", async ({ page }) => {
  const unexpected = await setupPage(page);
  await openChat(page);

  await page.getByRole("button", { name: /打开产物 研究报告.md/ }).click();
  await expect(page.getByTestId("artifact-sidebar")).toBeVisible();

  expect(new URL(page.url()).hash).toBe(`#artifact=${ARTIFACT_ID}`);

  await page.reload();

  await expect(
    page.getByRole("textbox", { name: "输入聊天消息" }),
  ).toBeVisible();
  await expect(page.getByTestId("artifact-sidebar")).toBeVisible();
  await expect(page.getByTestId("artifact-text")).toContainText("报告正文");

  expectNoUnmockedApi(unexpected);
});

test("活动摘要卡显示中文事实", async ({ page }) => {
  const unexpected = await setupPage(page);
  await openChat(page);

  const summary = page.getByTestId("activity-summary");

  await expect(summary).toBeVisible();
  await expect(summary).toContainText("2 次模型调用");
  await expect(summary).toContainText("耗时 1.5 秒");

  expectNoUnmockedApi(unexpected);
});

test("390×844 窄屏没有横向溢出", async ({ page }) => {
  const unexpected = await setupPage(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await openChat(page);

  await page.getByRole("button", { name: /打开产物 研究报告.md/ }).click();
  await expect(page.getByTestId("artifact-sidebar")).toBeVisible();

  const fits = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );

  expect(fits, "窄屏出现横向溢出").toBe(true);

  expectNoUnmockedApi(unexpected);
});

test("没有产物时不渲染产物条", async ({ page }) => {
  const unexpected = await setupPage(page, []);
  await openChat(page);

  await expect(page.getByTestId("artifact-strip")).toHaveCount(0);

  expectNoUnmockedApi(unexpected);
});
