// 语音播放的自测脚本（Playwright + 真实 Chromium）。
//
// 测的是最要命的两件事：
//   1. speakText 真的能播（audio 进入 playing）
//   2. stopSpeech 之后还能再播（队列没被卡死）—— 这正是"进下一阶段没声音"的根因
//
// 用法：node scripts/test-tts.cjs
// 前提：dev server 在 5173

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

  // 在页面脚本之前注入：劫持 Audio，记录实例并跟踪是否真的进入播放
  await page.addInitScript(() => {
    window.__audios = [];
    const Real = window.Audio;

    function PatchedAudio(...args) {
      const audio = new Real(...args);
      audio.__played = false;
      audio.__errored = null;

      audio.addEventListener("playing", () => {
        audio.__played = true;
      });
      audio.addEventListener("error", () => {
        audio.__errored = audio.error?.code ?? "unknown";
      });

      window.__audios.push(audio);
      return audio;
    }

    PatchedAudio.prototype = Real.prototype;
    window.Audio = PatchedAudio;
  });

  await page.goto(APP, { waitUntil: "domcontentloaded", timeout: 30000 });

  const result = await page.evaluate(async () => {
    const log = [];
    const notes = (msg) => log.push(msg);

    // 动态导入 dev server 上的源码模块
    const tts = await import("/src/lib/tts.ts");

    const wait = (ms) => new Promise((r) => setTimeout(r, ms));

    // ---- 用例 1：正常播一段 ----
    notes("case1: speakText 第一条");
    await tts.speakText("第一条测试语音", "female-1", 0.9);
    await wait(5000);

    const afterFirst = window.__audios.length;
    const first = window.__audios[0];
    const firstPlayed = Boolean(first?.__played);
    const firstError = first?.__errored ?? null;

    notes(
      `case1 结果: 实例数=${afterFirst}, 真的播了=${firstPlayed}, 错误=${firstError}`
    );

    // ---- 用例 2：stop 之后再播（关键回归） ----
    notes("case2: stopSpeech 后再 speakText");
    tts.stopSpeech();
    await wait(300);

    await tts.speakText("第二条测试语音", "male-1", 0.9);
    await wait(6000);

    const afterSecond = window.__audios.length;
    const second = window.__audios[window.__audios.length - 1];
    const secondPlayed = Boolean(second?.__played);
    const secondError = second?.__errored ?? null;

    notes(
      `case2 结果: 实例数=${afterSecond}, 真的播了=${secondPlayed}, 错误=${secondError}`
    );

    return {
      log,
      totalAudios: afterSecond,
      firstPlayed,
      secondPlayed,
      // 两个用例都必须真的进入播放状态
      passed: firstPlayed && secondPlayed,
    };
  });

  console.log(result.log.join("\n"));
  console.log("\n=== 判定 ===");
  console.log("总 Audio 实例数:", result.totalAudios);
  console.log("stop 后还能播:", result.passed ? "✅ PASS" : "❌ FAIL");

  await browser.close();

  process.exit(result.passed ? 0 : 1);
}

main().catch((error) => {
  console.error("脚本异常:", error);
  process.exit(2);
});
