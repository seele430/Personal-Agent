// 图生图面板：调 POST /api/tools/comfy/img2img。
//
// GPU 约束（2026-11-08 蓝屏后）：steps 上限 30（前端先挡一次，后端还有硬校验）；
// 一次一张；503 的 detail 原样展示（后端已经写了中文原因，比如"显存不足"）。

import { useState } from "react";

type Props = {
  authToken?: string;
  onClose: () => void;
};

const MAX_STEPS = 30;

export default function ComfyImagePanel({ authToken, onClose }: Props) {
  const [imagePath, setImagePath] = useState("input.png");
  const [prompt, setPrompt] = useState("");
  const [steps, setSteps] = useState(8);
  const [result, setResult] = useState<string>("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const tooManySteps = steps > MAX_STEPS || steps < 1;

  async function submit() {
    if (tooManySteps || !prompt.trim() || !imagePath.trim()) return;

    setLoading(true);
    setError("");
    setResult("");

    try {
      const response = await fetch("/api/tools/comfy/img2img", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify({
          image_path: imagePath.trim(),
          prompt: prompt.trim(),
          steps,
        }),
      });

      const payload = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(payload.detail ?? `HTTP ${response.status}`);
      }

      const images: string[] = payload.images ?? [];
      setResult(images[0] ?? "(没有返回文件名)");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "请求失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <aside className="comfy-panel" data-testid="comfy-panel">
      <header>
        <h3>图生图</h3>
        <button type="button" onClick={onClose} aria-label="关闭图生图">
          关闭
        </button>
      </header>

      <p className="artifact-hint">
        输入图需先放进 ComfyUI 的 <code>input/</code> 目录；每次只出一张，用完请停服务。
      </p>

      <label className="video-field">
        输入文件名
        <input
          value={imagePath}
          onChange={(event) => setImagePath(event.target.value)}
          aria-label="输入图片文件名"
        />
      </label>

      <label className="video-field">
        提示词
        <input
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="a photo of a cat sitting on a chair"
          aria-label="图生图提示词"
        />
      </label>

      <label className="video-field">
        步数（1–30）
        <input
          type="number"
          min={1}
          max={MAX_STEPS}
          value={steps}
          onChange={(event) => setSteps(Number(event.target.value))}
          aria-label="采样步数"
        />
      </label>

      {tooManySteps && (
        <p className="artifact-hint warning" role="status">
          步数必须在 1–30（显存保护上限）
        </p>
      )}

      <button
        type="button"
        onClick={() => void submit()}
        disabled={loading || tooManySteps || !prompt.trim()}
      >
        {loading ? "生成中…" : "开始生成"}
      </button>

      {error && (
        <p className="artifact-hint warning" role="status">
          失败：{error}
        </p>
      )}

      {result && (
        <p data-testid="comfy-result">
          已生成：{result}（文件在 ComfyUI 的 output 目录）
        </p>
      )}
    </aside>
  );
}
