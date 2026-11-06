import { test, expect, type Page } from "@playwright/test";

import {
  blockUnmockedApi,
  expectNoUnmockedApi,
  installAuthMocks,
  TEST_TOKEN,
  type MockUser,
} from "./support/mockAuth";

/**
 * 平台控制台（只读版）：总览 / 节点 / 沙箱 / 执行 / 弹性。
 *
 * 后端接口全部 mock：这里验证的是渲染与降级行为，不验证数据口径
 * （口径由后端 API 断言覆盖）。
 */

const TEST_TOKEN_VALUE = TEST_TOKEN;

const OWNER: MockUser = {
  id: "playwright-console-owner",
  username: "playwright",
  role: "owner",
  active: true,
};

const SNAPSHOT_API = /\/api\/console\/snapshot/;

type Envelope<T> = { data: T | null; error: { code: string; message: string } | null };

function ok<T>(data: T): Envelope<T> {
  return { data, error: null };
}

function fail(code: string, message: string): Envelope<null> {
  return { data: null, error: { code, message } };
}

const CAPACITY = {
  nodes_total: 3,
  nodes_accepting: 2,
  nodes_cordoned: 1,
  nodes_draining: 0,
  nodes_expired: 0,
  slots_accepting: 16,
  unit: "nodes",
};

const POLICY = {
  min_nodes: 1,
  max_nodes: 4,
  capacity_per_node: 8,
  headroom: 0.2,
  scale_up_after: 2,
  scale_down_cooldown_seconds: 60,
};

const EVENT = {
  id: "evt-1",
  at: "2026-11-06T13:00:00+00:00",
  action: "scale_up",
  reason: "load_above_capacity",
  before_capacity: 2,
  after_capacity: 3,
  running: 9,
  queued: 1,
  capacity_unit: "nodes",
  source: "live",
  label: "scale_out",
  detail: "capacity",
  title: "扩容",
};

const DECISION = {
  at: "2026-11-06T13:00:05+00:00",
  action: "scale_up",
  reason: "load_above_capacity",
  label: "scale_out",
  detail: "capacity",
  title: "扩容",
  desired_capacity: 3,
  actual_capacity: 2,
  actual_capacity_after: 3,
  running: 9,
  queued: 1,
  applied: true,
  blocked: null,
  controller_mode: "apply",
  event_id: "evt-1",
};

function buildSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-11-06T13:00:10+00:00",
    series_window: "15m",
    sample_count: null,
    data_source: "live",
    is_stale: false,
    stale_reason: null,
    viewer: "playwright",
    queue: ok({
      active: 3,
      queued: 2,
      max_active: 8,
      max_queued: 64,
      max_wait_seconds: 120,
    }),
    nodes: ok({
      summary: CAPACITY,
      rows: [
        {
          node_id: "node-alpha",
          state: "ready",
          capacity: 8,
          cordoned: false,
          draining: false,
          accepting: true,
          expired: false,
          lease_expires_at: "2026-11-06T13:01:00+00:00",
          seconds_since_heartbeat: 4,
          active_sandboxes: 2,
        },
      ],
    }),
    sandboxes: ok({
      counts: { running: 2, idle: 1 },
      active_total: 3,
      rows: [
        {
          id: "sbx-1",
          node_id: "node-alpha",
          tier: "tier1",
          state: "running",
          created_at: "2026-11-06T12:58:00+00:00",
          expires_at: "2026-11-06T13:08:00+00:00",
          reclaimed_at: null,
          reclaim_reason: null,
        },
      ],
    }),
    executions: ok({
      recent: [
        {
          id: "exec-1",
          sandbox_id: "sbx-1",
          state: "succeeded",
          exit_code: 0,
          created_at: "2026-11-06T12:59:00+00:00",
          started_at: "2026-11-06T12:59:01+00:00",
          finished_at: "2026-11-06T12:59:03+00:00",
          error_code: null,
        },
      ],
      states: { succeeded: 3, failed: 1 },
      window: {
        window_seconds: 900,
        samples: 25,
        terminal: { succeeded: 9, failed: 1, cancelled: 2, timed_out: 0 },
        success_rate: {
          value: 0.9,
          numerator: 9,
          denominator: 10,
          excluded_cancelled: 2,
          note: "取消不计入分母，单独展示",
        },
        queue_wait_ms: {
          p50: 120,
          p99: 900,
          samples: 25,
          insufficient_samples: false,
        },
        exec_ms: { p50: 1500, p99: 4200, samples: 25, insufficient_samples: false },
      },
    }),
    elastic: ok({
      controller_mode: "apply",
      controller_running: true,
      tick_interval_seconds: 30,
      desired_capacity: 3,
      actual_capacity: 2,
      capacity_unit: "nodes",
      capacity: CAPACITY,
      policy: POLICY,
      inputs: { running: 9, queued: 1 },
      last_decision: DECISION,
      stabilization: {
        required_samples: 2,
        satisfied_count: null,
        satisfied: true,
        note: "按连续采样次数计，不是秒；未运行控制器时无进度",
      },
      cooldown: { seconds: 60, active: false, note: "冷却期只阻止缩容" },
      events: [EVENT],
      event_count: 1,
      message: null,
    }),
    trends: ok({
      window_seconds: 900,
      interval_seconds: 30,
      sample_count: 3,
      source: "ring_buffer",
      persisted: false,
      note: "单进程采样，进程重启即丢；不构成完整历史监控",
      series: [
        { at: "2026-11-06T12:59:30+00:00", running: 1, queued: 0, capacity: 2, desired: 1, action: "hold" },
        { at: "2026-11-06T13:00:00+00:00", running: 6, queued: 3, capacity: 2, desired: 3, action: "hold" },
        { at: "2026-11-06T13:00:30+00:00", running: 9, queued: 1, capacity: 3, desired: 3, action: "scale_out" },
      ],
      exec_buckets: [
        {
          at: "2026-11-06T13:00:00+00:00",
          succeeded: 9,
          failed: 1,
          cancelled: 2,
          timed_out: 0,
          total: 12,
        },
      ],
    }),
    ...overrides,
  };
}

