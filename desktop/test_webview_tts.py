"""探测 WebView2 里的 speechSynthesis 有没有可用语音。

背景：桌面版语音主路径是后端 Edge TTS（不依赖浏览器），
但 `speakText` 失败时会退回浏览器 speechSynthesis。
如果 WebView2 里一个中文语音都没有，那兜底就是哑的。

用法：.venv\\Scripts\\python.exe desktop\\test_webview_tts.py
"""

import json
import sys

import webview

RESULT: dict = {}


def probe(window) -> None:
    try:
        raw = window.evaluate_js(
            """
            (() => {
              const out = { hasApi: false, count: 0, voices: [] };

              if (!("speechSynthesis" in window)) return out;

              out.hasApi = true;

              const voices = window.speechSynthesis.getVoices() || [];

              out.count = voices.length;
              out.voices = voices.slice(0, 8).map((v) => ({
                name: v.name,
                lang: v.lang,
                local: v.localService,
              }));

              return out;
            })()
            """
        )

        RESULT.update(raw if isinstance(raw, dict) else {"raw": raw})
    except Exception as error:  # noqa: BLE001
        RESULT["error"] = repr(error)
    finally:
        window.destroy()


def main() -> None:
    window = webview.create_window(
        "语音探测",
        html="<html><body><h3>probing speechSynthesis…</h3></body></html>",
        width=600,
        height=400,
    )

    webview.start(probe, window)

    print(json.dumps(RESULT, ensure_ascii=False, indent=2))

    has_chinese = any(
        "zh" in (v.get("lang") or "") for v in RESULT.get("voices", [])
    )

    print()
    print("有 speechSynthesis API:", RESULT.get("hasApi"))
    print("语音数量:", RESULT.get("count"))
    print("有中文语音:", has_chinese)
    print("→ 兜底路径", "可用" if has_chinese else "不可用（只能靠后端 Edge TTS）")


if __name__ == "__main__":
    main()
