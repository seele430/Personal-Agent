
import {
  useEffect,
  useRef,
  useState,
} from 'react'

import type {
  FormEvent,
} from 'react'

import App from './App.tsx'
import './AuthPreview.css'


type AuthUser = {
  id: string
  username: string
  role: string
  active: boolean
}

type AuthSession = {
  token: string
  user: AuthUser
}


const LOGIN_URL = '/api/auth/login'
const LOGOUT_URL = '/api/auth/logout'

const REQUEST_TIMEOUT_MS = 20000


function isAuthSession(value: unknown): value is AuthSession {
  if (
    typeof value !== 'object' ||
    value === null
  ) {
    return false
  }

  const data = value as Record<string, unknown>

  if (
    typeof data.token !== 'string' ||
    data.token.length === 0 ||
    typeof data.user !== 'object' ||
    data.user === null
  ) {
    return false
  }

  const user = data.user as Record<string, unknown>

  return (
    typeof user.id === 'string' &&
    typeof user.username === 'string' &&
    typeof user.role === 'string' &&
    user.active === true
  )
}


function roleName(role: string): string {
  switch (role) {
    case 'owner':
      return '站长'
    case 'admin':
      return '管理员'
    case 'trusted':
      return '受信用户'
    case 'member':
      return '普通用户'
    default:
      return '用户'
  }
}


