
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

type MessageStatus =
  | "pending"
  | "sent"
  | "unknown"
  | "rejected";

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: MessageStatus;
  requestId?: string;
  replyTo?: string;
};

type ChatResponse = {
  request_id?: string;
  status?: string;
  reply?: string | null;
  code?: string;
  detail?: string;
};

const CHAT_KEY = "personal-agent-dev-chat";
const DRAFT_KEY = "personal-agent-dev-draft";

type AppProps = {
  authToken?: string;
  userId?: string;
};

// 已登录用户使用独立的浏览器缓存。
function chatStorageKey(userId?: string): string {
  return userId
    ? `personal-agent-user-${userId}-chat`
    : CHAT_KEY;
}

function draftStorageKey(userId?: string): string {
  return userId
    ? `personal-agent-user-${userId}-draft`
    : DRAFT_KEY;
}

function saveMessages(
  messages: Message[],
  userId?: string
) {
  try {
    sessionStorage.setItem(
      chatStorageKey(userId),
      JSON.stringify(messages.slice(-100))
    );
  } catch {
    // 缓存失败不影响聊天
  }
}

function loadMessages(userId?: string): Message[] {
  try {
    const raw = sessionStorage.getItem(
      chatStorageKey(userId)
    );

    if (!raw) return [];

    const parsed: unknown = JSON.parse(raw);

    if (!Array.isArray(parsed)) return [];

    return parsed
      .filter(
        (item): item is Message =>
          typeof item === "object" &&
          item !== null &&
          typeof item.id === "string" &&
          typeof item.content === "string" &&
          (item.role === "user" ||
            item.role === "assistant") &&
          [
            "pending",
            "sent",
            "unknown",
            "rejected",
          ].includes(item.status)
      )
      .slice(-100)
      .map((item) => ({
        ...item,
        status:
          item.status === "pending" && !item.requestId
            ? "unknown"
            : item.status,
      }));
  } catch {
    return [];
  }
}

function loadDraft(userId?: string): string {
  try {
    return (
      sessionStorage.getItem(
        draftStorageKey(userId)
      ) ?? ""
    );
  } catch {
    return "";
  }
}

async function readJson(
  response: Response
): Promise<ChatResponse> {
  const value: unknown = await response.json();

  if (!value || typeof value !== "object") {
    throw new Error("服务器返回格式不正确");
  }

  return value as ChatResponse;
}

