// 阶段 5：登录 → 平台控制台 → 看到真实路由决策与执行记录。
const { chromium } = require('playwright')

const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8135'

async function main() {
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

  try {
    await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
    await page.locator('#pa-login-username').fill('seele')
    await page.locator('#pa-login-password').fill('stage5-accept-2026')
    await page.getByRole('button', { name: /登录工作空间/ }).click()
    await page.waitForURL(`${BASE}/`, { timeout: 20000 })

    await page.getByRole('button', { name: '平台' }).click()
    await page.getByText('平台控制台').first().waitFor({ timeout: 10000 })

    // 来自 demo_route_pipeline 的真实决策与执行。
    await page.getByText('data.analysis').first().waitFor({ timeout: 10000 })
    await page.getByText(/succeeded/).first().waitFor({ timeout: 10000 })

    await page.screenshot({ path: process.env.E2E_SHOT || 'stage5.png' })
    console.log('STAGE5 UI OK')
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error('STAGE5 UI FAIL:', error.message)
  process.exit(1)
})
