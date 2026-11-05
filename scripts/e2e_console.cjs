// 控制台前端验收：登录 → 点「平台」→ 四个区块可见。
const { chromium } = require('playwright')

const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8130'
const USER = 'seele'
const PASS = 'console-demo-2026'

async function main() {
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

  try {
    await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
    await page.locator('#pa-login-username').fill(USER)
    await page.locator('#pa-login-password').fill(PASS)
    await page.getByRole('button', { name: /登录工作空间/ }).click()
    await page.waitForURL(`${BASE}/`, { timeout: 20000 })

    await page.getByRole('button', { name: '平台' }).click()

    await page.getByText('平台控制台').first().waitFor({ timeout: 10000 })

    for (const section of ['队列', '最近路由决策', '节点', '最近执行']) {
      await page.getByText(section, { exact: false }).first().waitFor({ timeout: 10000 })
    }

    await page.waitForTimeout(1500)
    await page.screenshot({ path: process.env.E2E_SHOT || 'console.png' })
    console.log('CONSOLE UI OK')
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error('CONSOLE UI FAIL:', error.message)
  process.exit(1)
})