async function openConsole(
  page: Page,
  payload: Record<string, unknown> = buildSnapshot(),
) {
  const unexpected = await blockUnmockedApi(page);

  await installAuthMocks(page, TEST_TOKEN_VALUE, OWNER);

  await page.route(SNAPSHOT_API, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    }),
  );

  await page.goto("/login");
  await page.locator("#pa-login-username").fill(OWNER.username);
  await page.locator("#pa-login-password").fill("playwright-password");
  await page.getByRole("button", { name: /登录工作空间/ }).click();

  await expect(
    page.getByRole("textbox", { name: "输入聊天消息" }),
  ).toBeVisible();

  await page.getByRole("button", { name: "平台" }).click();
  await expect(
    page.getByRole("heading", { name: "平台控制台" }),
  ).toBeVisible();

  return unexpected;
}

test("总览展示容量、控制器模式与最近决策", async ({ page }) => {
  const unexpected = await openConsole(page);

  await expect(page.getByText("实际容量", { exact: true })).toBeVisible();
  await expect(page.getByText("期望容量", { exact: true })).toBeVisible();
  await expect(page.getByText("真实驱动", { exact: true })).toBeVisible();

  // 决策：动作 + 原因 + 容量变化（事件表里也有同样文案，取第一处）
  await expect(page.getByText("load_above_capacity").first()).toBeVisible();
  await expect(page.getByText(/2 → 3/).first()).toBeVisible();

  // 事件与趋势块都在
  await expect(page.getByRole("heading", { name: "扩缩容事件" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "趋势" })).toBeVisible();

  // 趋势：窗口、样本量与真实折线
  await expect(page.getByText(/采样间隔/)).toBeVisible();
  await expect(page.getByText(/样本 3/)).toBeVisible();
  await expect(
    page.getByRole("img", { name: "队列与容量趋势" }),
  ).toBeVisible();
  await expect(page.getByText(/窗口内执行/)).toBeVisible();

  expectNoUnmockedApi(unexpected);
});

test("五个分区可以切换", async ({ page }) => {
  const unexpected = await openConsole(page);

  await page.getByRole("button", { name: "节点", exact: true }).click();
  await expect(page.getByRole("heading", { name: "节点" })).toBeVisible();
  await expect(page.getByText("node-alpha")).toBeVisible();

  await page.getByRole("button", { name: "沙箱", exact: true }).click();
  await expect(page.getByRole("heading", { name: "沙箱" })).toBeVisible();
  await expect(page.getByText("sbx-1")).toBeVisible();

  await page.getByRole("button", { name: "执行", exact: true }).click();
  await expect(page.getByText("队列等待 p50 / p99")).toBeVisible();
  await expect(page.getByText("exec-1")).toBeVisible();

  await page.getByRole("button", { name: "弹性", exact: true }).click();
  await expect(page.getByRole("heading", { name: "控制器" })).toBeVisible();
  await expect(page.getByText("每节点容量")).toBeVisible();
  await expect(page.getByText("load_above_capacity")).toBeVisible();

  expectNoUnmockedApi(unexpected);
});

test("单块失败只在所属分区显示错误", async ({ page }) => {
  const payload = buildSnapshot({
    nodes: fail("BLOCK_FAILED", "nodes: boom"),
  });

  const unexpected = await openConsole(page, payload);

  // 总览不受影响
  await expect(page.getByText("实际容量", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "节点", exact: true }).click();

  await expect(page.getByText(/区块加载失败/)).toBeVisible();
  await expect(page.getByText(/nodes: boom/)).toBeVisible();

  expectNoUnmockedApi(unexpected);
});

test("数据陈旧时给出提示", async ({ page }) => {
  const payload = buildSnapshot({
    is_stale: true,
    stale_reason: "last_tick_300s_ago",
  });

  const unexpected = await openConsole(page, payload);

  await expect(page.getByText(/数据陈旧/)).toBeVisible();
  await expect(page.getByText(/last_tick_300s_ago/)).toBeVisible();

  expectNoUnmockedApi(unexpected);
});

test("移动端没有页面级横向溢出", async ({ page }) => {
  // 侧栏在 ≤700px 是 display:none（全局既有行为），所以先在桌面宽度进入控制台，
  // 再缩到手机宽度测内容溢出。
  const unexpected = await openConsole(page);

  await page.setViewportSize({ width: 390, height: 844 });

  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );

  expect(overflow).toBeLessThanOrEqual(1);

  await expect(page.getByText("实际容量", { exact: true })).toBeVisible();

  // 窄屏下表格允许自身横向滚动，但不能撑破页面
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const bottomOverflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );

  expect(bottomOverflow).toBeLessThanOrEqual(1);

  expectNoUnmockedApi(unexpected);
});
