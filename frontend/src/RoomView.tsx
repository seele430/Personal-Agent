import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from "react";

// ---- 类型（对应后端返回） ----

type Room = {
  id: number;
  name: string;
  stances: string[];
  archived: number;
  created_at: number;
  bot_count: number;
};

type RoomBot = {
  id: number;
  name: string;
  persona: string;
  avatar: string;
  activity: number;
};

type RoomMessage = {
  id: number;
  turn_id: string;
  speaker: "user" | "bot" | "host";
  bot_id: number | null;
  role: "core" | "expand" | "object" | "summary" | "decision";
  content: string;
  created_at: number;
};

type Hand = {
  id: number;
  turn_id: string;
  bot_id: number;
  kind: "expand" | "object";
  reason: string;
  status: "pending" | "approved" | "rejected";
};

type AllBot = {
  id: number;
  name: string;
  avatar: string;
};

// 头像 key → 图标（与 BotsView 一致）
function avatarIcon(key: string): string {
  const map: Record<string, string> = {
    default: "✦",
    deepseek: "🐋",
    doubao: "🫘",
    qwen: "🌐",
    kimi: "🌙",
    ernie: "🅑",
    claude: "✳",
    gpt: "◎",
    gemini: "◆",
  };
  return map[key] ?? "✦";
}

const ROLE_TEXT: Record<string, string> = {
  core: "观点",
  expand: "展开",
  object: "质疑",
  summary: "汇总",
  decision: "执行",
};

// @ 触发解析：输入里最后一个 @，且后面没空格（正在输入名字）。
function activeMention(input: string): { query: string } | null {
  const at = input.lastIndexOf("@");
  if (at === -1) return null;
  const after = input.slice(at + 1);
  if (after.includes(" ") || after.includes("\n")) return null;
  return { query: after };
}

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
    // 兜底
  }
  return fallback;
}

type Props = { authToken?: string };

