// 产物对比：选中另一个产物 → 展示文本差异；二进制给摘要。

import { expect, test, type Page } from "@playwright/test";

import {
  ARTIFACTS_API,
  TEST_TOKEN,
  TEST_USER,
  installAuthMocks,
} from "./support/mockAuth";

const CURRENT = {
  id: "art-current",
  title: "研究报告.md",
  kind: "markdown",
  media_type: "text/markdown",
  size_bytes: 120,
  sha256: "a".repeat(64),
  rel_path: "research.md",
  run_id: "",
  exec_id: "",
  created_at: "2026-11-08T00:00:00+00:00",
  previewable: true,
};

const OTHER = {
  ...CURRENT,
  id: "art-other",
  title: "研究报告-v2.md",
  sha256: "b".repeat(64),
};

const PREVIEW = {
  ...CURRENT,
  previewable: true,
  mode: "markdown",
  text: "# 报告\n\n第一版",
  truncated: false,
};

async function openChat(page: Page): Promise<void> {
  await page.goto("/login");

  await page.locator("#pa-login-username").fill("playwright");
  await page.locator("#pa-login-password").fill("PlaywrightTestPassword2026!");
  await page.getByRole("button", { name: /登录工作空间/ }).click();

  await expect(
    page.getByRole("textbox", { name: "输入聊天消息" }),
  ).toBeVisible();
}

test.describe("产物对比", () => {
  test.beforeEach(async ({ page }) => {
    await installAuthMocks(page, TEST_TOKEN, TEST_USER);

    await page.route(ARTIFACTS_API, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [CURRENT, OTHER] }),
      }),
    );

    await page.route("**/api/artifacts/*/preview", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(PREVIEW),
      }),
    );
  });

  async function openSidebar(page: Page): Promise<void> {
    await openChat(page);

    await page
      .getByRole("button", { name: /打开产物 研究报告\.md/ })
      .click();

    await expect(page.getByTestId("artifact-sidebar")).toBeVisible();
  }

  test("可以选择另一个产物并展示文本差异", async ({ page }) => {
    await page.route("**/api/artifacts/*/diff/*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          kind: "text",
          a_name: "research.md",
          b_name: "research-v2.md",
          added: 1,
          removed: 1,
          truncated: false,
          changed: true,
          a_size: 10,
          b_size: 12,
          lines: [
            { kind: "context", text: "# 报告" },
            { kind: "remove", text: "第一版" },
            { kind: "add", text: "第二版" },
          ],
        }),
      }),
    );

    await openSidebar(page);

    await page.getByLabel("选择对比对象").selectOption(OTHER.id);
    await page.getByRole("button", { name: "对比" }).click();

    const body = page.getByTestId("diff-body");
    await expect(body).toBeVisible();
    await expect(body).toContainText("第二版");
    await expect(body).toContainText("第一版");
  });

  test("二进制产物显示摘要而不是伪文本", async ({ page }) => {
    await page.route("**/api/artifacts/*/diff/*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          kind: "binary",
          a_name: "a.png",
          b_name: "b.png",
          added: 0,
          removed: 0,
          truncated: false,
          changed: true,
          a_size: 100,
          b_size: 200,
          lines: [],
        }),
      }),
    );

    await openSidebar(page);

    await page.getByLabel("选择对比对象").selectOption(OTHER.id);
    await page.getByRole("button", { name: "对比" }).click();

    await expect(page.getByTestId("diff-binary")).toContainText("内容已改变");
  });

  test("接口失败时显示错误", async ({ page }) => {
    await page.route("**/api/artifacts/*/diff/*", (route) =>
      route.fulfill({ status: 500, body: "boom" }),
    );

    await openSidebar(page);

    await page.getByLabel("选择对比对象").selectOption(OTHER.id);
    await page.getByRole("button", { name: "对比" }).click();

    await expect(page.getByText(/对比失败：HTTP 500/)).toBeVisible();
  });
});