export default function App(
  { authToken, userId }: AppProps = {}
) {
  const [messages, setMessages] =
    useState<Message[]>(() => loadMessages(userId));

  const [input, setInput] =
    useState(() => loadDraft(userId));

  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  const [checkingId, setCheckingId] =
    useState<string | null>(null);

  const sendingLock = useRef(false);
  const checkingIds = useRef(new Set<string>());
  const bottomRef = useRef<HTMLDivElement>(null);

  // ========================================
  // 保存聊天记录和草稿
  // ========================================

  useEffect(() => {
    saveMessages(messages, userId);
  }, [messages, userId]);

  useEffect(() => {
    try {
      sessionStorage.setItem(
        draftStorageKey(userId),
        input
      );
    } catch {
      // 草稿缓存失败不影响输入
    }
  }, [input, userId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({
      behavior: "smooth",
    });
  }, [messages, sending]);

  // ========================================
  // 请求结果未知
  // ========================================

  function markUnknown(
    requestId: string,
    detail: string
  ) {
    setMessages((previous) =>
      previous.map((item) =>
        item.requestId === requestId &&
        item.role === "user"
          ? { ...item, status: "unknown" }
          : item
      )
    );

    setError(`请求结果尚未确认：${detail}`);
  }

  // ========================================
  // 已被明确拒绝：没有调用模型
  // ========================================

  function markRejected(
    requestId: string,
    originalText: string,
    code?: string
  ) {
    setMessages((previous) =>
      previous.map((item) =>
        item.requestId === requestId &&
        item.role === "user"
          ? { ...item, status: "rejected" }
          : item
      )
    );

    // 恢复草稿，不自动重发。
    setInput(originalText);

    if (code === "daily_quota_exceeded") {
      setError(
        "过去 24 小时的新请求额度已用完。" +
          "这条消息没有调用 AI，已恢复到输入框，可稍后手动发送。"
      );
    } else if (
      code === "concurrency_limit_exceeded"
    ) {
      setError(
        "当前正在处理的请求过多。" +
          "这条消息没有调用 AI，已恢复到输入框，可稍后手动发送。"
      );
    } else {
      setError(
        "服务器拒绝接收本次新请求。" +
          "这条消息没有调用 AI，已恢复到输入框。"
      );
    }
  }

  // ========================================
  // 完成请求并去重显示
  // ========================================

  function finishRequest(
    requestId: string,
    reply: string
  ) {
    const replyMessage: Message = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: reply,
      status: "sent",
      replyTo: requestId,
    };

    setMessages((previous) => {
      const next: Message[] = previous.map((item) =>
        item.requestId === requestId &&
        item.role === "user"
          ? { ...item, status: "sent" }
          : item
      );

      if (
        next.some(
          (item) =>
            item.role === "assistant" &&
            item.replyTo === requestId
        )
      ) {
        return next;
      }

      return [...next, replyMessage];
    });

    setError("");
  }

  // ========================================
  // 查询服务器请求状态
  // ========================================

  async function queryStatus(
    requestId: string,
    poll = false
  ) {
    if (checkingIds.current.has(requestId)) {
      return;
    }

    checkingIds.current.add(requestId);
    setCheckingId(requestId);

    try {
      const maxChecks = poll ? 20 : 1;

      for (
        let index = 0;
        index < maxChecks;
        index++
      ) {
        const response = await fetch(
          `/api/chat/requests/${encodeURIComponent(
            requestId
          )}`,
          {
            cache: "no-store",
            headers: authToken
              ? {
                  Authorization:
                    `Bearer ${authToken}`,
                }
              : {},
          }
        );

        if (!response.ok) {
          throw new Error(
            `查询失败：HTTP ${response.status}`
          );
        }

        const result = await readJson(response);

        if (
          result.status === "completed" &&
          typeof result.reply === "string"
        ) {
          finishRequest(requestId, result.reply);
          return;
        }

        if (result.status === "unknown") {
          markUnknown(
            requestId,
            "服务器无法确定之前的模型调用结果，请不要直接重发。"
          );
          return;
        }

        if (result.status !== "processing") {
          throw new Error("无法识别的请求状态");
        }

        if (index + 1 < maxChecks) {
          await new Promise<void>((resolve) =>
            window.setTimeout(resolve, 1500)
          );
        }
      }

      setError(
        "服务器仍在处理该消息。" +
          "稍后点击“查询结果”，无需重复发送。"
      );
    } catch {
      markUnknown(
        requestId,
        "无法查询服务器状态。请稍后点击“查询结果”。"
      );
    } finally {
      checkingIds.current.delete(requestId);

      setCheckingId((current) =>
        current === requestId ? null : current
      );
    }
  }

  // ========================================
  // 页面刷新：恢复未完成的请求
  // ========================================

  useEffect(() => {
    const unfinished = loadMessages(userId).filter(
      (item) =>
        item.role === "user" &&
        item.status === "pending" &&
        item.requestId
    );

    unfinished.forEach((item) => {
      if (item.requestId) {
        void queryStatus(item.requestId, true);
      }
    });

    // 仅在页面首次打开时恢复
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ========================================
  // 发送聊天请求
  // ========================================

  async function sendMessage(
    event: FormEvent<HTMLFormElement>
  ) {
    event.preventDefault();

    const text = input.trim();

    if (!text || sendingLock.current) {
      return;
    }

    sendingLock.current = true;
    setSending(true);
    setError("");

    const requestId = crypto.randomUUID();

    const userMessage: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      status: "pending",
      requestId,
    };

    // 先持久保存原请求 ID，再发送请求。
    const updated = [...messages, userMessage];

    saveMessages(updated, userId);
    setMessages(updated);
    setInput("");

    const controller = new AbortController();

    const timeout = window.setTimeout(
      () => controller.abort(),
      60000
    );

    try {
      const response = await fetch("/api/chat", {
        method: "POST",

        headers: {
          "Content-Type": "application/json",
          ...(authToken
            ? {
                Authorization:
                  `Bearer ${authToken}`,
              }
            : {}),
        },

        body: JSON.stringify({
          message: text,
          request_id: requestId,
        }),

        signal: controller.signal,
      });

      const result = await readJson(response);

      // ====================================
      // HTTP 429：明确拒绝，没有调用模型
      // ====================================

      if (
        response.status === 429 &&
        result.status === "rejected"
      ) {
        markRejected(
          requestId,
          text,
          result.code
        );
      }

      // ====================================
      // 原请求仍在处理中
      // ====================================

      else if (
        response.status === 202 &&
        result.status === "processing"
      ) {
        await queryStatus(requestId, true);
      }

      // ====================================
      // 成功收到回复
      // ====================================

      else if (
        response.ok &&
        typeof result.reply === "string"
      ) {
        finishRequest(requestId, result.reply);
      }

      // ====================================
      // 其他无法确认的状态
      // ====================================

      else {
        markUnknown(
          requestId,
          "服务没有返回确认的结果，请先查询状态。"
        );
      }

    } catch {
      markUnknown(
        requestId,
        "网络异常或等待超时，模型可能已执行，请先查询状态。"
      );

    } finally {
      window.clearTimeout(timeout);
      sendingLock.current = false;
      setSending(false);
    }
  }

  // ========================================
  // Enter 发送，Shift + Enter 换行
  // ========================================

  function handleKeyDown(
    event: KeyboardEvent<HTMLTextAreaElement>
  ) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  // ========================================
  // 页面
  // ========================================

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">✳</div>

          <div>
            <strong>Personal Agent</strong>
            <span>AI Workspace</span>
          </div>
        </div>

        <div className="sidebar-section">
          <p className="section-label">
            工作空间
          </p>

          <div className="nav-item selected">
            <span className="nav-symbol">◻</span>
            <span>聊天室</span>
          </div>

          <div className="nav-item disabled">
            <span className="nav-symbol">☷</span>
            <span>任务中心</span>
            <small>即将推出</small>
          </div>
        </div>

        <div className="sidebar-section">
          <p className="section-label">
            我的 BOT
          </p>

          <div className="bot-item">
            <div className="bot-avatar">✦</div>

            <div>
              <strong>默认助手</strong>
              <span>Personal Assistant</span>
            </div>
          </div>
        </div>

        <div className="sidebar-footer">
          <div className="version">
            Personal Agent · Dev
          </div>

          <p>你的 AI 助手工作空间</p>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="assistant-info">
            <strong>默认助手</strong>
            <span>你的个人 AI 助理</span>
          </div>

          <div className="dev-badge">
            开发版本
          </div>
        </header>

        <section className="chat-area">
          {messages.length === 0 && (
            <div className="welcome">
              <div className="welcome-logo">
                ✳
              </div>

              <h1>今天有什么新想法？</h1>

              <p>
                欢迎来到 Personal Agent。
                <br />
                你的智能工作空间，从一次对话开始。
              </p>

              <div className="welcome-tip">
                一个简洁、可靠、不断成长的 AI 助手
              </div>
            </div>
          )}

          <div className="message-container">
            {messages.map((message) => (
              <article
                className={`message ${message.role}`}
                key={message.id}
              >
                <div className="message-avatar">
                  {message.role === "assistant"
                    ? "✦"
                    : "我"}
                </div>

                <div className="message-body">
                  <div className="message-name">
                    {message.role === "assistant"
                      ? "Personal Agent"
                      : "你"}
                  </div>

                  <div className="message-content">
                    {message.content}
                  </div>

                  {message.status === "pending" && (
                    <div className="message-status">
                      等待回复…
                    </div>
                  )}

                  {message.status === "unknown" && (
                    <div className="message-status warning">
                      请求结果尚未确认
                    </div>
                  )}

                  {message.status === "rejected" && (
                    <div className="message-status warning">
                      未发送成功 · AI 未执行
                    </div>
                  )}

                  {message.role === "user" &&
                    message.requestId &&
                    (
                      message.status === "pending" ||
                      message.status === "unknown"
                    ) && (
                      <button
                        type="button"
                        onClick={() =>
                          void queryStatus(
                            message.requestId!
                          )
                        }
                        disabled={
                          checkingId ===
                          message.requestId
                        }
                        aria-label="查询结果"
                      >
                        {checkingId === message.requestId
                          ? "查询中…"
                          : "查询结果"}
                      </button>
                    )}
                </div>
              </article>
            ))}

            {sending && (
              <div className="loading" role="status">
                <div className="spinner" />

                <span>
                  助手正在处理，请稍候…
                </span>
              </div>
            )}

            <div ref={bottomRef} />
          </div>
        </section>

        <div className="input-section">
          {error && (
            <div className="error-box" role="alert">
              {error}
            </div>
          )}

          <form
            className="input-box"
            onSubmit={sendMessage}
          >
            <textarea
              aria-label="输入聊天消息"
              placeholder="向 Personal Agent 发送消息..."
              value={input}
              onChange={(event) =>
                setInput(event.target.value)
              }
              onKeyDown={handleKeyDown}
              disabled={sending}
              maxLength={8000}
              rows={2}
            />

            <div className="input-actions">
              <span>
                Enter 发送 · Shift + Enter 换行
              </span>

              <button
                type="submit"
                disabled={sending || !input.trim()}
                aria-label={
                  sending ? "发送中…" : "发送 ↑"
                }
              >
                {sending ? "发送中…" : "发送 ↑"}
              </button>
            </div>
          </form>

          <p className="footer-note">
            当前为本地开发版 ·
            聊天记录暂存于当前浏览器标签页
          </p>
        </div>
      </main>
    </div>
  );
}