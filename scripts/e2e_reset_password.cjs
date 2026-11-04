// 端到端验证「忘记密码」：真实 Chromium 点按钮、填表单、重置、再登录。
const { chromium } = require('playwright')

const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8123'
const USER = 'seele'
const OLD = 'seeledeepseek2026'
const NEW = 'brandnewpass123'

async function main() {
  const boot = await fetch(`${BASE}/api/auth/bootstrap`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: USER, password: OLD }),
  })
  console.log('bootstrap:', boot.status)

  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })

  try {
    await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
    await page.getByText('欢迎回来').waitFor({ timeout: 10000 })
    console.log('login page: ok')

    await page.getByRole('button', { name: '忘记密码？' }).click()
    await page.getByText('reset password', { exact: false }).waitFor({ timeout: 5000 }).catch(() => {})
    await page.getByText('重置密码').first().waitFor({ timeout: 5000 })
    console.log('reset page: ok')

    await page.locator('#pa-login-username').fill(USER)
    await page.locator('#pa-login-password').fill(NEW)
    await page.locator('#pa-login-password-confirm').fill(NEW)
    await page.getByRole('button', { name: '设置新密码 →' }).click()

    await page.getByText('密码已重置，请用新密码登录').waitFor({ timeout: 10000 })
    console.log('reset submit: ok')

    await page.locator('#pa-login-password').fill(NEW)
    await page.getByRole('button', { name: '登录工作空间 →' }).click()
    await page.waitForURL(`${BASE}/`, { timeout: 15000 })
    console.log('login with new password: ok ->', page.url())

    await page.screenshot({ path: process.env.E2E_SHOT || 'e2e-after-login.png' })
    console.log('E2E PASS')
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error('E2E FAIL:', error.message)
  process.exit(1)
})
