// 语音合成：**优先后端 Edge TTS**（神经网络语音，音质好、免费），
// 失败则退回浏览器 speechSynthesis（音质一般但一定能出声）。
//
// 两条硬规则（都是踩过坑补的）：
// 1. **同一时刻只允许一段声音**：两个 Bot 的消息如果同时触发播放，
//    听起来就是两个人抢话。所以播放走队列，一段播完才轮到下一段；
// 2. **停止要停全部**：曾经只记了一个 audio 实例，两条重合时
//    「禁用声音」只停掉其中一个。

export type VoicePreset = {
  key: string;
  label: string;
  /** 浏览器兜底时的音高（Edge 音色不受影响） */
  pitch: number;
  rate: number;
};

export const VOICES: readonly VoicePreset[] = [
  { key: "female-1", label: "女声 · 晓晓", pitch: 1.25, rate: 1.0 },
  { key: "female-2", label: "女声 · 晓伊", pitch: 1.1, rate: 0.95 },
  { key: "male-1", label: "男声 · 云希", pitch: 0.8, rate: 0.95 },
  { key: "male-2", label: "男声 · 云健", pitch: 0.6, rate: 0.9 },
] as const;

export function findVoice(key: string | null | undefined): VoicePreset {
  return VOICES.find((item) => item.key === key) ?? VOICES[0];
}

/** 试听缓存：key = `${voice}:${text}` → blob url。合成一次就不再等。 */
const audioCache = new Map<string, string>();

/** 正在播/排队的 audio 实例（可能不止一个，停止时要全停） */
const liveAudios = new Set<HTMLAudioElement>();

/**
 * 播放「世代号」。
 *
 * 每次 stop 就 +1：已经在队列里排队、但属于旧世代的任务直接作废。
 * 之前只把 playChain 重置成 resolved —— 旧链还在跑，新旧并行，
 * 听起来就是两个人抢话。
 */
let generation = 0;

function stopAllAudio() {
  for (const audio of liveAudios) {
    try {
      // 光 pause 不够：某些情况下 pause 后仍有残音。
      // 断源 + 归零，确保真的哑掉。
      audio.pause();
      audio.currentTime = 0;
      audio.removeAttribute("src");
      audio.load();
    } catch {
      // 已结束的实例，忽略
    }
  }

  liveAudios.clear();

  if (typeof window !== "undefined" && "speechSynthesis" in window) {
    const synth = window.speechSynthesis;

    // Chrome 的 cancel() 对正在播的长句有时不生效，
    // pause → resume → cancel 连招才能真停住。
    synth.cancel();
    synth.pause();
    synth.resume();
    synth.cancel();
  }
}

/** 请求后端合成，返回可播放的 blob url（带缓存）。 */
async function synthUrl(
  text: string,
  voiceKey: string
): Promise<string | null> {
  const cacheKey = `${voiceKey}:${text}`;
  const cached = audioCache.get(cacheKey);

  if (cached) return cached;

  const res = await fetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: text.slice(0, 2000), voice: voiceKey }),
  });

  if (!res.ok) throw new Error(`tts ${res.status}`);

  const url = URL.createObjectURL(await res.blob());

  audioCache.set(cacheKey, url);

  return url;
}

/** 预生成试听音频：进 Bot 管理页就悄悄把四个音色合成好。 */
export function prewarmVoices(text = "你好，世界") {
  for (const item of VOICES) {
    void synthUrl(text, item.key).catch(() => {
      // 预生成失败无所谓，点的时候会再试
    });
  }
}

