
import {
  test,
  expect,
} from "@playwright/test";

test.describe("Personal Agent Login Gate", () => {

  test(
    "直接访问根路径必须进入登录页面",
    async ({ page }) => {

      await page.goto("/");

      await expect(page).toHaveURL(
        /\/login(?:[?#]|$)/
      );

      await expect(
        page.getByRole("heading", {
          name: "欢迎回来",
        })
      ).toBeVisible();

      // 没有登录时，不能看到聊天输入框。
      await expect(
        page.getByRole("textbox", {
          name: "输入聊天消息",
        })
      ).toHaveCount(0);

      await expect(
        page.getByRole("button", {
          name: /登录工作空间/,
        })
      ).toBeVisible();
    }
  );

  test(
    "直接访问登录地址正常显示登录界面",
    async ({ page }) => {

      await page.goto("/login");

      await expect(
        page.locator("#pa-login-username")
      ).toBeVisible();

      await expect(
        page.locator("#pa-login-password")
      ).toBeVisible();

      await expect(
        page.getByRole("textbox", {
          name: "输入聊天消息",
        })
      ).toHaveCount(0);
    }
  );

});