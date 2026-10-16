import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from "react";

type Bot = {
  id: number;
  owner_id: string | null;
  name: string;
  persona: string;
  system_prompt: string;
  expertise: string | null;
  style: string | null;
  api_source: "platform" | "byok";
  avatar: string;
  enabled: boolean;
};

// 头像 key → 风格化图标。故意用 emoji/符号，不用厂商官方 logo（商标）。
const AVATAR_CHOICES: { key: string; label: string; icon: string }[] = [
  { key: "default", label: "默认", icon: "✦" },
  { key: "deepseek", label: "DeepSeek 风", icon: "🐋" },
  { key: "doubao", label: "豆包风", icon: "🫘" },
  { key: "qwen", label: "千问风", icon: "🌐" },
  { key: "kimi", label: "Kimi 风", icon: "🌙" },
  { key: "ernie", label: "文心风", icon: "🅑" },
  { key: "claude", label: "Claude 风", icon: "✳" },
  { key: "gpt", label: "GPT 风", icon: "◎" },
  { key: "gemini", label: "Gemini 风", icon: "◆" },
];

function avatarIcon(key: string): string {
  return (
    AVATAR_CHOICES.find((item) => item.key === key)?.icon ?? "✦"
  );
}

const API_SOURCE_TEXT: Record<string, string> = {
  platform: "平台 API",
  byok: "自带密钥（BYOK）",
};

async function readDetail(
  response: Response,
  fallback: string
): Promise<string> {
  try {
    const value: unknown = await response.json();

    if (
      value &&
      typeof value === "object" &&
      typeof (value as { detail?: unknown }).detail === "string"
    ) {
      return (value as { detail: string }).detail;
    }
  } catch {
    // 解析失败用兜底
  }

  return fallback;
}

type Props = {
  authToken?: string;
};

