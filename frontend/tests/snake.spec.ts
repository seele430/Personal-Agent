import { test, expect, type Page } from "@playwright/test";

/**
 * 贪吃蛇产物链路：可加载、可控方向、500 tick 不炸、能计分、能结束能重开。
 *
 * 用 `window.__snake.tick(n)` 手动步进，不依赖 requestAnimationFrame，
 * 这样 headless 下的帧节流不会让测试变成"有时候过有时候不过"。
 */

type SnakeState = {
  score: number;
  alive: boolean;
  paused: boolean;
  length: number;
  ticks: number;
  head: { x: number; y: number };
  food: { x: number; y: number };
};

async function openGame(page: Page): Promise<void> {
  await page.goto("/games/snake.html");
  await expect(page.locator("canvas#board")).toBeVisible();

  // 关掉自动计时：测试用 tick() 手动驱动，避免真实帧和手动步进互相抢
  await page.evaluate(() => {
    (window as unknown as { __snake: { setAuto: (v: boolean) => void } })
      .__snake.setAuto(false);
  });
}

async function state(page: Page): Promise<SnakeState> {
  return page.evaluate(() => (window as unknown as {
    __snake: { state: () => SnakeState };
  }).__snake.state());
}

async function tick(page: Page, n: number): Promise<void> {
  await page.evaluate((count) => {
    (window as unknown as { __snake: { tick: (n: number) => void } }).__snake.tick(count);
  }, n);
}

async function errors(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as {
    __snake: { errors: () => string[] };
  }).__snake.errors());
}

test("页面可加载且初始状态正确", async ({ page }) => {
  await openGame(page);

  const initial = await state(page);

  expect(initial.alive).toBe(true);
  expect(initial.score).toBe(0);
  expect(initial.length).toBe(3);
  expect(await errors(page)).toEqual([]);
});

test("方向键能改变行进方向", async ({ page }) => {
  await openGame(page);

  const before = await state(page);

  await page.keyboard.press("ArrowUp");
  await tick(page, 1);

  const after = await state(page);

  expect(after.head.y).toBe(before.head.y - 1);
  expect(after.head.x).toBe(before.head.x);
});

test("吃到一个食物会加分", async ({ page }) => {
  await openGame(page);

  // 初始蛇头 (10,10) 向右，食物在 (14,10)：走 4 步正好吃到
  await tick(page, 4);

  const after = await state(page);

  expect(after.score).toBe(1);
  expect(after.length).toBe(4);
});

test("500 tick 不抛异常", async ({ page }) => {
  await openGame(page);

  await tick(page, 500);

  const after = await state(page);

  expect(await errors(page)).toEqual([]);
  expect(typeof after.score).toBe("number");
  expect(after.ticks).toBeGreaterThan(0);
});

test("撞墙会结束并给出重新开始", async ({ page }) => {
  await openGame(page);

  await tick(page, 60);

  const after = await state(page);
  expect(after.alive).toBe(false);

  await expect(page.locator("#overlay")).toHaveClass(/show/);

  await page.getByRole("button", { name: "重新开始" }).click();

  const restarted = await state(page);
  expect(restarted.alive).toBe(true);
  expect(restarted.score).toBe(0);
});

test("390×844 窄屏没有横向溢出", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openGame(page);

  const fits = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );

  expect(fits, "贪吃蛇页面在窄屏横向溢出").toBe(true);
});
