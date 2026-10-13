import {
  useEffect,
  useRef,
  useState,
} from 'react'

import type { FormEvent } from 'react'

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
const ME_URL = '/api/auth/me'
const LOGOUT_URL = '/api/auth/logout'

const SESSION_KEY = 'personal-agent-auth-token'
const REQUEST_TIMEOUT_MS = 20000


function parseAuthUser(value: unknown): AuthUser | null {
  if (typeof value !== 'object' || value === null) {
    return null
  }

  const data = value as Record<string, unknown>

  const id =
    typeof data.id === 'string'
      ? data.id
      : data.user_id

  const active =
    data.active === true ||
    data.is_active === true

  if (
    typeof id !== 'string' ||
    !id ||
    typeof data.username !== 'string' ||
    !data.username ||
    typeof data.role !== 'string' ||
    !active
  ) {
    return null
  }

  return {
    id,
    username: data.username,
    role: data.role,
    active: true,
  }
}


function parseLoginSession(value: unknown): AuthSession | null {
  if (typeof value !== 'object' || value === null) {
    return null
  }

  const data = value as Record<string, unknown>

  if (
    typeof data.token !== 'string' ||
    !data.token
  ) {
    return null
  }

  const user = parseAuthUser(data.user)

  if (!user) {
    return null
  }

  return {
    token: data.token,
    user,
  }
}


function parseMeResponse(value: unknown): AuthUser | null {
  if (typeof value !== 'object' || value === null) {
    return null
  }

  const data = value as Record<string, unknown>

  // 兼容直接返回用户对象，或者 { user: ... }。
  return parseAuthUser(data.user ?? data)
}


function getSavedToken(): string | null {
  try {
    return window.sessionStorage.getItem(SESSION_KEY)
  } catch {
    return null
  }
}


function saveToken(token: string): boolean {
  try {
    window.sessionStorage.setItem(SESSION_KEY, token)
    return true
  } catch {
    return false
  }
}


function clearSavedToken(): void {
  try {
    window.sessionStorage.removeItem(SESSION_KEY)
  } catch {
    // 浏览器禁止存储时，不应导致页面崩溃。
  }
}


