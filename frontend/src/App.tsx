
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

type ReminderStatusValue =
  | "pending"
  | "sent"
  | "cancelled";

type Reminder = {
  id: number;
  message: string;
  minutes: number;
  remind_at: string;
  delivery: "ntfy" | "in_app";
  status: ReminderStatusValue;
};

type DueReminder = {
  id: number;
  message: string;
  remind_at: string;
};

type ReminderListResponse = {
  enabled?: boolean;
  reminders?: unknown;
};

const REMINDER_STATUS_TEXT: Record<
  ReminderStatusValue,
  string
> = {
  pending: "待发送",
  sent: "已送达",
  cancelled: "已取消",
};

// 到期提醒的轮询间隔。
const DUE_POLL_MS = 60_000;

function formatRemindAt(value: string): string {
  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString("zh-CN", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

async function readReminders(
  response: Response
): Promise<{
  enabled: boolean;
  reminders: Reminder[];
}> {
  const value: unknown = await response.json();

  if (!value || typeof value !== "object") {
    throw new Error("服务器返回格式不正确");
  }

  const data = value as ReminderListResponse;

  return {
    enabled: data.enabled !== false,
    reminders: Array.isArray(data.reminders)
      ? (data.reminders as Reminder[])
      : [],
  };
}

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

  const [view, setView] =
    useState<"chat" | "reminders">("chat");

  const [reminders, setReminders] =
    useState<Reminder[]>([]);

  const [dueReminders, setDueReminders] =
    useState<DueReminder[]>([]);

  const [reminderError, setReminderError] = useState("");

  const [reminderBusy, setReminderBusy] =
    useState<number | null>(null);

  const [delayMinutes, setDelayMinutes] = useState<
    Record<number, string>
  >({});

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
    } else if (code === "user_budget_exceeded") {
      setError(
        "你过去 24 小时的个人 AI 预算已用完。" +
          "这条消息没有调用 AI，已恢复到输入框，可稍后手动发送。"
      );
    } else if (code === "global_budget_exceeded") {
      setError(
        "全站过去 24 小时的 AI 预算已用完。" +
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
  // 站内提醒
  // ========================================

  function authHeaders(): Record<string, string> {
    return authToken
      ? { Authorization: `Bearer ${authToken}` }
      : {};
  }

  async function loadReminders() {
    if (!authToken) return;

    try {
      const response = await fetch("/api/reminders", {
        headers: authHeaders(),
      });

      if (!response.ok) {
        throw new Error(
          `读取提醒失败（${response.status}）`
        );
      }

      const data = await readReminders(response);

      setReminders(data.reminders);
      setReminderError("");
    } catch (cause) {
      setReminderError(
        cause instanceof Error
          ? cause.message
          : "读取提醒失败"
      );
    }
  }

  async function loadDueReminders() {
    if (!authToken) return;

    try {
      const response = await fetch(
        "/api/reminders/in-app/due",
        { headers: authHeaders() }
      );

      if (!response.ok) return;

      const data = await readReminders(response);

      setDueReminders(
        data.enabled
          ? (data.reminders as DueReminder[])
          : []
      );
    } catch {
      // 到期提醒读取失败不打断聊天
    }
  }

  async function cancelReminder(reminderId: number) {
    setReminderBusy(reminderId);

    try {
      const response = await fetch(
        `/api/reminders/${reminderId}`,
        {
          method: "DELETE",
          headers: authHeaders(),
        }
      );

      if (!response.ok) {
        throw new Error(
          `删除失败（${response.status}）`
        );
      }

      setDueReminders((previous) =>
        previous.filter(
          (item) => item.id !== reminderId
        )
      );

      await loadReminders();
    } catch (cause) {
      setReminderError(
        cause instanceof Error
          ? cause.message
          : "删除失败"
      );
    } finally {
      setReminderBusy(null);
    }
  }

  async function rescheduleReminder(reminderId: number) {
    const raw = delayMinutes[reminderId] ?? "10";
    const minutes = Number(raw);

    if (
      !Number.isInteger(minutes) ||
      minutes < 1 ||
      minutes > 43200
    ) {
      setReminderError("请输入 1～43200 之间的分钟数");
      return;
    }

    setReminderBusy(reminderId);

    try {
      const response = await fetch(
        `/api/reminders/${reminderId}`,
        {
          method: "PATCH",
          headers: {
            ...authHeaders(),
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ minutes }),
        }
      );

      if (!response.ok) {
        throw new Error(
          `修改失败（${response.status}）`
        );
      }

      await loadReminders();
    } catch (cause) {
      setReminderError(
        cause instanceof Error
          ? cause.message
          : "修改失败"
      );
    } finally {
      setReminderBusy(null);
    }
  }

  async function acknowledgeReminder(reminderId: number) {
    try {
      const response = await fetch(
        `/api/reminders/${reminderId}/ack`,
        {
          method: "POST",
          headers: authHeaders(),
        }
      );

      if (!response.ok) return;

      setDueReminders((previous) =>
        previous.filter(
          (item) => item.id !== reminderId
        )
      );

      await loadReminders();
    } catch {
      // 确认失败时保留提示条，下次轮询还会出现
    }
  }

  // 打开页面取一次到期提醒，之后定时轮询。
  useEffect(() => {
    if (!authToken) return;

    void loadDueReminders();

    const timer = window.setInterval(
      () => void loadDueReminders(),
      DUE_POLL_MS
    );

    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authToken]);

  // 提醒列表用于面板和侧栏数量，按需重新加载。
  useEffect(() => {
    void loadReminders();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authToken, view]);

  const pendingCount = reminders.filter(
    (item) => item.status === "pending"
  ).length;

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

          <button
            type="button"
            className={`nav-item ${
              view === "chat" ? "selected" : ""
            }`}
            onClick={() => setView("chat")}
            aria-pressed={view === "chat"}
          >
            <span className="nav-symbol">◻</span>
            <span>聊天室</span>
          </button>

          <button
            type="button"
            className={`nav-item ${
              view === "reminders" ? "selected" : ""
            }`}
            onClick={() => setView("reminders")}
            aria-pressed={view === "reminders"}
            aria-label="提醒"
          >
            <span className="nav-symbol">⏰</span>
            <span>提醒</span>

            {pendingCount > 0 && (
              <small>{pendingCount}</small>
            )}
          </button>

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

        {dueReminders.length > 0 && (
          <section
            className="due-banner"
            aria-label="到期的站内提醒"
          >
            <strong>到期的提醒</strong>

            <ul>
              {dueReminders.map((reminder) => (
                <li key={reminder.id}>
                  <span className="due-message">
                    {reminder.message}
                  </span>

                  <span className="due-time">
                    {formatRemindAt(reminder.remind_at)}
                  </span>

                  <button
                    type="button"
                    aria-label={`知道了 ${reminder.id}`}
                    onClick={() =>
                      void acknowledgeReminder(
                        reminder.id
                      )
                    }
                  >
                    知道了
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}

        {view === "reminders" ? (
          <section
            className="reminder-panel"
            aria-label="我的提醒"
          >
            <div className="reminder-head">
              <div>
                <h2>我的提醒</h2>

                <p className="reminder-hint">
                  在聊天里说“10 分钟后提醒我开会”，
                  助手就会在这里创建提醒。
                  站内提醒只在网页打开时显示。
                </p>
              </div>

              <button
                type="button"
                onClick={() => void loadReminders()}
                aria-label="刷新提醒"
              >
                刷新
              </button>
            </div>

            {reminderError && (
              <div
                className="error-box"
                data-testid="reminder-error"
              >
                {reminderError}
              </div>
            )}

            {reminders.length === 0 ? (
              <p
                className="reminder-empty"
                data-testid="reminder-empty"
              >
                还没有提醒
              </p>
            ) : (
              <ul className="reminder-list">
                {reminders.map((reminder) => (
                  <li
                    className={`reminder-row ${reminder.status}`}
                    key={reminder.id}
                  >
                    <div className="reminder-main">
                      <strong>{reminder.message}</strong>

                      <span className="reminder-time">
                        {formatRemindAt(
                          reminder.remind_at
                        )}
                      </span>

                      <span className="reminder-status">
                        {
                          REMINDER_STATUS_TEXT[
                            reminder.status
                          ]
                        }
                      </span>
                    </div>

                    {reminder.status === "pending" && (
                      <div className="reminder-actions">
                        <label>
                          改时间（分钟）

                          <input
                            type="number"
                            min={1}
                            max={43200}
                            aria-label={`新的提醒分钟数 ${reminder.id}`}
                            value={
                              delayMinutes[reminder.id] ??
                              "10"
                            }
                            onChange={(event) =>
                              setDelayMinutes(
                                (previous) => ({
                                  ...previous,
                                  [reminder.id]:
                                    event.target.value,
                                })
                              )
                            }
                          />
                        </label>

                        <button
                          type="button"
                          disabled={
                            reminderBusy === reminder.id
                          }
                          aria-label={`改时间 ${reminder.id}`}
                          onClick={() =>
                            void rescheduleReminder(
                              reminder.id
                            )
                          }
                        >
                          改时间
                        </button>

                        <button
                          type="button"
                          disabled={
                            reminderBusy === reminder.id
                          }
                          aria-label={`删除提醒 ${reminder.id}`}
                          onClick={() =>
                            void cancelReminder(
                              reminder.id
                            )
                          }
                        >
                          删除
                        </button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        ) : (
          <>
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
          </>
        )}
      </main>
    </div>
  );
}