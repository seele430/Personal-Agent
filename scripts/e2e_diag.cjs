// 诊断：打开 /login，打印控制台错误与页面文本（用于排查前端为什么没渲染）。
const { chromium } = require('playwright')

const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8123'

async function main() {
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })

  page.on('console', (msg) => console.log('CONSOLE', msg.type(), msg.text()))
  page.on('pageerror', (error) => console.log('PAGEERROR', error.message))

  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2000)

  console.log('URL:', page.url())
  console.log('TEXT:', (await page.innerText('body')).slice(0, 400))
  await page.screenshot({ path: process.env.E2E_SHOT || 'diag.png' })

  await browser.close()
}

main().catch((error) => {
  console.error('DIAG FAIL:', error.message)
  process.exit(1)
})
