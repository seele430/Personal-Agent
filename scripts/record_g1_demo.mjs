import { createRequire } from "node:module";

const require = createRequire(
  new URL("../frontend/package.json", import.meta.url),
);
const { chromium } = require("@playwright/test");

const baseURL = process.env.G1_FRONTEND_URL;
const username = process.env.G1_DEMO_USERNAME;
const password = process.env.G1_DEMO_PASSWORD;
const videoDir = process.env.G1_VIDEO_DIR;

if (!baseURL || !username || !password || !videoDir) {
  throw new Error("缺少录屏所需的环境配置");
}

const question =
  "请使用工具创建一个 30 分钟后的提醒，内容是“喝水”。创建后简短确认。";
const browser = await chromium.launch({ headless: true });
let context;

try {
  context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    recordVideo: {
      dir: videoDir,
      size: { width: 1440, height: 900 },
    },
  });
  const page = await context.newPage();
  const startedAt = Date.now();

  await page.goto(`${baseURL}/login`, { waitUntil: "domcontentloaded" });
  await page.getByLabel("用户名", { exact: true }).waitFor();
  await page.waitForTimeout(2000);
  await page.getByLabel("用户名", { exact: true }).pressSequentially(
    username,
    { delay: 45 },
  );
  await page.getByLabel("密码", { exact: true }).pressSequentially(
    password,
    { delay: 30 },
  );
  await page.waitForTimeout(1000);
  await page.getByRole("button", { name: /登录工作空间/ }).click();

  const input = page.getByRole("textbox", { name: "输入聊天消息" });
  await input.waitFor({ timeout: 20000 });
  await page.waitForTimeout(4000);

  await page.evaluate(() => {
    window.__g1ToolStates = [];
    const collect = () => {
      document.querySelectorAll(".loading span").forEach((node) => {
        const text = node.textContent ?? "";
        if (
          text.includes("工具") &&
          !window.__g1ToolStates.includes(text)
        ) {
          window.__g1ToolStates.push(text);
        }
      });
    };
    new MutationObserver(collect).observe(document.body, {
      subtree: true,
      childList: true,
      characterData: true,
    });
  });

  await input.pressSequentially(question, { delay: 30 });
  await page.waitForTimeout(1200);
  await page.getByRole("button", { name: "发送 ↑" }).click();

  const assistant = page.locator("article.message.assistant").last();
  await assistant.waitFor({ state: "visible", timeout: 60000 });
  await page.locator(".loading").waitFor({ state: "hidden", timeout: 60000 });
  const answer = (
    await assistant.locator(".message-content").innerText()
  ).trim();
  if (!answer) throw new Error("最终回答为空");

  const toolStatuses = await page.evaluate(() => window.__g1ToolStates);
  if (!toolStatuses.some((text) =>
    text.includes("create_reminder") && text.includes("已完成")
  )) {
    throw new Error("录屏期间未清楚显示 create_reminder 完成状态");
  }
  if (await page.locator(".error-box").count()) {
    throw new Error(`页面出现错误：${await page.locator(".error-box").innerText()}`);
  }

  await page.waitForTimeout(5000);
  await page.getByRole("button", { name: "提醒", exact: true }).click();
  const reminderPanel = page.locator(".reminder-panel");
  await reminderPanel.getByText("喝水", { exact: false }).first().waitFor({
    timeout: 10000,
  });
  await page.waitForTimeout(8000);

  await page.getByRole("button", { name: /对话/ }).click();
  await assistant.waitFor({ state: "visible" });
  const remaining = 60500 - (Date.now() - startedAt);
  if (remaining > 0) await page.waitForTimeout(remaining);

  const video = page.video();
  if (!video) throw new Error("浏览器没有生成视频");
  const result = {
    real_browser_flow: true,
    login_success: true,
    question,
    answer,
    tool_statuses: toolStatuses,
    reminder_visible: true,
    recorded_elapsed_ms: Date.now() - startedAt,
    temporary_video_path: await video.path(),
  };
  await context.close();
  context = undefined;
  process.stdout.write(JSON.stringify(result));
} finally {
  if (context) await context.close();
  await browser.close();
}