/** 朗读用的文本清洗：TTS 会把 * 念成「星号」。 */
export function cleanForSpeech(raw: string): string {
  return raw
    .replace(/\*\*/g, "")
    .replace(/__/g, "")
    .replace(/`/g, "")
    .replace(/(^|\s)\*(?=\S)/g, "$1")
    .trim();
}

/**
 * 预热一句话：只合成、不播放。
 *
 * 消息一到就调它，把 2-3 秒的合成时间藏进「文字逐字显示」里，
 * 等显示完了再播，用户就感觉不到前摇。
 * **必须和 speakText 用同一套清洗**，否则缓存 key 对不上、白预热。
 */
export function prewarmSpeech(
  text: string,
  voiceKey: string | null | undefined
) {
  const trimmed = cleanForSpeech(String(text ?? ""));

  if (!trimmed) return;

  void synthUrl(trimmed.slice(0, 2000), findVoice(voiceKey).key).catch(() => {
    // 预热失败无所谓，播的时候会再试
  });
}

/** 真正播一段（内部用，会被队列串起来）。 */
/** 朗读结果：失败时把原因带出来，让界面能显示。 */
export type SpeakResult = { ok: boolean; error?: string };

async function playOne(
  text: string,
  voiceKey: string | null | undefined,
  volume: number,
  gen: number
): Promise<SpeakResult> {
  const trimmed = cleanForSpeech(String(text ?? ""));

  if (!trimmed) return { ok: true };

  const preset = findVoice(voiceKey);

  // 注意：这里**不再退回浏览器 speechSynthesis**。
  // 实测 WebView2 里语音数量是 0（打包版一个系统语音都没有），
  // 回退过去只会静默无声 —— 比报错更难查。
  try {
    const url = await synthUrl(trimmed, preset.key);

    if (!url) throw new Error("合成结果为空");

    // 关键：合成期间可能已经被 stop 了（用户点了「进入下一阶段」）。
    // 那时候任务还没进 liveAudios，stopAllAudio 停不到它；
    // 如果不在这里再查一次世代号，它合成完就会照播，
    // 和刚启动的新一段声音**叠在一起**。
    if (gen !== generation) {
      if (import.meta.env.DEV) {
        console.warn("[tts] 合成完成但已被 stop，放弃播放", {
          gen,
          now: generation,
        });
      }
      return { ok: true };
    }

    const audio = new Audio(url);

    audio.volume = Math.max(0, Math.min(1, volume));
    liveAudios.add(audio);

    // 播完（或出错、或被主动暂停）就释放这个实例，队列继续往下走。
    // 必须监听 pause：stopAllAudio 走的是 pause()，不监听的话
    // 这个 Promise 永远不结束，队列会卡死。
    await new Promise<void>((resolve) => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        resolve();
      };

      audio.onended = done;
      audio.onerror = done;
      audio.onpause = done;
      void audio.play().catch(done);
    });

    liveAudios.delete(audio);

    return { ok: true };
  } catch (cause) {
    return {
      ok: false,
      error:
        cause instanceof Error ? cause.message : "语音合成或播放失败",
    };
  }
}

// 播放队列：保证同一时刻只有一段声音在播
let playChain: Promise<SpeakResult> = Promise.resolve({ ok: true });

/** 朗读一段文字。失败时返回 ok=false + 原因（调用方负责提示）。 */
export function speakText(
  text: string,
  voiceKey: string | null | undefined,
  volume = 0.9
): Promise<SpeakResult> {
  if (typeof window === "undefined" || volume <= 0) {
    return Promise.resolve({ ok: true });
  }

  const gen = generation;

  if (import.meta.env.DEV) {
    console.warn(
      "[tts] speakText · 入队",
      { voice: voiceKey, len: String(text ?? "").length, gen }
    );
  }

  playChain = playChain
    .then(() => {
      // stop 之后排队中的旧任务直接作废，避免和新任务一起响
      if (gen !== generation) {
        if (import.meta.env.DEV) {
          console.warn("[tts] 任务作废（已被 stop）", { gen, now: generation });
        }
        return { ok: true } as SpeakResult;
      }
      return playOne(text, voiceKey, volume, gen);
    })
    .catch((error: unknown) => ({
      ok: false,
      error: error instanceof Error ? error.message : "语音失败",
    }));

  return playChain;
}

/** 试听：先掐掉所有声音，再读一句。 */
export function previewVoice(
  voiceKey: string,
  text = "你好，世界"
): Promise<SpeakResult> {
  stopSpeech();
  return speakText(text, voiceKey, 0.95);
}

/** 关掉语音时调用：停掉全部正在播的，并作废排队中的任务。 */
export function stopSpeech() {
  generation += 1;

  if (import.meta.env.DEV) {
    console.warn(
      "[tts] stopSpeech · 正在播的实例数 =",
      liveAudios.size
    );
  }

  stopAllAudio();

  // 关键：把队列链**重置**，否则新任务会排在旧链后面等一个
  // 永远不会来的结束事件 —— 表现就是「进入下一阶段后彻底没声音」。
  //
  // 重置后会话里的旧 playOne 还在 await，但它所在的旧链已经不再被引用，
  // 而 generation 又让排队中的旧任务直接作废，所以不会出现两段声音并行。
  playChain = Promise.resolve({ ok: true });
}
