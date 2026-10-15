
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

type QuotaReport = {
  window_hours: number;
  role: string;
  requests: {
    used: number;
    limit: number;
    remaining: number;
  };
  budget: {
    used_usd: string;
    limit_usd: string;
    remaining_usd: string;
    unresolved_reservations: number;
  };
  note: string;
};

const ROLE_TEXT: Record<string, string> = {
  owner: "站长",
  admin: "管理员",
  trusted: "可信用户",
  member: "普通用户",
};

function roleText(role: string): string {
  return ROLE_TEXT[role] ?? role;
}

// 后端返回的是十进制字符串，展示时统一成 4 位小数。
function formatUsd(value: string): string {
  const amount = Number(value);

  if (!Number.isFinite(amount)) {
    return `US$${value}`;
  }

  return `US$${amount.toFixed(4)}`;
}

function usagePercent(used: number, limit: number): number {
  if (
    !Number.isFinite(used) ||
    !Number.isFinite(limit) ||
    limit <= 0
  ) {
    return 0;
  }

  return Math.min(
    Math.max((used / limit) * 100, 0),
    100,
  );
}

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

type MonitorTask = {
  id: string;
  owner_id: string;
  task_type: string;
  source_url: string;
  interval_minutes: number;
  status: "active" | "paused";
  last_checked_at: string | null;
  next_run_at: string | null;
  last_error: string | null;
  created_at: string;
  archived_at: string | null;
};

type MonitorListResponse = {
  monitors?: unknown;
  source_types?: unknown;
};

// 收件箱条目：非站长用户的结果与到点提醒都汇总到这里。
// kind 决定确认时打到哪个来源。
type InboxItem = {
  kind: "monitor" | "reminder";
  id: number;
  message: string;
  created_at: string;
};

type InboxResponse = {
  items?: unknown;
};

async function readInbox(
  response: Response
): Promise<InboxItem[]> {
  const value: unknown = await response.json();

  if (!value || typeof value !== "object") {
    throw new Error("服务器返回格式不正确");
  }

  const data = value as InboxResponse;

  return Array.isArray(data.items)
    ? (data.items as InboxItem[])
    : [];
}

const MONITOR_STATUS_TEXT: Record<string, string> = {
  active: "运行中",
  paused: "已暂停",
};

// 数据源类型的中文名。后端会给出支持的列表，
// 这里只负责把已知类型翻译得好看一点。
const MONITOR_SOURCE_TEXT: Record<string, string> = {
  rss: "RSS 订阅",
  http_text: "网页内容变化",
};

function sourceTypeText(sourceType: string): string {
  return MONITOR_SOURCE_TEXT[sourceType] ?? sourceType;
}

const MONITOR_MIN_INTERVAL = 15;

async function readMonitors(
  response: Response
): Promise<{
  monitors: MonitorTask[];
  sourceTypes: string[];
}> {
  const value: unknown = await response.json();

  if (!value || typeof value !== "object") {
    throw new Error("服务器返回格式不正确");
  }

  const data = value as MonitorListResponse;

  return {
    monitors: Array.isArray(data.monitors)
      ? (data.monitors as MonitorTask[])
      : [],
    sourceTypes: Array.isArray(data.source_types)
      ? (data.source_types as unknown[]).filter(
          (item): item is string =>
            typeof item === "string"
        )
      : [],
  };
}

