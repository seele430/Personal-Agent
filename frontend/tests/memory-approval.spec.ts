import { test, expect, type Page } from "@playwright/test";

const USERNAME = "owner";
const PASSWORD = "test-password-123";

async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.locator("#pa-login-username").fill(USERNAME);
  await page.locator("#pa-login-password").fill(PASSWORD);
  await page.getByRole("button", { name: /登录工作空间/ }).click();
  await expect(page.getByRole("textbox", { name: "输入聊天消息" })).toBeVisible();
}

async function memoryTexts(page: Page, query: string): Promise<string[]> {
  return page.evaluate(async (search) => {
    const token = window.sessionStorage.getItem("personal-agent-auth-token");
    const response = await fetch(`/api/memory/items?query=${encodeURIComponent(search)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });

    if (!response.ok) return [];

    const data = await response.json();
    return (data.items ?? []).map((item: { text: string }) => item.text);
  }, query);
}

async function send(page: Page, text: string): Promise<void> {
  const input = page.getByRole("textbox", { name: "输入聊天消息" });
  await input.fill(text);
  await input.press("Enter");
}

test.describe("记忆审批浏览器闭环", () => {
  test("批准后恰好写入一条", async ({ page }) => {
    await login(page);
    await send(page, "记住：12 月 1 日交项目");

    const dialog = page.getByRole("alertdialog", { name: "工具审批" });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("memory_remember");
    expect(await memoryTexts(page, "12 月 1 日")).toEqual([]);

    await page.getByRole("button", { name: "批准执行" }).click();

    await expect
      .poll(async () => (await memoryTexts(page, "12 月 1 日")).length, { timeout: 20000 })
      .toBe(1);
  });

  test("拒绝后零写入", async ({ page }) => {
    await login(page);
    await send(page, "记住：拒绝写入的测试内容");

    const dialog = page.getByRole("alertdialog", { name: "工具审批" });
    await expect(dialog).toBeVisible();
    await page.getByRole("button", { name: "拒绝" }).click();

    await page.waitForTimeout(1000);
    expect(await memoryTexts(page, "拒绝写入的测试内容")).toEqual([]);
  });
});
