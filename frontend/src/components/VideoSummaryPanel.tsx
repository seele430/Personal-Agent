// 视频总结面板：接 POST /api/video/summarize。
//
// 不做上传：文件必须由用户放进 data/videos/，面板只填文件名——这一点写在文案里，
// 不假装有上传能力。

import { useState } from "react";

type Point = { text: string; start: number; end: number; segments: number[] };

type Result = {
  video: string;
  duration_s: number;
  language: string;
  speech_chars: number;
  points: Point[];
  usage: { prompt: number; completion: number; total: number };
  truncated: boolean;
  report_path: string;
};

type Props = {
  authToken?: string;
  onClose: () => void;
};

export default function VideoSummaryPanel({ authToken, onClose }: Props) {
  const [path, setPath] = useState("");
  const [prompt, setPrompt] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [unverified, setUnverified] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit() {
    setLoading(true);
    setError("");
    setUnverified("");
    setResult(null);

    try {
      const response = await fetch("/api/video/summarize", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify({ path, prompt }),
      });

      const payload = await response.json().catch(() => ({}));

      if (response.status === 422) {
        setUnverified(payload.detail ?? "无法验证");

        return;
      }

      if (!response.ok) {
        throw new Error(payload.detail ?? `HTTP ${response.status}`);
      }

      setResult(payload as Result);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "请求失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <aside className="video-panel" data-testid="video-panel">
      <header>
        <h3>视频总结</h3>
        <button type="button" onClick={onClose} aria-label="关闭视频总结">
          关闭
        </button>
      </header>

      <p className="artifact-hint">
        文件需先在 <code>data/videos/</code> 目录下，这里填文件名。
      </p>

      <label className="video-field">
        文件名
        <input
          value={path}
          onChange={(event) => setPath(event.target.value)}
          placeholder="demo.mp4"
          aria-label="视频文件名"
        />
      </label>

      <label className="video-field">
        术语提示（可选）
        <input
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="PagedAttention, vLLM"
          aria-label="术语提示"
        />
      </label>

      <button
        type="button"
        onClick={() => void submit()}
        disabled={!path || loading}
      >
        {loading ? "转写中…" : "开始总结"}
      </button>

      {unverified && (
        <p className="artifact-hint warning" role="status">
          无法验证：{unverified}
        </p>
      )}

      {error && (
        <p className="artifact-hint warning" role="status">
          失败：{error}
        </p>
      )}

      {result && (
        <section data-testid="video-result">
          <p>
            {result.video} · {result.duration_s}s · {result.language} ·{" "}
            {result.speech_chars} 字 · {result.usage.total} tokens
          </p>

          <ol>
            {result.points.map((point, index) => (
              <li key={index}>
                <span className="video-time">
                  [{point.start.toFixed(1)}–{point.end.toFixed(1)}]
                </span>{" "}
                {point.text}
              </li>
            ))}
          </ol>
        </section>
      )}
    </aside>
  );
}
