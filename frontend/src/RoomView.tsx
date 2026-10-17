import {
  Fragment,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { cleanForSpeech, prewarmSpeech, speakText, stopSpeech } from "./lib/tts";

// ---- 类型（对应后端返回） ----

type Room = {
  id: number;
  name: string;
  stances: string[];
  host_bot_id: number | null;
  stage: string;
  stage_started_at: number | null;
  archived: number;
  created_at: number;
  bot_count: number;
};

// 质询阶段限时（秒）
const CROSSFIRE_SECONDS = 180;

// 辩论流程：顺序即推进顺序
const STAGES = [
  { key: "opening", label: "开场" },
  { key: "statement", label: "立论" },
  { key: "rebuttal", label: "驳论" },
  { key: "crossfire", label: "质询" },
  { key: "free", label: "交锋" },
  { key: "closing", label: "结辩" },
  { key: "vote", label: "投票" },
  { key: "review", label: "复盘" },
] as const;

function stageLabel(key: string | null | undefined): string {
  return STAGES.find((item) => item.key === key)?.label ?? "讨论";
}

type RoomBot = {
  id: number;
  name: string;
  persona: string;
  avatar: string;
  voice: string;
  stance: string | null;
  activity: number;
};

type RoomMessage = {
  id: number;
  turn_id: string;
  speaker: "user" | "bot" | "host";
  bot_id: number | null;
  role: "core" | "expand" | "object" | "summary" | "decision";
  stage: string | null;
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

  // 房间主持人：默认用「对话」那边的默认助手
  const [defaultBotId, setDefaultBotId] = useState<number | null>(null);
  const [defaultBotName, setDefaultBotName] = useState("");

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
  // 串行吐字队列：一条说完，停一下，再说下一条
  const [revealQueue, setRevealQueue] = useState<number[]>([]);

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
  // 结算：站对边的不扣，其余扣这个数（默认 30）
  const [penalty, setPenalty] = useState(30);

  // 结辩后的评委打分（每个辩手 0-10，默认 5）
  const [scoreDraft, setScoreDraft] = useState<Record<number, number>>({});
  const [scoreDone, setScoreDone] = useState("");

  // 投票汇总（按立场加权）
  const [voteTally, setVoteTally] = useState<
    { stance: string; weighted: number; votes: number }[]
  >([]);

  // 已经投过哪一边：投了就锁按钮，别让人反复点刷屏
  const [votedStance, setVotedStance] = useState<string | null>(null);

  // 语音：开关 + 已朗读过的消息（避免重复读）。默认开着。
  const [audioOn, setAudioOn] = useState(true);
  const spokenRef = useRef<Set<number>>(new Set());

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

  // 默认助手 = 新房子的主持人（它只裁判、不表态）
  const loadDefaultBot = useCallback(async () => {
    try {
      const res = await fetch("/api/bots/default", {
        headers: authHeaders(),
      });

      if (!res.ok) return;

      const value: unknown = await res.json();

      const bot =
        value && typeof value === "object"
          ? (value as { bot?: { id?: number; name?: string } }).bot
          : null;

      if (bot?.id) {
        setDefaultBotId(bot.id);
        setDefaultBotName(bot.name ?? "");
      }
    } catch {
      // 拿不到就没主持人，不致命
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
    void loadDefaultBot();
  }, [loadRooms, loadAllBots, loadDefaultBot]);

  useEffect(() => {
    void loadMembers();
    void loadMessages();
    void loadHands();
    setSummary(null);
    setDecisionReply("");
  }, [selectedId, loadMembers, loadMessages, loadHands]);

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
        body: JSON.stringify({
          name,
          stances,
          host_bot_id: defaultBotId,
        }),
      });

      if (!res.ok) throw new Error(await readDetail(res, `建房间失败（${res.status}）`));

      const room = (await res.json()) as { id?: number };

      await loadRooms();

      // 新房间：主持人先开场，辩手再依次开篇立论
      if (typeof room?.id === "number") {
        setSelectedId(room.id);

        await runOpening(room.id);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "建房间失败");
    } finally {
      setBusy(false);
    }
  }

  // 开场：主持人先讲规矩，辩手逐个立论（比较慢，显示思考中）
  async function runOpening(roomId: number) {
    setThinking(true);
    setError("");

    try {
      const res = await fetch(`/api/rooms/${roomId}/opening`, {
        method: "POST",
        headers: authHeaders(),
      });

      if (!res.ok) {
        throw new Error(
          await readDetail(res, `开场失败（${res.status}）`)
        );
      }

      const fresh = await fetchMessages(roomId);

      // 开场词也要走逐字队列：否则它不逐字、reveal 恒为 null，
      // 播放 effect 永远不触发 —— 表现就是「建房后没声音」。
      queueNewBots(fresh, messages);
      setMessages(fresh);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "开场失败");
    } finally {
      setThinking(false);
    }
  }

  async function submitCreate() {
    const filled = draftStances
      .map((item) => item.trim())
      .filter(Boolean);

    // 至少给两个立场：否则拉 Bot 时弹框里空空如也（用户会一脸问号）。
    const stances =
      filled.length >= 2 ? filled : ["支持", "反对"];

    // 辩题必填：留空会让主持人把「新房间」当议题，整场跑得莫名其妙
    const name = draftName.trim();

    if (!name) {
      setError("先填一个辩题：辩论室要靠它当讨论主题");
      return;
    }

    // 通用规则：建完房要等主持人开场（慢），所以先关框，
    // 让消息区的「省略号」承担等待反馈。
    setCreateOpen(false);
    setDraftName("");
    setDraftStances(["", ""]);

    await submitRoom(name, stances);
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

  async function confirmAddBot(): Promise<boolean> {
    if (selectedId === null || !pendingBotId) return false;

    const roomId = selectedId;

    // 通用规则：要等模型就**先关框**，别让弹框卡着；
    // 用消息区的「省略号」告诉用户正在等。
    setAddBotId("");
    setAddStanceOpen(false);
    setAskPreview("");
    setError("");
    setBusy(true);
    setThinking(true);

    try {
      const res = await fetch(`/api/rooms/${roomId}/bots`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          bot_id: Number(pendingBotId),
          stance: chosenStance || null,
        }),
      });

      if (!res.ok) throw new Error(await readDetail(res, `拉进房失败（${res.status}）`));

      // 它进房就立论了（后端已经把立论写进消息流）
      await loadMembers();
      await loadRooms();

      const fresh = await fetchMessages(roomId);

      queueNewBots(fresh, messages);
      setMessages(fresh);

      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "拉进房失败");

      return false;
    } finally {
      setThinking(false);
      setBusy(false);
    }
  }

  // 弹框里的「问问 Bot 怎么想」：**只问它倾向哪一边**，
  // 不拉进房、不生成长篇立论 —— 用户要的是结论，不是噪音。
  async function addAndAsk() {
    if (selectedId === null || !addBotId) return;

    setBusy(true);
    setError("");
    setAskPreview("");

    try {
      const res = await fetch(`/api/rooms/${selectedId}/lean`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ bot_id: Number(addBotId) }),
      });

      if (!res.ok) {
        throw new Error(
          await readDetail(res, `问它失败（${res.status}）`)
        );
      }

      const value: unknown = await res.json();

      setAskPreview(
        value && typeof value === "object"
          ? String((value as { lean?: unknown }).lean ?? "")
          : ""
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "问它失败");
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
        stage: selectedRoom?.stage ?? "opening",
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

      queueNewBots(fresh, messages);
      setMessages(fresh);

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

  // 投票：选一边（房主 1.5 票），前端顺手把汇总拉回来
  async function castVote(stance: string) {
    if (selectedId === null) return;

    // 已经投过就不再接受第二次点击（按钮也锁了，这里再兜一层）
    if (votedStance !== null) return;

    setBusy(true);
    setError("");

    try {
      const res = await fetch(`/api/rooms/${selectedId}/vote`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ stance }),
      });

      if (!res.ok) {
        throw new Error(
          await readDetail(res, `投票失败（${res.status}）`)
        );
      }

      setMessages(await fetchMessages(selectedId));
      setVotedStance(stance);

      const tallyRes = await fetch(`/api/rooms/${selectedId}/votes`, {
        headers: authHeaders(),
      });

      if (tallyRes.ok) {
        const data: unknown = await tallyRes.json();

        setVoteTally(
          data &&
            typeof data === "object" &&
            Array.isArray((data as { tally?: unknown }).tally)
            ? (
                data as {
                  tally: {
                    stance: string;
                    weighted: number;
                    votes: number;
                  }[];
                }
              ).tally
            : []
        );
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "投票失败");
    } finally {
      setBusy(false);
    }
  }

  // 打分：结辩之后由用户给辩手评分（房主权重 1.5）
  async function submitScores() {
    if (selectedId === null) return;

    const scores = members
      .filter((m) => m.id !== selectedRoom?.host_bot_id)
      .map((m) => ({
        bot_id: m.id,
        score: Math.max(
          0,
          Math.min(10, scoreDraft[m.id] ?? 5)
        ),
      }));

    if (scores.length === 0) return;

    setBusy(true);
    setError("");
    setScoreDone("");

    try {
      const res = await fetch(`/api/rooms/${selectedId}/scores`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ scores }),
      });

      if (!res.ok) {
        throw new Error(
          await readDetail(res, `打分失败（${res.status}）`)
        );
      }

      setScoreDone("已提交");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "打分失败");
    } finally {
      setBusy(false);
    }
  }

  // 推进到下一个阶段，并生成该阶段内容（慢，所以先转省略号）
  async function advanceStage() {
    if (selectedId === null || nextStage === null) return;

    // 换阶段立刻掐掉正在播的声音：不然上一段会念到下一阶段里
    stopSpeech();

    // 先把阶段切过去：分割线立刻出现，用户知道进入了新阶段
    setOptimisticStage(nextStage.key);
    setBusy(true);
    setError("");
    setThinking(true);

    try {
      const res = await fetch(`/api/rooms/${selectedId}/advance`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ stage: nextStage.key }),
      });

      if (!res.ok) {
        throw new Error(
          await readDetail(res, `推进失败（${res.status}）`)
        );
      }

      await loadRooms();

      const fresh = await fetchMessages(selectedId);

      queueNewBots(fresh, messages);
      setMessages(fresh);

      // 消息已经带上新阶段的分割线了，乐观那条可以撤
      setOptimisticStage(null);

      await loadHands();
    } catch (cause) {
      // 推进失败：撤掉乐观阶段，别让进度条停在假的位置
      setOptimisticStage(null);
      setError(cause instanceof Error ? cause.message : "推进失败");
    } finally {
      setThinking(false);
      setBusy(false);
    }
  }

  // 交锋回合：点一次，轮到的那位说一条
  async function continueFreeTurn() {
    if (selectedId === null) return;

    // 打断正在播的声音，直接出下一位
    stopSpeech();

    setBusy(true);
    setError("");
    setThinking(true);

    try {
      const res = await fetch(`/api/rooms/${selectedId}/turn`, {
        method: "POST",
        headers: authHeaders(),
      });

      if (!res.ok) {
        throw new Error(
          await readDetail(res, `交锋失败（${res.status}）`)
        );
      }

      const fresh = await fetchMessages(selectedId);

      queueNewBots(fresh, messages);
      setMessages(fresh);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "交锋失败");
    } finally {
      setThinking(false);
      setBusy(false);
    }
  }

  async function decideHand(hand: Hand, action: "approve" | "reject", unfair = false) {
    setBusy(true);
    setError("");

    // 批准展开要等模型，用省略号顶着（拒绝是本地操作，不用等）
    if (action === "approve") {
      setThinking(true);
    }

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
      setThinking(false);
      setBusy(false);
    }
  }

  async function decide(option: { label: string; risk: string }) {
    if (selectedId === null) return;
    const lastUser = [...messages].reverse().find((m) => m.speaker === "user");
    if (!lastUser) return;
    setBusy(true);
    setError("");
    setThinking(true);
    try {
      const res = await fetch(`/api/rooms/${selectedId}/turns/${lastUser.turn_id}/decision`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          option: option.label,
          penalty,
        }),
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
      setThinking(false);
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

  // 推进时先本地切阶段：分割线立刻出现，不用等模型
  const [optimisticStage, setOptimisticStage] = useState<string | null>(
    null
  );

  const currentStage =
    optimisticStage ?? selectedRoom?.stage ?? "opening";

  // 换房间就别带着上一个房间的乐观阶段
  useEffect(() => {
    setOptimisticStage(null);
  }, [selectedId]);

  // 当前走到流程的第几步（进度条用）
  const stageIndex = Math.max(
    0,
    STAGES.findIndex((item) => item.key === currentStage)
  );

  const nextStage =
    stageIndex < STAGES.length - 1 ? STAGES[stageIndex + 1] : null;

  // 能不能开始"对抗"：至少两个 Bot、且**立场要分歧**。
  // 两个都站同一边的话，驳论/质询/交锋根本无从谈起。
  const debateSides = new Set(
    members
      .filter(
        (m) =>
          m.id !== selectedRoom?.host_bot_id && Boolean(m.stance)
      )
      .map((m) => m.stance)
  );

  const canDebate = debateSides.size >= 2;

  const needsDebateStage =
    nextStage !== null &&
    ["rebuttal", "crossfire", "free", "closing", "vote"].includes(
      nextStage.key
    );

  // 质询限时：3 分钟倒计时（电子钟）——用户要求放"交锋"阶段
  const [nowTick, setNowTick] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(
      () => setNowTick(Date.now()),
      1000
    );

    return () => window.clearInterval(timer);
  }, []);

  const crossfireRemaining =
    selectedRoom?.stage === "free"
      ? Math.max(
          0,
          CROSSFIRE_SECONDS -
            Math.floor(
              nowTick / 1000 -
                (selectedRoom.stage_started_at ?? 0)
            )
        )
      : null;

  const cdMinutes =
    crossfireRemaining === null
      ? 0
      : Math.floor(crossfireRemaining / 60);

  const cdSeconds =
    crossfireRemaining === null ? 0 : crossfireRemaining % 60;

  // 交锋倒计时归零：直接进结辩。用 ref 防重，别一秒触发好几次。
  const autoAdvancedRef = useRef(false);

  useEffect(() => {
    if (currentStage !== "free") {
      autoAdvancedRef.current = false;
      return;
    }

    if (crossfireRemaining === null || crossfireRemaining > 0) return;
    if (autoAdvancedRef.current || busy) return;

    autoAdvancedRef.current = true;
    void advanceStage();
  }, [crossfireRemaining, currentStage, busy]);

  // 老房间可能没立场：这时也给两个默认选项，别让弹框空着。
  const stanceOptions =
    roomStances.length > 0 ? roomStances : ["支持", "反对"];

  // 逐字吐字：约 25 字/秒（慢一点，不眼花）；一条说完停 450ms 再说下一条
  useEffect(() => {
    if (reveal === null) {
      if (revealQueue.length === 0) return;

      const [nextId, ...rest] = revealQueue;

      setRevealQueue(rest);

      const timer = window.setTimeout(() => {
        setReveal({ id: nextId, shown: 0 });
      }, 450);

      return () => window.clearTimeout(timer);
    }

    const target = messages.find((m) => m.id === reveal.id);

    if (!target || reveal.shown >= target.content.length) {
      setReveal(null);
      return;
    }

    const timer = window.setTimeout(() => {
      setReveal((prev) =>
        prev === null ? null : { ...prev, shown: prev.shown + 1 }
      );
    }, 30);

    return () => window.clearTimeout(timer);
  }, [reveal, revealQueue, messages]);

  // 把「这一批新出现的 Bot / 主持人发言」排进逐条吐字队列
  function queueNewBots(fresh: RoomMessage[], before: RoomMessage[]) {
    const seen = new Set(before.map((m) => m.id));

    const ids = fresh
      .filter(
        (m) =>
          (m.speaker === "bot" || m.speaker === "host") &&
          !seen.has(m.id)
      )
      .map((m) => m.id);

    if (ids.length > 0) {
      setRevealQueue((prev) => [...prev, ...ids]);
    }
  }

  // 刚打开声音时，把现有消息全部记为「已读」：
  // 否则会去补播最后一条（可能是上一个阶段的立论），听着像穿帮。
  useEffect(() => {
    if (!audioOn) return;

    for (const item of messages) {
      if (item.speaker === "bot" || item.speaker === "host") {
        spokenRef.current.add(item.id);
      }
    }
    // 只在 audioOn 变化时执行一次，不跟 messages 走
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [audioOn]);

  // 预热：新消息一到就合成（不播），把 3 秒合成塞进逐字显示的时间里
  useEffect(() => {
    if (!audioOn) return;

    const latest = [...messages]
      .reverse()
      .find((m) => m.speaker === "bot" || m.speaker === "host");

    if (!latest) return;

    const voiceKey =
      members.find((m) => m.id === latest.bot_id)?.voice ?? "female-1";

    prewarmSpeech(latest.content, voiceKey);
  }, [messages, audioOn, members]);

  // 刚刚逐字完的是哪一条（逐字结束时用它决定播什么）
  const lastRevealedIdRef = useRef<number | null>(null);

  // 播放：按「谁刚显示完」+「队列是否清空」双条件判断。
  //
  // 之前只靠 reveal 的 null 边沿，某些路径（开场词、投票主持词）
  // 会因为时序差异错过那一刻，表现就是彻底没声音。现在多一条兜底：
  // 队列清空且没有正在逐字的消息时，补播最后一条没读过的。
  useEffect(() => {
    if (!audioOn) return;

    const play = (id: number) => {
      if (spokenRef.current.has(id)) return;

      const message = messages.find((item) => item.id === id);

      if (!message) return;

      spokenRef.current.add(id);

      const voiceKey =
        members.find((m) => m.id === message.bot_id)?.voice ?? "female-1";

      // 语音失败要看得见：以前是静默回退到浏览器语音，
      // 而 WebView2 里语音数量是 0 —— 表现就是"莫名没声音"。
      void speakText(message.content, voiceKey, 0.9).then((result) => {
        if (!result.ok) {
          setError(`语音播报失败：${result.error ?? "未知原因"}`);
        }
      });
    };

    if (reveal !== null) {
      // 正在逐字：记下它，等它结束
      lastRevealedIdRef.current = reveal.id;
      return;
    }

    // 队列里还有没显示的：再等等
    if (revealQueue.length > 0) return;

    const pending = lastRevealedIdRef.current;

    if (pending !== null) {
      lastRevealedIdRef.current = null;
      play(pending);
      return;
    }

    // 兜底：没有任何逐字过程也要能出声（开场词这类）
    const latest = [...messages]
      .reverse()
      .find((m) => m.speaker === "bot" || m.speaker === "host");

    if (latest) {
      const timer = window.setTimeout(() => play(latest.id), 250);
      return () => window.clearTimeout(timer);
    }

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal, revealQueue, messages, audioOn]);

  // 去掉 Markdown 装饰符：`**` 显示成星号很难看，朗读出来更蠢
  function stripMarkdown(raw: string): string {
    return cleanForSpeech(raw);
  }

  // 正在逐字的那条消息，只显示前 N 个字
  function displayContent(message: RoomMessage): string {
    const text =
      reveal && reveal.id === message.id
        ? message.content.slice(0, reveal.shown)
        : message.content;

    return stripMarkdown(text);
  }

  // 立场 → 配色档位（a 淡红 / b 淡蓝 / c 淡黄）
  function stanceClass(botId: number | null): string {
    const bot = members.find((item) => item.id === botId);

    if (!bot?.stance) return "";

    const index = roomStances.indexOf(bot.stance);

    return index < 0 ? "" : `stance-c${index % 3}`;
  }

  // 主持人复盘存的是 JSON：渲染成可读的段落，别直接吐原始字符串
  function renderSummaryBody(raw: string) {
    try {
      const parsed = JSON.parse(raw) as {
        consensus?: string;
        disagreement?: string;
        options?: { label?: string; risk?: string }[];
      };

      const options = parsed.options ?? [];

      return (
        <div className="summary-inline">
          <p>
            <strong>共识：</strong>
            {parsed.consensus || "（暂无）"}
          </p>
          <p>
            <strong>分歧：</strong>
            {parsed.disagreement || "（暂无）"}
          </p>

          {options.length > 0 && (
            <ul>
              {options.map((option, index) => (
                <li key={index}>
                  <strong>{option.label || `选项 ${index + 1}`}</strong>
                  {option.risk ? `（代价：${option.risk}）` : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
      );
    } catch {
      return raw;
    }
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
                  {members.map((m) => {
                    const isHost = m.id === selectedRoom?.host_bot_id;

                    return (
                      <span
                        className={`member-chip ${
                          isHost ? "host" : ""
                        }`}
                        key={m.id}
                      >
                        {avatarIcon(m.avatar)} {m.name}

                        {/* 主持人不算活跃值：它是裁判，不是辩手 */}
                        <small>
                          {isHost ? "主持人" : `活跃 ${m.activity}`}
                        </small>
                      </span>
                    );
                  })}
                </div>

                <div className="room-addbot">
                  <select
                    aria-label="选择要拉进房的 Bot"
                    value={addBotId}
                    // 只有「立论」阶段能拉人：立论之后阵容就定了
                    disabled={busy || currentStage !== "statement"}
                    onChange={(e) => setAddBotId(e.target.value)}
                  >
                    <option value="">
                      {currentStage === "statement"
                        ? "拉 bot 进房…"
                        : "（立论阶段才能拉 bot）"}
                    </option>
                    {allBots.filter((b) => !members.some((m) => m.id === b.id)).map((b) => (
                      <option key={b.id} value={b.id}>{b.name}</option>
                    ))}
                  </select>

                  <button
                    type="button"
                    disabled={
                      busy ||
                      !addBotId ||
                      currentStage !== "statement"
                    }
                    onClick={addBotClick}
                  >
                    拉进房
                  </button>
                </div>

                {/* 语音开关：开了就按各自的音色朗读 Bot 发言 */}
                <button
                  type="button"
                  className={`audio-toggle ${audioOn ? "on" : ""}`}
                  aria-label={audioOn ? "关闭语音播报" : "开启语音播报"}
                  title={audioOn ? "语音播报：开" : "语音播报：关"}
                  onClick={() => {
                    setAudioOn((prev) => {
                      if (prev) {
                        stopSpeech();
                      }

                      return !prev;
                    });
                  }}
                >
                  {audioOn ? "🔊" : "🔈"}
                </button>
              </div>

              {/* 流程进度：走过的亮绿，没走过的亮黄 */}
              <div className="stage-bar" aria-label="辩论流程">
                {STAGES.map((item, index) => (
                  <div className="stage-seg" key={item.key}>
                    <div
                      className={`stage-fill ${
                        index <= stageIndex ? "done" : "pending"
                      }`}
                    />
                    <span
                      className={`stage-label ${
                        index === stageIndex ? "current" : ""
                      }`}
                    >
                      {item.label}
                    </span>
                  </div>
                ))}
              </div>

              {/* 质询限时：左分右秒的电子钟 */}
              {crossfireRemaining !== null && (
                <div
                  className="countdown"
                  aria-label="质询剩余时间"
                  data-testid="crossfire-countdown"
                >
                  <span className="cd-num">
                    {String(cdMinutes).padStart(2, "0")}
                  </span>
                  <span className="cd-sep">:</span>
                  <span className="cd-num">
                    {String(cdSeconds).padStart(2, "0")}
                  </span>
                </div>
              )}

              {error && <div className="error-box" data-testid="room-error">{error}</div>}

              <div className="room-messages">
                {messages.length === 0 && !thinking ? (
                  <p className="reminder-empty">房间还是空的，发句话开始讨论</p>
                ) : (
                  <>
                    {messages.map((m, index) => {
                      const speakerBot = members.find(
                        (x) => x.id === m.bot_id
                      );

                      // 阶段变了就插一条分割线
                      const prev = messages[index - 1];
                      const showDivider =
                        !prev ||
                        (prev.stage ?? "") !== (m.stage ?? "");

                      return (
                        <Fragment key={m.id}>
                          {showDivider && (
                            <div className="stage-divider">
                              <span>{stageLabel(m.stage)}</span>
                            </div>
                          )}

                          {/* 用户 @ 提问：上方单独一条「追问」分割线 */}
                          {m.speaker === "user" && m.content.includes("@") && (
                            <div className="stage-divider">
                              <span>追问</span>
                            </div>
                          )}

                          <article
                            className={`room-msg ${m.speaker} ${stanceClass(
                              m.bot_id
                            )}`}
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
                              {m.role === "summary"
                                ? renderSummaryBody(m.content)
                                : displayContent(m)}
                            </div>
                          </div>
                          </article>
                        </Fragment>
                      );
                    })}

                    {/* 刚点了「进入下一阶段」：先出分割线，再转省略号 */}
                    {optimisticStage && (
                      <div className="stage-divider">
                        <span>{stageLabel(optimisticStage)}</span>
                      </div>
                    )}

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
                <button
                  type="button"
                  className="primary"
                  disabled={
                    busy || nextStage === null || (needsDebateStage && !canDebate)
                  }
                  title={
                    needsDebateStage && !canDebate
                      ? "至少要有两个 Bot 站在对立面才能辩论"
                      : undefined
                  }
                  onClick={() => void advanceStage()}
                >
                  {nextStage === null
                    ? "流程已走完"
                    : needsDebateStage && !canDebate
                      ? "还不能辩论（同边）"
                      : `进入「${nextStage.label}」`}
                </button>

                {/* 交锋是回合制：一次只让一位说，说完再点 */}
                {currentStage === "free" && (
                  <button
                    type="button"
                    className="primary"
                    disabled={busy}
                    onClick={() => void continueFreeTurn()}
                  >
                    下一位发言
                  </button>
                )}

                <button
                  type="button"
                  onClick={() => {
                    setAudioOn((prev) => {
                      if (prev) {
                        stopSpeech();
                      }

                      return !prev;
                    });
                  }}
                >
                  {audioOn ? "禁用声音" : "开启声音"}
                </button>
              </div>

              {summary && (
                <div className="summary-panel">
                  <p><strong>共识：</strong>{summary.consensus}</p>
                  <p><strong>分歧：</strong>{summary.disagreement}</p>
                  {summary.options.length > 0 && (
                    <label className="penalty-row">
                      未站对边的 Bot 扣活跃值
                      <input
                        type="number"
                        min={0}
                        max={100}
                        value={penalty}
                        aria-label="扣多少活跃值"
                        onChange={(e) =>
                          setPenalty(Number(e.target.value) || 0)
                        }
                      />
                    </label>
                  )}

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

              {/* 投票阶段：红蓝按钮，点一下投一边 */}
              {currentStage === "vote" && roomStances.length > 0 && (
                <div className="vote-panel">
                  <h4>投票：哪一方的论证更站得住？</h4>

                  <div className="vote-buttons">
                    {roomStances.map((stance, index) => (
                      <button
                        key={stance}
                        type="button"
                        className={`vote-btn ${
                          index === 0 ? "red" : "blue"
                        }`}
                        disabled={busy || votedStance !== null}
                        onClick={() => void castVote(stance)}
                      >
                        {votedStance === stance ? `${stance} ✓` : stance}
                      </button>
                    ))}
                  </div>

                  <p className="vote-hint">
                    房主的票按 1.5 票计，其他评委 1 票
                  </p>

                  {voteTally.length > 0 && (
                    <ul className="vote-tally">
                      {voteTally.map((item) => (
                        <li key={item.stance}>
                          <strong>{item.stance}</strong>：
                          {item.weighted} 票（{item.votes} 人）
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {/* 结辩之后：给辩手打分（房主权重 1.5） */}
              {selectedRoom?.stage === "review" && (
                <div className="score-panel">
                  <h4>给辩手打分（0–10，房主按 1.5 票计）</h4>

                  {members
                    .filter(
                      (m) => m.id !== selectedRoom?.host_bot_id
                    )
                    .map((m) => (
                      <label className="score-row" key={m.id}>
                        <span>
                          {avatarIcon(m.avatar)} {m.name}
                        </span>

                        <input
                          type="number"
                          min={0}
                          max={10}
                          value={scoreDraft[m.id] ?? 5}
                          aria-label={`给 ${m.name} 打分`}
                          onChange={(e) =>
                            setScoreDraft({
                              ...scoreDraft,
                              [m.id]:
                                Number(e.target.value) || 0,
                            })
                          }
                        />
                      </label>
                    ))}

                  <div className="score-actions">
                    <button
                      type="button"
                      className="primary"
                      disabled={busy}
                      onClick={() => void submitScores()}
                    >
                      提交评分
                    </button>

                    {scoreDone && (
                      <span className="score-done">{scoreDone}</span>
                    )}
                  </div>
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

            <p className="modal-hint">
              主持人：{defaultBotName || "（没找到默认助手）"}
              （只汇总、给选项，不参与辩论）
            </p>

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
                disabled={busy || !draftName.trim()}
                title={
                  draftName.trim()
                    ? undefined
                    : "先填辩题：它同时是房间名和讨论主题"
                }
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

            {/* 问过之后只留结论：它更倾向哪一边（大字） */}
            {askPreview && (
              <div className="ask-preview">
                <span className="ask-label">它更倾向</span>
                <strong className="ask-lean">{askPreview}</strong>
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
                disabled={busy}
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