function replacePath(path: '/login' | '/'): void {
  if (window.location.pathname !== path) {
    window.history.replaceState(null, '', path)
  }
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

  const [checkingSession, setCheckingSession] =
    useState(true)

  const [restoreError, setRestoreError] = useState('')
  const [restoreAttempt, setRestoreAttempt] = useState(0)

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)

  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')

  const [loggingOut, setLoggingOut] = useState(false)
  const [logoutError, setLogoutError] = useState('')

  const loginLocked = useRef(false)
  const logoutLocked = useRef(false)

  const loginRequestRef =
    useRef<AbortController | null>(null)


  // ==========================================
  // 启动时恢复登录状态
  // ==========================================

  useEffect(() => {
    const token = getSavedToken()

    if (!token) {
      setSession(null)
      setCheckingSession(false)
      replacePath('/login')
      return
    }

    let active = true

    const controller = new AbortController()

    const timeout = window.setTimeout(() => {
      controller.abort()
    }, REQUEST_TIMEOUT_MS)

    // token 通过参数传入：函数声明会被提升，
    // TypeScript 不会把外层的非空判断带进函数体。
    async function restoreSession(savedToken: string) {
      try {
        const response = await fetch(ME_URL, {
          method: 'GET',
          headers: {
            Authorization: `Bearer ${savedToken}`,
            Accept: 'application/json',
          },
          credentials: 'omit',
          signal: controller.signal,
        })

        if (!active) {
          return
        }

        if (
          response.status === 401 ||
          response.status === 403
        ) {
          clearSavedToken()
          setSession(null)
          setRestoreError('')
          setError('登录已过期，请重新登录')
          replacePath('/login')
          return
        }

        if (!response.ok) {
          throw new Error('无法验证登录状态')
        }

        const result: unknown = await response.json()

        if (!active) {
          return
        }

        const user = parseMeResponse(result)

        if (!user) {
          throw new Error('身份接口返回的数据格式不正确')
        }

        setSession({
          token: savedToken,
          user,
        })

        setRestoreError('')
        setError('')

        replacePath('/')

      } catch (reason) {
        if (!active) {
          return
        }

        // 网络故障不等于令牌失效。
        // 不清除 token，也不允许进入工作空间。
        setSession(null)

        if (
          reason instanceof Error &&
          reason.name === 'AbortError'
        ) {
          setRestoreError(
            '验证登录状态超时，请检查后端服务。',
          )
        } else {
          setRestoreError(
            '暂时无法验证上次登录状态，请重试。',
          )
        }

        replacePath('/login')

      } finally {
        window.clearTimeout(timeout)

        if (active) {
          setCheckingSession(false)
        }
      }
    }

    void restoreSession(token)

    return () => {
      active = false
      controller.abort()
      window.clearTimeout(timeout)
    }
  }, [restoreAttempt])


  // 页面卸载时取消登录请求。
  useEffect(() => {
    return () => {
      loginRequestRef.current?.abort()
    }
  }, [])


  // ==========================================
  // 登录
  // ==========================================

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
    setRestoreError('')

    const controller = new AbortController()
    loginRequestRef.current = controller

    const timeout = window.setTimeout(() => {
      controller.abort()
    }, REQUEST_TIMEOUT_MS)

    try {
      const response = await fetch(LOGIN_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
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
      const validSession = parseLoginSession(result)

      if (!validSession) {
        setError('服务器返回的登录数据格式不正确')
        return
      }

      if (!saveToken(validSession.token)) {
        setError(
          '浏览器无法保存当前标签页的登录状态，请检查存储权限。',
        )
        return
      }

      setPassword('')
      setSession(validSession)
      setError('')

      // 登录成功后切换工作空间地址。
      replacePath('/')

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
      loginRequestRef.current = null
      loginLocked.current = false
      setSending(false)
    }
  }


  // ==========================================
  // 退出登录
  // ==========================================

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

      if (
        response.ok ||
        response.status === 401
      ) {
        clearSavedToken()

        setSession(null)
        setPassword('')
        setError('')
        setRestoreError('')

        replacePath('/login')
        return
      }

      setLogoutError('服务器未确认退出，请重试')

    } catch {
      setLogoutError(
        '网络异常，无法确认服务端会话已撤销，请重试。',
      )

    } finally {
      window.clearTimeout(timeout)
      logoutLocked.current = false
      setLoggingOut(false)
    }
  }


  // ==========================================
  // 正在验证已保存的会话
  // ==========================================

  if (checkingSession) {
    return (
      <main className="pa-auth-page">
        <div
          role="status"
          style={{
            margin: 'auto',
            padding: 32,
            textAlign: 'center',
          }}
        >
          正在验证登录状态，请稍候…
        </div>
      </main>
    )
  }


  // ==========================================
  // 已登录：显示原有聊天工作空间
  // ==========================================

  if (session) {
    return (
      <>
        <App
          key={session.user.id}
          authToken={session.token}
          userId={session.user.id}
        />

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


  // ==========================================
  // 未登录：显示原有登录页面
  // ==========================================

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


            {restoreError && (
              <div className="pa-auth-error" role="alert">
                <p>{restoreError}</p>

                <button
                  type="button"
                  onClick={() => {
                    setCheckingSession(true)
                    setRestoreError('')
                    setRestoreAttempt((value) => value + 1)
                  }}
                >
                  重新验证上次登录
                </button>
              </div>
            )}


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
                  type={showPassword ? 'text' : 'password'}
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
                    showPassword ? '隐藏密码' : '显示密码'
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
              本地开发版本。
              登录状态仅保存在当前浏览器标签页，
              刷新时会重新向服务器验证。
            </div>

          </div>
        </section>

      </section>
    </main>
  )
}