export default function AuthPreview() {
  const [session, setSession] =
    useState<AuthSession | null>(null)

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)

  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')

  const [loggingOut, setLoggingOut] = useState(false)
  const [logoutError, setLogoutError] = useState('')

  // 同步锁：阻止重复提交
  const loginLocked = useRef(false)
  const logoutLocked = useRef(false)

  const requestRef = useRef<AbortController | null>(null)

  // 页面卸载时取消仍在等待的登录请求
  useEffect(() => {
    return () => {
      requestRef.current?.abort()
    }
  }, [])


  async function handleLogin(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault()

    if (loginLocked.current) {
      return
    }

    const cleanedUsername = username.trim().toLowerCase()

    if (!cleanedUsername) {
      setError('请输入用户名')
      return
    }

    if (!password) {
      setError('请输入密码')
      return
    }

    loginLocked.current = true
    setSending(true)
    setError('')

    const controller = new AbortController()
    requestRef.current = controller

    const timeout = window.setTimeout(() => {
      controller.abort()
    }, REQUEST_TIMEOUT_MS)

    try {
      const response = await fetch(LOGIN_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        credentials: 'omit',
        signal: controller.signal,
        body: JSON.stringify({
          username: cleanedUsername,
          password,
        }),
      })

      if (!response.ok) {
        if (response.status === 401) {
          setError('用户名或密码不正确')
        } else if (response.status === 429) {
          setError('尝试次数过多，请稍后再试')
        } else if (response.status >= 500) {
          setError('服务暂时不可用，请稍后重试')
        } else {
          setError('登录失败，请检查输入后重试')
        }

        return
      }

      const result: unknown = await response.json()

      if (!isAuthSession(result)) {
        setError('服务器返回的数据格式不正确')
        return
      }

      // 清除密码，不保存至浏览器持久存储
      setPassword('')

      // 只在内存里保存当前登录状态
      setSession(result)

    } catch (reason) {
      if (
        reason instanceof Error &&
        reason.name === 'AbortError'
      ) {
        setError('登录请求超时，请检查后端服务')
      } else {
        setError('无法连接服务器，请检查网络和后端')
      }

    } finally {
      window.clearTimeout(timeout)
      requestRef.current = null
      loginLocked.current = false
      setSending(false)
    }
  }


  async function handleLogout() {
    if (!session || logoutLocked.current) {
      return
    }

    logoutLocked.current = true
    setLoggingOut(true)
    setLogoutError('')

    const controller = new AbortController()

    const timeout = window.setTimeout(() => {
      controller.abort()
    }, REQUEST_TIMEOUT_MS)

    try {
      const response = await fetch(LOGOUT_URL, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${session.token}`,
          Accept: 'application/json',
        },
        credentials: 'omit',
        signal: controller.signal,
      })

      if (response.ok || response.status === 401) {
        setSession(null)
        setPassword('')
        setError('')
        return
      }

      setLogoutError('服务器未确认退出，请重试')

    } catch {
      setLogoutError(
        '网络异常，无法确认服务端会话已撤销',
      )

    } finally {
      window.clearTimeout(timeout)
      logoutLocked.current = false
      setLoggingOut(false)
    }
  }


  // ====================================
  // 已登录：显示已有 AI 工作空间
  // ====================================

  if (session) {
    return (
      <>
        <App />

        <div className="pa-auth-session-panel">
          <span className="pa-auth-session-user">
            {session.user.username}
            {' · '}
            {roleName(session.user.role)}
          </span>

          <button
            type="button"
            className="pa-auth-logout-button"
            disabled={loggingOut}
            onClick={handleLogout}
          >
            {loggingOut ? '退出中…' : '退出登录'}
          </button>

          {logoutError && (
            <span
              className="pa-auth-logout-error"
              role="alert"
            >
              {logoutError}
            </span>
          )}
        </div>
      </>
    )
  }


  // ====================================
  // 未登录：显示登录页面
  // ====================================

  return (
    <main className="pa-auth-page">

      <section className="pa-auth-layout">

        <aside className="pa-auth-hero">

          <div className="pa-auth-brand">
            <div className="pa-auth-brand-icon">
              ✳
            </div>

            <div>
              <strong>Personal Agent</strong>
              <span>AI Workspace</span>
            </div>
          </div>

          <div className="pa-auth-hero-content">
            <div className="pa-auth-hero-label">
              YOUR PERSONAL AI SPACE
            </div>

            <h1>
              让灵感，
              <br />
              从这里开始。
            </h1>

            <p>
              一个可靠、私密、持续成长的
              <br />
              个人 AI 工作空间。
            </p>
          </div>

          <div className="pa-auth-hero-footer">
            Personal Agent · Development Preview
          </div>

          <div
            className="pa-auth-decoration pa-auth-decoration-one"
            aria-hidden="true"
          />

          <div
            className="pa-auth-decoration pa-auth-decoration-two"
            aria-hidden="true"
          />
        </aside>


        <section className="pa-auth-form-side">

          <div className="pa-auth-form-container">

            <div className="pa-auth-mobile-brand">
              ✳ Personal Agent
            </div>

            <div className="pa-auth-eyebrow">
              WELCOME BACK
            </div>

            <h2>欢迎回来</h2>

            <p className="pa-auth-subtitle">
              登录你的 Personal Agent 工作空间
            </p>

            <form
              className="pa-auth-form"
              onSubmit={handleLogin}
              noValidate
            >

              <label htmlFor="pa-login-username">
                用户名
              </label>

              <input
                id="pa-login-username"
                name="username"
                type="text"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={64}
                placeholder="请输入用户名"
                value={username}
                disabled={sending}
                onChange={(event) => {
                  setUsername(event.target.value)
                  setError('')
                }}
              />

              <div className="pa-auth-password-label">
                <label htmlFor="pa-login-password">
                  密码
                </label>
              </div>

              <div className="pa-auth-password-field">
                <input
                  id="pa-login-password"
                  name="password"
                  type={
                    showPassword
                      ? 'text'
                      : 'password'
                  }
                  autoComplete="current-password"
                  spellCheck={false}
                  autoCapitalize="none"
                  placeholder="请输入密码"
                  value={password}
                  disabled={sending}
                  onChange={(event) => {
                    setPassword(event.target.value)
                    setError('')
                  }}
                />

                <button
                  type="button"
                  className="pa-auth-password-toggle"
                  aria-label={
                    showPassword
                      ? '隐藏密码'
                      : '显示密码'
                  }
                  aria-pressed={showPassword}
                  disabled={sending}
                  onClick={() => {
                    setShowPassword(!showPassword)
                  }}
                >
                  {showPassword ? '隐藏' : '显示'}
                </button>
              </div>

              <p className="pa-auth-password-help">
                密码默认隐藏。请留意中文输入法的候选窗口。
              </p>

              {error && (
                <div
                  className="pa-auth-error"
                  role="alert"
                >
                  {error}
                </div>
              )}

              <button
                type="submit"
                className="pa-auth-submit"
                disabled={sending}
              >
                {sending ? (
                  <>
                    <span className="pa-auth-spinner" />
                    正在验证身份…
                  </>
                ) : (
                  '登录工作空间 →'
                )}
              </button>

            </form>

            <div className="pa-auth-note">
              <span aria-hidden="true">◇</span>
              邀请制访问 · 安全身份验证
            </div>

            <div className="pa-auth-preview-note">
              当前为本地登录预览。
              账号注册与邀请码功能将在后续开放。
            </div>

          </div>
        </section>

      </section>

    </main>
  )
}