// 后端的 detail 已经是给人看的中文，直接展示，
// 前端不再自己造一套文案（否则两处措辞会漂移）。
async function readDetail(
  response: Response,
  fallback: string
): Promise<string> {
  try {
    const value: unknown = await response.json();

    if (
      value &&
      typeof value === "object" &&
      typeof (value as { detail?: unknown }).detail ===
        "string"
    ) {
      return (value as { detail: string }).detail;
    }
  } catch {
    // 解析失败就用兜底文案
  }

  return fallback;
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
    useState<"chat" | "reminders" | "quota" | "monitors">(
      "chat"
    );

  const [quota, setQuota] = useState<QuotaReport | null>(
    null
  );

  const [quotaError, setQuotaError] = useState("");
  const [quotaLoading, setQuotaLoading] = useState(false);

  const [monitors, setMonitors] = useState<MonitorTask[]>(
    []
  );

  const [monitorError, setMonitorError] = useState("");
  const [monitorNotice, setMonitorNotice] = useState("");
  const [monitorForbidden, setMonitorForbidden] =
    useState(false);

  const [monitorBusy, setMonitorBusy] = useState<
    string | null
  >(null);

  const [monitorUrl, setMonitorUrl] = useState("");
  const [monitorInterval, setMonitorInterval] =
    useState("30");

  // 数据源类型：默认 RSS；后端返回支持列表后校正选择。
  const [monitorSourceType, setMonitorSourceType] =
    useState("rss");

  const [monitorSourceTypes, setMonitorSourceTypes] =
    useState<string[]>([]);

  const [notifications, setNotifications] = useState<
    InboxItem[]
  >([]);

  const [notificationError, setNotificationError] =
    useState("");

  const [notificationBusy, setNotificationBusy] = useState<
    number | null
  >(null);

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

  async function loadQuota() {
    if (!authToken) return;

    setQuotaLoading(true);

    try {
      const response = await fetch(
        "/api/usage/summary",
        { headers: authHeaders() }
      );

      if (!response.ok) {
        throw new Error(
          `读取额度失败（${response.status}）`
        );
      }

      const value: unknown = await response.json();

      if (!value || typeof value !== "object") {
        throw new Error("服务器返回格式不正确");
      }

      setQuota(value as QuotaReport);
      setQuotaError("");
    } catch (cause) {
      setQuotaError(
        cause instanceof Error
          ? cause.message
          : "读取额度失败"
      );
    } finally {
      setQuotaLoading(false);
    }
  }

  // ========================================
  // 收件箱（任何角色都能看自己的）
  // ========================================

  async function loadNotifications() {
    if (!authToken) return;

    try {
      const response = await fetch("/api/inbox", {
        headers: authHeaders(),
      });

      if (!response.ok) {
        throw new Error(
          await readDetail(
            response,
            `读取收件箱失败（${response.status}）`
          )
        );
      }

      setNotifications(await readInbox(response));
      setNotificationError("");
    } catch (cause) {
      setNotificationError(
        cause instanceof Error
          ? cause.message
          : "读取收件箱失败"
      );
    }
  }

  async function acknowledgeNotification(
    kind: InboxItem["kind"],
    notificationId: number
  ) {
    setNotificationBusy(notificationId);

    try {
      const response = await fetch(
        `/api/inbox/${kind}/${notificationId}/ack`,
        {
          method: "POST",
          headers: authHeaders(),
        }
      );

      if (!response.ok) {
        throw new Error(
          await readDetail(
            response,
            `确认失败（${response.status}）`
          )
        );
      }

      await loadNotifications();
    } catch (cause) {
      setNotificationError(
        cause instanceof Error
          ? cause.message
          : "确认失败"
      );
    } finally {
      setNotificationBusy(null);
    }
  }

  // ========================================
  // 监控任务（任务中心）
  // ========================================

  async function loadMonitors() {
    if (!authToken) return;

    try {
      const response = await fetch("/api/monitors", {
        headers: authHeaders(),
      });

      // 普通用户没有管理权限：这不是错误，是权限边界。
      if (response.status === 403) {
        setMonitorForbidden(true);
        setMonitors([]);
        setMonitorError("");
        return;
      }

      if (!response.ok) {
        throw new Error(
          await readDetail(
            response,
            `读取监控任务失败（${response.status}）`
          )
        );
      }

      setMonitorForbidden(false);

      const data = await readMonitors(response);

      setMonitors(data.monitors);

      // 以后端声明的支持列表为准：当前选择不在列表里就回退到第一个，
      // 避免界面停在一个引擎其实不支持的类型上。
      if (data.sourceTypes.length > 0) {
        setMonitorSourceTypes(data.sourceTypes);

        setMonitorSourceType((current) =>
          data.sourceTypes.includes(current)
            ? current
            : data.sourceTypes[0]
        );
      }

      setMonitorError("");
    } catch (cause) {
      setMonitorError(
        cause instanceof Error
          ? cause.message
          : "读取监控任务失败"
      );
    }
  }

  async function createMonitor() {
    const minutes = Number(monitorInterval);

    if (
      !Number.isInteger(minutes) ||
      minutes < MONITOR_MIN_INTERVAL
    ) {
      setMonitorNotice("");
      setMonitorError(
        `检查间隔必须是 ${MONITOR_MIN_INTERVAL} 分钟以上的整数`
      );
      return;
    }

    setMonitorBusy("create");

    try {
      const response = await fetch("/api/monitors", {
        method: "POST",
        headers: {
          ...authHeaders(),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          source_url: monitorUrl.trim(),
          interval_minutes: minutes,
          source_type: monitorSourceType,
        }),
      });

      if (!response.ok) {
        throw new Error(
          await readDetail(
            response,
            `创建失败（${response.status}）`
          )
        );
      }

      setMonitorUrl("");
      setMonitorNotice(
        "监控任务已创建，稍后会开始首次抓取。"
      );
      setMonitorError("");

      await loadMonitors();
    } catch (cause) {
      setMonitorNotice("");
      setMonitorError(
        cause instanceof Error ? cause.message : "创建失败"
      );
    } finally {
      setMonitorBusy(null);
    }
  }

  async function changeMonitorState(
    taskId: string,
    action: "pause" | "resume"
  ) {
    setMonitorBusy(taskId);

    try {
      const response = await fetch(
        `/api/monitors/${encodeURIComponent(
          taskId
        )}/${action}`,
        {
          method: "POST",
          headers: authHeaders(),
        }
      );

      if (!response.ok) {
        throw new Error(
          await readDetail(
            response,
            `操作失败（${response.status}）`
          )
        );
      }

      await loadMonitors();
    } catch (cause) {
      setMonitorError(
        cause instanceof Error ? cause.message : "操作失败"
      );
    } finally {
      setMonitorBusy(null);
    }
  }

  async function archiveMonitor(taskId: string) {
    setMonitorBusy(taskId);

    try {
      const response = await fetch(
        `/api/monitors/${encodeURIComponent(taskId)}`,
        {
          method: "DELETE",
          headers: authHeaders(),
        }
      );

      if (!response.ok) {
        throw new Error(
          await readDetail(
            response,
            `归档失败（${response.status}）`
          )
        );
      }

      setMonitorNotice("监控任务已归档。");

      await loadMonitors();
    } catch (cause) {
      setMonitorError(
        cause instanceof Error ? cause.message : "归档失败"
      );
    } finally {
      setMonitorBusy(null);
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

  // 提醒列表用于面板和侧栏数量；额度只在需要时读取。
  useEffect(() => {
    void loadReminders();

    if (view === "quota") {
      void loadQuota();
    }

    // 监控任务同样按需读取：没打开就不发请求。
    // 站内通知也在这里取 —— 它不属于监控权限，任何角色都能看自己的。
    if (view === "monitors") {
      void loadNotifications();
      void loadMonitors();
    }
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

          <button
            type="button"
            className={`nav-item ${
              view === "quota" ? "selected" : ""
            }`}
            onClick={() => setView("quota")}
            aria-pressed={view === "quota"}
            aria-label="额度"
          >
            <span className="nav-symbol">◔</span>
            <span>额度</span>
          </button>

          <button
            type="button"
            className={`nav-item ${
              view === "monitors" ? "selected" : ""
            }`}
            onClick={() => setView("monitors")}
            aria-pressed={view === "monitors"}
            aria-label="任务中心"
          >
            <span className="nav-symbol">☷</span>
            <span>任务中心</span>
          </button>
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

        {view === "monitors" ? (
          <section
            className="monitor-panel"
            aria-label="监控任务"
          >
            <div className="reminder-head">
              <div>
                <h2>任务中心</h2>

                <p className="reminder-hint">
                  只支持公网 HTTPS 订阅源；同一台主机
                  5 分钟内最多抓取一次。
                  当前只有站长可以创建，通知发往站长的
                  ntfy。
                </p>
              </div>

              <button
                type="button"
                onClick={() => void loadMonitors()}
                aria-label="刷新监控任务"
              >
                刷新
              </button>
            </div>

            {notificationError && (
              <div
                className="error-box"
                data-testid="notification-error"
              >
                {notificationError}
              </div>
            )}

            {notifications.length > 0 && (
              <section
                className="notice-panel"
                aria-label="收件箱"
              >
                <h3>
                  收件箱（{notifications.length}）
                </h3>

                <ul className="monitor-list">
                  {notifications.map((item) => (
                    <li
                      className="monitor-row"
                      key={`${item.kind}-${item.id}`}
                    >
                      <div className="reminder-main">
                        <strong className="notice-message">
                          {item.message}
                        </strong>

                        <span className="reminder-time">
                          {item.kind === "reminder"
                            ? "提醒"
                            : "监控"}{" "}
                          ·{" "}
                          {formatRemindAt(
                            item.created_at
                          )}
                        </span>
                      </div>

                      <div className="reminder-actions">
                        <button
                          type="button"
                          disabled={
                            notificationBusy === item.id
                          }
                          aria-label={`知道了 ${item.kind} ${item.id}`}
                          onClick={() =>
                            void acknowledgeNotification(
                              item.kind,
                              item.id
                            )
                          }
                        >
                          知道了
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {monitorError && (
              <div
                className="error-box"
                data-testid="monitor-error"
              >
                {monitorError}
              </div>
            )}

            {monitorNotice && (
              <div
                className="notice-box"
                data-testid="monitor-notice"
              >
                {monitorNotice}
              </div>
            )}

            {monitorForbidden ? (
              <p
                className="reminder-empty"
                data-testid="monitor-forbidden"
              >
                没有管理监控任务的权限（需要管理员或站长身份）。
              </p>
            ) : (
              <>
                <form
                  className="monitor-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void createMonitor();
                  }}
                >
                  <label>
                    数据源类型

                    <select
                      aria-label="数据源类型"
                      value={monitorSourceType}
                      onChange={(event) =>
                        setMonitorSourceType(
                          event.target.value
                        )
                      }
                    >
                      {(
                        monitorSourceTypes.length > 0
                          ? monitorSourceTypes
                          : ["rss"]
                      ).map((item) => (
                        <option key={item} value={item}>
                          {sourceTypeText(item)}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label>
                    订阅地址

                    <input
                      type="url"
                      aria-label="订阅地址"
                      placeholder="https://example.com/rss.xml"
                      value={monitorUrl}
                      onChange={(event) =>
                        setMonitorUrl(event.target.value)
                      }
                    />
                  </label>

                  <label>
                    检查间隔（分钟）

                    <input
                      type="number"
                      min={MONITOR_MIN_INTERVAL}
                      max={1440}
                      aria-label="检查间隔分钟数"
                      value={monitorInterval}
                      onChange={(event) =>
                        setMonitorInterval(
                          event.target.value
                        )
                      }
                    />
                  </label>

                  <button
                    type="submit"
                    disabled={
                      monitorBusy === "create" ||
                      !monitorUrl.trim()
                    }
                    aria-label="创建监控任务"
                  >
                    {monitorBusy === "create"
                      ? "创建中…"
                      : "创建"}
                  </button>
                </form>

                {monitors.length === 0 ? (
                  <p
                    className="reminder-empty"
                    data-testid="monitor-empty"
                  >
                    还没有监控任务
                  </p>
                ) : (
                  <ul className="monitor-list">
                    {monitors.map((task) => (
                      <li
                        className={`monitor-row ${task.status}`}
                        key={task.id}
                      >
                        <div className="reminder-main">
                          <strong className="monitor-url">
                            {task.source_url}
                          </strong>

                          <span className="reminder-time">
                            {sourceTypeText(task.task_type)}
                            {" · "}
                            每 {task.interval_minutes} 分钟
                            {" · "}
                            {MONITOR_STATUS_TEXT[
                              task.status
                            ] ?? task.status}
                          </span>

                          {task.last_checked_at && (
                            <span className="reminder-time">
                              上次检查：
                              {formatRemindAt(
                                task.last_checked_at
                              )}
                            </span>
                          )}

                          {task.status === "active" &&
                            task.next_run_at && (
                              <span className="reminder-time">
                                下次检查：
                                {formatRemindAt(
                                  task.next_run_at
                                )}
                              </span>
                            )}

                          {task.last_error && (
                            <span className="monitor-error-text">
                              {task.last_error}
                            </span>
                          )}
                        </div>

                        <div className="reminder-actions">
                          {task.status === "active" ? (
                            <button
                              type="button"
                              disabled={
                                monitorBusy === task.id
                              }
                              aria-label={`暂停 ${task.id}`}
                              onClick={() =>
                                void changeMonitorState(
                                  task.id,
                                  "pause"
                                )
                              }
                            >
                              暂停
                            </button>
                          ) : (
                            <button
                              type="button"
                              disabled={
                                monitorBusy === task.id
                              }
                              aria-label={`恢复 ${task.id}`}
                              onClick={() =>
                                void changeMonitorState(
                                  task.id,
                                  "resume"
                                )
                              }
                            >
                              恢复
                            </button>
                          )}

                          <button
                            type="button"
                            disabled={
                              monitorBusy === task.id
                            }
                            aria-label={`归档 ${task.id}`}
                            onClick={() =>
                              void archiveMonitor(task.id)
                            }
                          >
                            归档
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </section>
        ) : view === "quota" ? (
          <section
            className="quota-panel"
            aria-label="我的额度"
          >
            <div className="reminder-head">
              <div>
                <h2>我的额度</h2>

                <p className="reminder-hint">
                  过去 {quota?.window_hours ?? 24} 小时的用量，
                  超出后新的聊天请求会被拒绝。
                  身份：{quota ? roleText(quota.role) : "…"}
                </p>
              </div>

              <button
                type="button"
                onClick={() => void loadQuota()}
                aria-label="刷新额度"
              >
                刷新
              </button>
            </div>

            {quotaError && (
              <div
                className="error-box"
                data-testid="quota-error"
              >
                {quotaError}
              </div>
            )}

            {quotaLoading && !quota && (
              <p className="reminder-empty">
                正在读取…
              </p>
            )}

            {quota && (
              <div
                className="quota-cards"
                data-testid="quota-cards"
              >
                <article className="quota-card">
                  <h3>请求次数</h3>

                  <p className="quota-value">
                    {quota.requests.used} /{" "}
                    {quota.requests.limit}
                  </p>

                  <div className="quota-bar">
                    <span
                      style={{
                        width: `${usagePercent(
                          quota.requests.used,
                          quota.requests.limit
                        )}%`,
                      }}
                    />
                  </div>

                  <p className="quota-foot">
                    剩余 {quota.requests.remaining} 次
                  </p>
                </article>

                <article className="quota-card">
                  <h3>AI 预算</h3>

                  <p className="quota-value">
                    {formatUsd(quota.budget.used_usd)}
                    {" / "}
                    {formatUsd(quota.budget.limit_usd)}
                  </p>

                  <div className="quota-bar">
                    <span
                      style={{
                        width: `${usagePercent(
                          Number(
                            quota.budget.used_usd
                          ),
                          Number(
                            quota.budget.limit_usd
                          )
                        )}%`,
                      }}
                    />
                  </div>

                  <p className="quota-foot">
                    剩余{" "}
                    {formatUsd(
                      quota.budget.remaining_usd
                    )}
                  </p>
                </article>

                <p
                  className="quota-note"
                  data-testid="quota-note"
                >
                  {quota.note}
                </p>

                {quota.budget.unresolved_reservations >
                  0 && (
                  <p className="quota-note">
                    有{" "}
                    {
                      quota.budget
                        .unresolved_reservations
                    }{" "}
                    次请求还在结算中，金额可能继续变化。
                  </p>
                )}
              </div>
            )}
          </section>
        ) : view === "reminders" ? (
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