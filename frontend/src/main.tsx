import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import './index.css'
import AuthPreview from './AuthPreview.tsx'

// 页面入口不直接决定用户是否已登录。
// 由 AuthPreview 向后端验证会话，再决定显示哪个页面。

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthPreview />
  </StrictMode>,
)