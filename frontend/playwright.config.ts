
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",

  testMatch: "**/*.spec.ts",

  fullyParallel: false,

  workers: 1,

  retries: 0,

  timeout: 30000,

  expect: {
    timeout: 5000,
  },

  reporter: [["list"]],

  use: {
    ...devices["Desktop Chrome"],

    baseURL: "http://127.0.0.1:5173",

    headless: true,

    trace: "retain-on-failure",

    screenshot: "only-on-failure",
  },

  webServer: {
    command:
      "npm run dev -- --host 127.0.0.1 --port 5173 --strictPort",

    url: "http://127.0.0.1:5173",

    reuseExistingServer: true,

    timeout: 30000,
  },
});