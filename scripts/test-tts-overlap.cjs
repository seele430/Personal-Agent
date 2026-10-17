// 语音「同时说话」回归测试。
//
// 判定标准：两段音频的 [播放开始, 播放结束] 时间区间**不允许重叠**。
//
// 用法（dev server 必须在 5173）：
//   $env:NODE_PATH = "frontend\node_modules"
//   node scripts\test-tts-overlap.cjs

const { chromium } = require("playwright-core");

const APP = "http://127.0.0.1:5173";

const EXECUTABLE =
  "C:\\Users\\123\\AppData\\Local\\ms-playwright\\chromium-1248\\chrome-win64\\chrome.exe";

async function main() {
  const browser = await chromium.launch({
    executablePath: EXECUTABLE,
    args: ["--autoplay-policy=no-user-gesture-required"],
  });

  const page = await browser.newPage();

  // 劫持 Audio：记录每段音频的 playing / ended 时刻
  await page.addInitScript(() => {
    window.__log = [];
    const Real = window.Audio;

    function PatchedAudio(...args) {
      const audio = new Real(...args);
      const entry = {
        id: window.__log.length + 1,
        src: String(args[0] || "").slice(0, 40),
        playingAt: null,
        endedAt: null,
      };

      window.__log.push(entry);

      audio.addEventListener("playing", () => {
        entry.playingAt = performance.now();
      });

      const markEnd = () => {
        if (entry.endedAt === null) entry.endedAt = performance.now();
      };

      audio.addEventListener("ended", markEnd);
      audio.addEventListener("pause", markEnd);
      audio.addEventListener("error", markEnd);

      return audio;
    }

    PatchedAudio.prototype = Real.prototype;
    window.Audio = PatchedAudio;
  });

  await page.goto(APP, { waitUntil: "domcontentloaded", timeout: 30000 });

  const result = await page.evaluate(async () => {
    const tts = await import("/src/lib/tts.ts");
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));

    // 关键场景：**不 await**，连续压两条进队列
    void tts.speakText("第一段测试语音，甲", "female-1", 0.9);
    void tts.speakText("第二段测试语音，乙", "male-1", 0.9);

    // 等两段都播完（合成各 ~3s，播放各 ~2s）
    await wait(16000);

    return window.__log;
  });

  console.log("=== 音频时间轴 ===");
  for (const item of result) {
    const start = item.playingAt;
    const end = item.endedAt;
    console.log(
      `#${item.id} 开始=${start === null ? "-" : start.toFixed(0)}ms` +
        ` 结束=${end === null ? "-" : end.toFixed(0)}ms`
    );
  }

  // 重叠判定：后一段的开始早于前一段的结束
  let overlap = false;

  for (let i = 1; i < result.length; i += 1) {
    const prev = result[i - 1];
    const cur = result[i];

    if (
      prev.endedAt !== null &&
      cur.playingAt !== null &&
      cur.playingAt < prev.endedAt - 50
    ) {
      overlap = true;
      console.log(
        `❌ 重叠：#${cur.id} 在 #${prev.id} 结束前 ${(
          prev.endedAt - cur.playingAt
        ).toFixed(0)}ms 就开始播了`
      );
    }
  }

  console.log();
  console.log(`音频段数: ${result.length}`);
  console.log(`同时播放: ${overlap ? "❌ 有重叠" : "✅ 无重叠（串行）"}`);

  await browser.close();

  process.exit(overlap ? 1 : 0);
}

main().catch((error) => {
  console.error("脚本异常:", error);
  process.exit(2);
});