export default function BotsView({ authToken }: Props) {
  const [bots, setBots] = useState<Bot[]>([]);
  const [defaultId, setDefaultId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [persona, setPersona] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [apiSource, setApiSource] = useState("platform");
  const [avatar, setAvatar] = useState("default");

  const authHeaders = useCallback(
    (): Record<string, string> =>
      authToken
        ? { Authorization: `Bearer ${authToken}` }
        : {},
    [authToken]
  );

  const load = useCallback(async () => {
    if (!authToken) return;

    try {
      const [listRes, defaultRes] = await Promise.all([
        fetch("/api/bots", { headers: authHeaders() }),
        fetch("/api/bots/default", { headers: authHeaders() }),
      ]);

      if (!listRes.ok) {
        throw new Error(
          await readDetail(
            listRes,
            `读取 Bot 失败（${listRes.status}）`
          )
        );
      }

      const listValue: unknown = await listRes.json();

      setBots(
        listValue &&
          typeof listValue === "object" &&
          Array.isArray((listValue as { bots?: unknown }).bots)
          ? ((listValue as { bots: Bot[] }).bots)
          : []
      );

      if (defaultRes.ok) {
        const defaultValue: unknown = await defaultRes.json();

        const bot =
          defaultValue &&
          typeof defaultValue === "object"
            ? (defaultValue as { bot?: Bot | null }).bot
            : null;

        setDefaultId(bot ? bot.id : null);
      }

      setError("");
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "读取 Bot 失败"
      );
    }
  }, [authToken, authHeaders]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!name.trim() || !persona.trim() || !systemPrompt.trim()) {
      setError("名字、天然立场、行为指令都不能为空");
      return;
    }

    setBusy("create");
    setNotice("");

    try {
      const response = await fetch("/api/bots", {
        method: "POST",
        headers: {
          ...authHeaders(),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name: name.trim(),
          persona: persona.trim(),
          system_prompt: systemPrompt.trim(),
          api_source: apiSource,
          avatar,
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

      setName("");
      setPersona("");
      setSystemPrompt("");
      setNotice("Bot 已创建。");
      await load();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "创建失败"
      );
    } finally {
      setBusy(null);
    }
  }

  async function toggle(bot: Bot) {
    setBusy(`toggle-${bot.id}`);

    try {
      const action = bot.enabled ? "disable" : "enable";

      const response = await fetch(
        `/api/bots/${bot.id}/${action}`,
        { method: "POST", headers: authHeaders() }
      );

      if (!response.ok) {
        throw new Error(
          await readDetail(
            response,
            `操作失败（${response.status}）`
          )
        );
      }

      await load();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "操作失败"
      );
    } finally {
      setBusy(null);
    }
  }

  async function makeDefault(bot: Bot) {
    setBusy(`default-${bot.id}`);

    try {
      const response = await fetch(
        `/api/bots/${bot.id}/default`,
        { method: "POST", headers: authHeaders() }
      );

      if (!response.ok) {
        throw new Error(
          await readDetail(
            response,
            `设置失败（${response.status}）`
          )
        );
      }

      setNotice(`「${bot.name}」已设为默认 Bot。`);
      await load();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "设置失败"
      );
    } finally {
      setBusy(null);
    }
  }

  const ownCount = bots.filter(
    (bot) => bot.owner_id !== null
  ).length;

  return (
    <section className="bot-panel" aria-label="Bot 管理">
      <div className="reminder-head">
        <div>
          <h2>Bot 管理</h2>

          <p className="reminder-hint">
            每个 Bot 有独立身份卡（立场 + 行为指令）与独立记忆；
            最多创建 3 个自己的 Bot（已创建 {ownCount}/3）。
            带「站长」标记的是平台预置 Bot，所有人都能用。
          </p>
        </div>

        <button
          type="button"
          onClick={() => void load()}
          aria-label="刷新 Bot 列表"
        >
          刷新
        </button>
      </div>

      {error && (
        <div className="error-box" data-testid="bot-error">
          {error}
        </div>
      )}

      {notice && (
        <div className="notice-box" data-testid="bot-notice">
          {notice}
        </div>
      )}

      <form className="bot-form" onSubmit={create}>
        <label>
          名字
          <input
            aria-label="Bot 名字"
            value={name}
            maxLength={50}
            placeholder="例如：保守派"
            onChange={(event) => setName(event.target.value)}
          />
        </label>

        <label>
          天然立场（persona）
          <input
            aria-label="Bot 立场"
            value={persona}
            maxLength={200}
            placeholder="例如：风险厌恶者，凡事先算代价"
            onChange={(event) => setPersona(event.target.value)}
          />
        </label>

        <label>
          行为指令（system prompt）
          <textarea
            aria-label="Bot 行为指令"
            value={systemPrompt}
            maxLength={4000}
            rows={3}
            placeholder="例如：发言简短，先给结论，再给一句理由。"
            onChange={(event) =>
              setSystemPrompt(event.target.value)
            }
          />
        </label>

        <div className="bot-form-row">
          <label>
            API 来源
            <select
              aria-label="API 来源"
              value={apiSource}
              onChange={(event) =>
                setApiSource(event.target.value)
              }
            >
              <option value="platform">平台 API</option>
              <option value="byok">自带密钥（BYOK）</option>
            </select>
          </label>

          <label>
            头像
            <select
              aria-label="Bot 头像"
              value={avatar}
              onChange={(event) => setAvatar(event.target.value)}
            >
              {AVATAR_CHOICES.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.icon} {item.label}
                </option>
              ))}
            </select>
          </label>

          <button
            type="submit"
            disabled={busy === "create"}
            aria-label="创建 Bot"
          >
            {busy === "create" ? "创建中…" : "创建 Bot"}
          </button>
        </div>
      </form>

      {bots.length === 0 ? (
        <p className="reminder-empty" data-testid="bot-empty">
          还没有 Bot
        </p>
      ) : (
        <ul className="bot-list">
          {bots.map((bot) => (
            <li
              className={`bot-row ${bot.enabled ? "" : "off"}`}
              key={bot.id}
            >
              <div className="bot-avatar">
                {avatarIcon(bot.avatar)}
              </div>

              <div className="bot-main">
                <strong>
                  {bot.name}

                  {bot.owner_id === null && (
                    <small className="bot-tag">站长</small>
                  )}

                  {bot.id === defaultId && (
                    <small className="bot-tag default">默认</small>
                  )}

                  {!bot.enabled && (
                    <small className="bot-tag off">已禁用</small>
                  )}
                </strong>

                <span className="bot-persona">{bot.persona}</span>

                <span className="reminder-time">
                  {API_SOURCE_TEXT[bot.api_source] ??
                    bot.api_source}
                </span>
              </div>

              <div className="reminder-actions">
                {bot.owner_id !== null && (
                  <button
                    type="button"
                    disabled={busy === `toggle-${bot.id}`}
                    aria-label={`${
                      bot.enabled ? "禁用" : "启用"
                    } ${bot.id}`}
                    onClick={() => void toggle(bot)}
                  >
                    {bot.enabled ? "禁用" : "启用"}
                  </button>
                )}

                <button
                  type="button"
                  disabled={
                    busy === `default-${bot.id}` ||
                    bot.id === defaultId ||
                    !bot.enabled
                  }
                  aria-label={`设为默认 ${bot.id}`}
                  onClick={() => void makeDefault(bot)}
                >
                  设为默认
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
