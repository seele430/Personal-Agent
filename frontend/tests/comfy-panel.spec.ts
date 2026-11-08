// 图生图面板：打开、成功、503 显存不足、steps 上限。

import { expect, test, type Page } from "@playwright/test";

import {
  TEST_TOKEN,
  TEST_USER,
  installAuthMocks,
} from "./support/mockAuth";

const API = "**/api/tools/comfy/img2img";

async function openChat(page: Page): Promise<void> {
  await page.goto("/login");

  await page.locator("#pa-login-username").fill("playwright");
  await page.locator("#pa-login-password").fill("PlaywrightTestPassword2026!");
  await page.getByRole("button", { name: /登录工作空间/ }).click();

  await expect(
    page.getByRole("textbox", { name: "输入聊天消息" }),
  ).toBeVisible();
}

test.describe("图生图面板", () => {
  test.beforeEach(async ({ page }) => {
    await installAuthMocks(page, TEST_TOKEN, TEST_USER);
    await openChat(page);
  });

  test("打开面板并看到输入目录说明", async ({ page }) => {
    await page.getByRole("button", { name: "图生图" }).click();

    const panel = page.getByTestId("comfy-panel");
    await expect(panel).toBeVisible();
    await expect(panel.getByText("input/")).toBeVisible();
  });

  test("提交后展示生成的文件名", async ({ page }) => {
    await page.route(API, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          success: true,
          prompt_id: "p1",
          images: ["personal_agent_00001_.png"],
          path: "personal_agent_00001_.png",
        }),
      }),
    );

    await page.getByRole("button", { name: "图生图" }).click();
    await page.getByLabel("图生图提示词").fill("a cat on a chair");
    await page.getByRole("button", { name: "开始生成" }).click();

    await expect(page.getByTestId("comfy-result")).toContainText(
      "personal_agent_00001_.png",
    );
  });

  test("503 显存不足原样显示", async ({ page }) => {
    await page.route(API, (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "显存不足：空闲 1200 MB < 2500 MB，先关掉游戏/浏览器",
        }),
      }),
    );

    await page.getByRole("button", { name: "图生图" }).click();
    await page.getByLabel("图生图提示词").fill("a cat");
    await page.getByRole("button", { name: "开始生成" }).click();

    await expect(page.getByText(/显存不足/)).toBeVisible();
    await expect(page.getByTestId("comfy-result")).toHaveCount(0);
  });

  test("steps 超过 30 时按钮禁用且不提示可生成", async ({ page }) => {
    await page.getByRole("button", { name: "图生图" }).click();
    await page.getByLabel("图生图提示词").fill("a cat");
    await page.getByLabel("采样步数").fill("31");

    await expect(page.getByText("步数必须在 1–30")).toBeVisible();
    await expect(page.getByRole("button", { name: "开始生成" })).toBeDisabled();
  });
});