export default function RoomView({ authToken }: Props) {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  // 进入房间后收起房间列表，腾出主区空间（左上角留「展开」）。
  const [sideCollapsed, setSideCollapsed] = useState(false);
  const [members, setMembers] = useState<RoomBot[]>([]);
  const [messages, setMessages] = useState<RoomMessage[]>([]);
  const [hands, setHands] = useState<Hand[]>([]);
  const [allBots, setAllBots] = useState<AllBot[]>([]);

  const [addBotId, setAddBotId] = useState<string>("");

  // 拉 Bot 进房时给它选立场
  const [addStanceOpen, setAddStanceOpen] = useState(false);
  const [pendingBotId, setPendingBotId] = useState("");
  const [chosenStance, setChosenStance] = useState("");
  const [askPreview, setAskPreview] = useState("");

  // 发送后的即时反馈：思考中（省略号）+ 逐字吐字
  const [thinking, setThinking] = useState(false);
  const [reveal, setReveal] = useState<{ id: number; shown: number } | null>(
    null
  );

  // 新建房间弹框（自绘，不用浏览器原生 prompt）
  const [createOpen, setCreateOpen] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [draftStances, setDraftStances] = useState<string[]>([
    "",
    "",
  ]);

  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // 汇总 / 决策
  const [summary, setSummary] = useState<{
    consensus: string;
    disagreement: string;
    options: { label: string; risk: string }[];
  } | null>(null);
  const [decisionReply, setDecisionReply] = useState("");

  const authHeaders = useCallback(
    (): Record<string, string> =>
      authToken
        ? { Authorization: `Bearer ${authToken}` }
        : {},
    [authToken]
  );

  const loadRooms = useCallback(async () => {
    try {
      const res = await fetch("/api/rooms", { headers: authHeaders() });
      if (!res.ok) throw new Error(await readDetail(res, `读取房间失败（${res.status}）`));
      const value: unknown = await res.json();
      setRooms(
        value && typeof value === "object" && Array.isArray((value as { rooms?: unknown }).rooms)
          ? ((value as { rooms: Room[] }).rooms)
          : []
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取房间失败");
    }
  }, [authHeaders]);

  const loadAllBots = useCallback(async () => {
    try {
      const res = await fetch("/api/bots", { headers: authHeaders() });
      if (!res.ok) return;
      const value: unknown = await res.json();
      const bots = value && typeof value === "object" && Array.isArray((value as { bots?: unknown }).bots)
        ? ((value as { bots: AllBot[] }).bots)
        : [];
      setAllBots(bots.filter((b) => b.id !== undefined));
    } catch {
      // 拉不到 bot 列表不致命
    }
  }, [authHeaders]);

  const loadMembers = useCallback(async () => {
    if (selectedId === null) return;
    try {
      const res = await fetch(`/api/rooms/${selectedId}/bots`, { headers: authHeaders() });
      if (!res.ok) throw new Error(await readDetail(res, `读取成员失败（${res.status}）`));
      const value: unknown = await res.json();
      setMembers(
        value && typeof value === "object" && Array.isArray((value as { bots?: unknown }).bots)
          ? ((value as { bots: RoomBot[] }).bots)
          : []
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取成员失败");
    }
  }, [selectedId, authHeaders]);

  const fetchMessages = useCallback(
    async (roomId: number): Promise<RoomMessage[]> => {
      const res = await fetch(`/api/rooms/${roomId}/messages`, {
        headers: authHeaders(),
      });

      if (!res.ok) {
        throw new Error(
          await readDetail(res, `读取消息失败（${res.status}）`)
        );
      }

      const value: unknown = await res.json();

      return value &&
        typeof value === "object" &&
        Array.isArray((value as { messages?: unknown }).messages)
        ? (value as { messages: RoomMessage[] }).messages
        : [];
    },
    [authHeaders]
  );

  const loadMessages = useCallback(async () => {
    if (selectedId === null) return;

    try {
      setMessages(await fetchMessages(selectedId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取消息失败");
    }
  }, [selectedId, fetchMessages]);

  const loadHands = useCallback(async () => {
    if (selectedId === null) return;
    try {
      const res = await fetch(`/api/rooms/${selectedId}/hands`, { headers: authHeaders() });
      if (!res.ok) return;
      const value: unknown = await res.json();
      setHands(
        value && typeof value === "object" && Array.isArray((value as { hands?: unknown }).hands)
          ? ((value as { hands: Hand[] }).hands)
          : []
      );
    } catch {
      // 举手读取失败不打断
    }
  }, [selectedId, authHeaders]);

  useEffect(() => {
    void loadRooms();
    void loadAllBots();
  }, [loadRooms, loadAllBots]);

  useEffect(() => {
    void loadMembers();
    void loadMessages();
    void loadHands();
    setSummary(null);
    setDecisionReply("");
  }, [selectedId, loadMembers, loadMessages, loadHands]);

  function refresh() {
    void loadMembers();
    void loadMessages();
    void loadHands();
    void loadRooms();
  }

  function createRoomClick() {
    setDraftName("");
    setDraftStances(["", ""]);
    setCreateOpen(true);
  }

  // 「用示例填一下」：一键把示例写进输入框，省得每次手打。
  function fillSample() {
    setDraftName("人性本善还是人性本恶");
    setDraftStances(["人性本善", "人性本恶"]);
  }

  async function submitRoom(name: string, stances: string[]) {
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/rooms", {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ name, stances }),
      });
      if (!res.ok) throw new Error(await readDetail(res, `建房间失败（${res.status}）`));
      await loadRooms();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "建房间失败");
    } finally {
      setBusy(false);
    }
  }

  async function submitCreate() {
    const filled = draftStances
      .map((item) => item.trim())
      .filter(Boolean);

    // 至少给两个立场：否则拉 Bot 时弹框里空空如也（用户会一脸问号）。
    const stances =
      filled.length >= 2 ? filled : ["支持", "反对"];

    await submitRoom(draftName.trim() || "新房间", stances);
    setCreateOpen(false);
  }

  async function archiveRoom(room: Room) {
    setBusy(true);
    setError("");
    try {
      const res = await fetch(`/api/rooms/${room.id}/archive`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ archived: true }),
      });
      if (!res.ok) throw new Error(await readDetail(res, `归档失败（${res.status}）`));
      if (selectedId === room.id) setSelectedId(null);
      await loadRooms();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "归档失败");
    } finally {
      setBusy(false);
    }
  }

  async function deleteRoom(room: Room) {
    if (
      !window.confirm(
        `删除房间「${room.name}」？房间里的消息也会一起删掉。`
      )
    ) {
      return;
    }

    setBusy(true);
    setError("");
    try {
      const res = await fetch(`/api/rooms/${room.id}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error(await readDetail(res, `删除失败（${res.status}）`));
      if (selectedId === room.id) setSelectedId(null);
      await loadRooms();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "删除失败");
    } finally {
      setBusy(false);
    }
  }

  // 拉 Bot 进房：先弹框给它选立场（选项来自建房时设的立场）。
  function addBotClick() {
    if (!addBotId) return;

    setPendingBotId(addBotId);
    setChosenStance("");
    setAskPreview("");
    setAddStanceOpen(true);
  }

  async function confirmAddBot(closeModal = true): Promise<boolean> {
    if (selectedId === null || !pendingBotId) return false;

    setBusy(true);
    setError("");

    try {
      const res = await fetch(`/api/rooms/${selectedId}/bots`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          bot_id: Number(pendingBotId),
          stance: chosenStance || null,
        }),
      });

      if (!res.ok) throw new Error(await readDetail(res, `拉进房失败（${res.status}）`));

      setAddBotId("");

      if (closeModal) {
        setAddStanceOpen(false);
      }

      await loadMembers();
      await loadRooms();

      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "拉进房失败");

      return false;
    } finally {
      setBusy(false);
    }
  }

  // 弹框里的「问问 Bot 怎么想」：先拉人，再**只问它**，
  // 不关弹框，把它的回答直接显示在框里。
  async function addAndAsk() {
    const ok = await confirmAddBot(false);

    if (!ok || selectedId === null) return;

    setBusy(true);
    setError("");
    setAskPreview("");

    try {
      const res = await fetch(`/api/rooms/${selectedId}/ask`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          bot_id: Number(pendingBotId),
          word_limit: 120,
        }),
      });

      if (!res.ok) throw new Error(await readDetail(res, `失败（${res.status}）`));

      const data: unknown = await res.json();

      const replies =
        data &&
        typeof data === "object" &&
        Array.isArray((data as { replies?: unknown }).replies)
          ? (data as { replies: { content: string }[] }).replies
          : [];

      setAskPreview(replies[0]?.content ?? "（它这次没说话）");

      await loadMessages();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "失败");
    } finally {
      setBusy(false);
    }
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const text = input.trim();

    if (!text || selectedId === null) return;

    const roomId = selectedId;
    const optimisticId = -Date.now();

    // 乐观更新：先把我的话放进消息流，不等后端。
    setMessages((prev) => [
      ...prev,
      {
        id: optimisticId,
        turn_id: "pending",
        speaker: "user",
        bot_id: null,
        role: "core",
        content: text,
        created_at: Math.floor(Date.now() / 1000),
      },
    ]);
    setInput("");
    setSummary(null);
    setError("");
    setThinking(true);

    try {
      const res = await fetch(`/api/rooms/${roomId}/messages`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });

      if (!res.ok) throw new Error(await readDetail(res, `发送失败（${res.status}）`));

      // 换成真实数据（乐观消息拿到真 id，Bot 回复也在里面）
      const fresh = await fetchMessages(roomId);

      setMessages(fresh);

      // 最新一条 Bot 回复 → 逐字吐出来
      const lastBot = [...fresh]
        .reverse()
        .find((m) => m.speaker === "bot");

      if (lastBot) {
        setReveal({ id: lastBot.id, shown: 0 });
      }

      await loadHands();
    } catch (cause) {
      // 失败就撤掉乐观消息，把文字还给输入框
      setMessages((prev) => prev.filter((m) => m.id !== optimisticId));
      setInput(text);
      setError(cause instanceof Error ? cause.message : "发送失败");
    } finally {
      setThinking(false);
    }
  }

  async function decideHand(hand: Hand, action: "approve" | "reject", unfair = false) {
    setBusy(true);
    try {
      const res = await fetch(`/api/hands/${hand.id}/${action}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ unfair }),
      });
      if (!res.ok) throw new Error(await readDetail(res, `操作失败（${res.status}）`));
      await loadHands();
      await loadMessages();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function summarize() {
    if (selectedId === null) return;
    // 取最后一个 turn_id（最近的用户消息）
    const lastUser = [...messages].reverse().find((m) => m.speaker === "user");
    if (!lastUser) {
      setError("还没有用户消息，先发一句话");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await fetch(`/api/rooms/${selectedId}/turns/${lastUser.turn_id}/summary`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error(await readDetail(res, `汇总失败（${res.status}）`));
      const value: unknown = await res.json();
      setSummary(value as { consensus: string; disagreement: string; options: { label: string; risk: string }[] });
      await loadMessages();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "汇总失败");
    } finally {
      setBusy(false);
    }
  }

  async function decide(option: { label: string; risk: string }) {
    if (selectedId === null) return;
    const lastUser = [...messages].reverse().find((m) => m.speaker === "user");
    if (!lastUser) return;
    setBusy(true);
    setError("");
    try {
      const res = await fetch(`/api/rooms/${selectedId}/turns/${lastUser.turn_id}/decision`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ option: option.label }),
      });
      if (!res.ok) throw new Error(await readDetail(res, `执行失败（${res.status}）`));
      const value: unknown = await res.json();
      setDecisionReply(
        value && typeof value === "object" ? String((value as { reply?: unknown }).reply ?? "") : ""
      );
      await loadMessages();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "执行失败");
    } finally {
      setBusy(false);
    }
  }

  // @ 下拉：输入里最后一个 @ 未结束 → 列出成员
  const mention = activeMention(input);
  const mentionCandidates =
    mention === null
      ? []
      : members.filter((m) => m.name.includes(mention.query));

  function pickMention(name: string) {
    if (mention === null) return;
    const at = input.lastIndexOf("@");
    const next = input.slice(0, at) + "@" + name + " ";
    setInput(next);
  }

  const selectedRoom =
    rooms.find((room) => room.id === selectedId) ?? null;
  const roomStances = selectedRoom?.stances ?? [];

  // 老房间可能没立场：这时也给两个默认选项，别让弹框空着。
  const stanceOptions =
    roomStances.length > 0 ? roomStances : ["支持", "反对"];

  const pendingBotName =
    allBots.find((b) => String(b.id) === pendingBotId)?.name ??
    "这个 Bot";

  // 逐字吐字：约 45 字/秒（比人阅读快一点，但不糊成一团）
  useEffect(() => {
    if (reveal === null) return;

    const target = messages.find((m) => m.id === reveal.id);

    if (!target || reveal.shown >= target.content.length) {
      setReveal(null);
      return;
    }

    const timer = window.setTimeout(() => {
      setReveal((prev) =>
        prev === null ? null : { ...prev, shown: prev.shown + 1 }
      );
    }, 22);

    return () => window.clearTimeout(timer);
  }, [reveal, messages]);

  // 正在逐字的那条消息，只显示前 N 个字
  function displayContent(message: RoomMessage): string {
    if (reveal && reveal.id === message.id) {
      return message.content.slice(0, reveal.shown);
    }

    return message.content;
  }

  const pendingHands = hands.filter((h) => h.status === "pending");
  const memberName = (id: number | null) =>
    id === null ? "主持人" : members.find((m) => m.id === id)?.name ?? `Bot#${id}`;

  return (
    <section className="room-panel" aria-label="聊天室">
      <div className="room-layout">
        {/* 左：房间列表 —— 进入房间后收起，由左上角「展开」恢复 */}
        {!sideCollapsed && (
        <aside className="room-side">
          <div className="reminder-head">
            <h2>聊天室</h2>
          </div>

          <div className="room-create">
            <button
              type="button"
              disabled={busy}
              onClick={createRoomClick}
              aria-label="建房间"
            >
              ＋ 新建房间
            </button>
          </div>

          {rooms.length === 0 ? (
            <p className="reminder-empty">还没有房间</p>
          ) : (
            <ul className="room-list">
              {rooms.map((room) => (
                <li key={room.id}>
                  <button
                    type="button"
                    className={`room-item ${
                      room.id === selectedId ? "selected" : ""
                    }`}
                    onClick={() => {
                      setSelectedId(room.id);
                      setSideCollapsed(true);
                    }}
                  >
                    <span className="room-item-name">
                      {room.name}
                    </span>
                    <small>
                      {room.bot_count} 个 Bot
                      {room.stances.length > 0
                        ? ` · ${room.stances.length} 个立场`
                        : ""}
                    </small>
                  </button>

                  <div className="room-item-actions">
                    <button
                      type="button"
                      disabled={busy}
                      title="归档"
                      aria-label={`归档房间 ${room.id}`}
                      onClick={() => void archiveRoom(room)}
                    >
                      归档
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      title="删除"
                      aria-label={`删除房间 ${room.id}`}
                      onClick={() => void deleteRoom(room)}
                    >
                      删除
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </aside>
        )}

        {/* 右：房间详情 */}
        <div className="room-main">
          {sideCollapsed && (
            <button
              type="button"
              className="room-expand"
              onClick={() => setSideCollapsed(false)}
              aria-label="展开房间列表"
            >
              ☰ 房间列表
            </button>
          )}

          {selectedId === null ? (
            <p className="reminder-empty">选一个房间，或新建一个</p>
          ) : (
            <>
              <div className="room-toolbar">
                <div className="member-chips">
                  {members.map((m) => (
                    <span className="member-chip" key={m.id}>
                      {avatarIcon(m.avatar)} {m.name}
                      <small>活跃 {m.activity}</small>
                    </span>
                  ))}
                </div>

                <div className="room-addbot">
                  <select aria-label="选择要拉进房的 Bot" value={addBotId} onChange={(e) => setAddBotId(e.target.value)}>
                    <option value="">拉 Bot 进房…</option>
                    {allBots.filter((b) => !members.some((m) => m.id === b.id)).map((b) => (
                      <option key={b.id} value={b.id}>{b.name}</option>
                    ))}
                  </select>

                  <button type="button" disabled={busy || !addBotId} onClick={addBotClick}>
                    拉进房
                  </button>
                </div>
              </div>

              {error && <div className="error-box" data-testid="room-error">{error}</div>}

              <div className="room-messages">
                {messages.length === 0 && !thinking ? (
                  <p className="reminder-empty">房间还是空的，发句话开始讨论</p>
                ) : (
                  <>
                    {messages.map((m) => {
                      const speakerBot = members.find(
                        (x) => x.id === m.bot_id
                      );

                      return (
                        <article
                          className={`room-msg ${m.speaker}`}
                          key={m.id}
                        >
                          <div className="room-msg-avatar">
                            {m.speaker === "user"
                              ? "我"
                              : m.speaker === "host"
                                ? "主"
                                : avatarIcon(
                                    speakerBot?.avatar ?? "default"
                                  )}
                          </div>

                          <div className="room-msg-main">
                            <div className="room-msg-head">
                              <strong>
                                {m.speaker === "user"
                                  ? "你"
                                  : memberName(m.bot_id)}
                              </strong>

                              {m.role !== "core" && (
                                <small>
                                  {ROLE_TEXT[m.role] ?? m.role}
                                </small>
                              )}
                            </div>

                            <div className="room-msg-body">
                              {displayContent(m)}
                            </div>
                          </div>
                        </article>
                      );
                    })}

                    {/* Bot 正在想：先跳动的省略号，答案到了再逐字吐 */}
                    {thinking && (
                      <article className="room-msg bot thinking">
                        <div className="room-msg-avatar">…</div>

                        <div className="room-msg-main">
                          <div className="room-msg-head">
                            <strong>正在思考</strong>
                          </div>

                          <div className="room-msg-body typing">
                            <span />
                            <span />
                            <span />
                          </div>
                        </div>
                      </article>
                    )}
                  </>
                )}
              </div>

              {/* 举手区 */}
              {pendingHands.length > 0 && (
                <div className="hand-panel">
                  <h3>举手（{pendingHands.length}）</h3>
                  {pendingHands.map((h) => (
                    <div className="hand-row" key={h.id}>
                      <div>
                        <strong>{memberName(h.bot_id)}</strong>
                        <span>{h.kind === "expand" ? "想展开" : "想追问"}</span>
                        <p>{h.reason}</p>
                      </div>
                      <div className="reminder-actions">
                        <button type="button" disabled={busy} onClick={() => void decideHand(h, "approve")}>批准</button>
                        <button type="button" disabled={busy} onClick={() => void decideHand(h, "reject", false)}>拒绝</button>
                        <button type="button" disabled={busy} onClick={() => void decideHand(h, "reject", true)}>
                          拒绝并扣分
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* 汇总区 */}
              <div className="summary-bar">
                <button type="button" disabled={busy} onClick={() => void summarize()}>
                  主持人汇总
                </button>
                <button type="button" onClick={() => refresh()} aria-label="刷新">刷新</button>
              </div>

              {summary && (
                <div className="summary-panel">
                  <p><strong>共识：</strong>{summary.consensus}</p>
                  <p><strong>分歧：</strong>{summary.disagreement}</p>
                  {summary.options.length > 0 && (
                    <ul className="decision-list">
                      {summary.options.map((o) => (
                        <li key={o.label}>
                          <button type="button" disabled={busy} onClick={() => void decide(o)}>
                            {o.label}
                          </button>
                          <span>{o.risk}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  {decisionReply && <p className="decision-reply">执行结果：{decisionReply}</p>}
                </div>
              )}

              {/* 输入框 + @ 下拉 */}
              <div className="preset-wrap">
                {mentionCandidates.length > 0 && (
                  <ul className="preset-list">
                    {mentionCandidates.map((m) => (
                      <li key={m.id}>
                        <button type="button" onMouseDown={(e) => { e.preventDefault(); pickMention(m.name); }}>
                          {avatarIcon(m.avatar)} {m.name}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                <form className="room-input" onSubmit={sendMessage}>
                  <input
                    aria-label="输入讨论内容"
                    value={input}
                    placeholder="输入讨论内容，打 @ 点名某个 Bot…"
                    onChange={(e) => setInput(e.target.value)}
                  />

                  <button type="submit" disabled={busy || !input.trim()}>发送</button>
                </form>
              </div>
            </>
          )}
        </div>
      </div>

      {/* 新建房间弹框（自绘：房间名 + 立场 A/B/＋） */}
      {createOpen && (
        <div
          className="modal-mask"
          onClick={() => setCreateOpen(false)}
        >
          <div
            className="modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-head">
              <h3>新建房间</h3>

              <button
                type="button"
                className="sample-fill"
                onClick={fillSample}
              >
                用示例填一下
              </button>
            </div>

            <label className="modal-label">
              题目（可留空）
              <input
                aria-label="房间题目"
                value={draftName}
                placeholder="例如：人性本善还是人性本恶"
                onChange={(e) => setDraftName(e.target.value)}
              />
            </label>

            <p className="modal-hint">
              立场（至少两个；之后拉 Bot 进房时给它选一个）
            </p>

            {draftStances.map((stance, index) => (
              <div className="stance-row" key={index}>
                <input
                  aria-label={`立场 ${index + 1}`}
                  value={stance}
                  placeholder={
                    index === 0
                      ? "立场 A，例如：人性本善"
                      : index === 1
                        ? "立场 B，例如：人性本恶"
                        : `立场 ${index + 1}`
                  }
                  onChange={(e) => {
                    const next = [...draftStances];
                    next[index] = e.target.value;
                    setDraftStances(next);
                  }}
                />

                {draftStances.length > 2 && (
                  <button
                    type="button"
                    aria-label={`删除立场 ${index + 1}`}
                    onClick={() =>
                      setDraftStances(
                        draftStances.filter(
                          (_, i) => i !== index
                        )
                      )
                    }
                  >
                    ×
                  </button>
                )}
              </div>
            ))}

            <button
              type="button"
              className="stance-add"
              onClick={() =>
                setDraftStances([...draftStances, ""])
              }
            >
              ＋ 加一个立场
            </button>

            <div className="modal-actions">
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
              >
                取消
              </button>
              <button
                type="button"
                className="primary"
                disabled={busy}
                onClick={() => void submitCreate()}
              >
                创建
              </button>
            </div>
          </div>
        </div>
      )}
      {/* 拉 Bot 进房：给它选立场（选项来自建房时设的立场） */}
      {addStanceOpen && (
        <div
          className="modal-mask"
          onClick={() => setAddStanceOpen(false)}
        >
          <div
            className="modal"
            onClick={(event) => event.stopPropagation()}
          >
            <h3>给这个 Bot 选立场</h3>

            <p className="modal-hint">
              议题「{selectedRoom?.name ?? "未命名"}」
              {roomStances.length > 0 ? " 的立场：" : "（先给它定一个）："}
            </p>

            <div
              className={`stance-choices ${
                askPreview ? "locked" : ""
              }`}
            >
              {stanceOptions.map((stance, index) => (
                <button
                  key={stance}
                  type="button"
                  className={`stance-choice c${index % 3} ${
                    chosenStance === stance ? "chosen" : ""
                  }`}
                  onClick={() => setChosenStance(stance)}
                >
                  <span className="stance-key">
                    {String.fromCharCode(97 + index)}
                  </span>
                  <span>{stance}</span>
                </button>
              ))}
            </div>

            {roomStances.length === 0 && (
              <p className="modal-hint">
                这个房间还没设立场（建房时填的立场会显示在这里），
                先给这个 Bot 定一个。
              </p>
            )}

            {/* 问过之后，它的回答直接留在框里（不关框） */}
            {askPreview && (
              <div className="ask-preview">
                <strong>{pendingBotName} 的想法</strong>
                <p>{askPreview}</p>
              </div>
            )}

            <div className="modal-actions">
              <button
                type="button"
                onClick={() => setAddStanceOpen(false)}
              >
                取消
              </button>
              <button
                type="button"
                disabled={busy || !chosenStance}
                onClick={() => void confirmAddBot()}
              >
                加进房间
              </button>
              <button
                type="button"
                className="primary"
                disabled={busy || !chosenStance}
                onClick={() => void addAndAsk()}
              >
                问问 Bot 怎么想
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
