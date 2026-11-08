// 产物预览侧栏：四类预览 + 下载 + 关闭。
//
// 安全口径：只按 artifact_id 请求，前端拼不出任意路径；
// 预览失败不阻塞下载按钮（降级可用）。

import { useEffect, useState } from "react";

import ArtifactCard, { formatSize } from "./ArtifactCard";
import type {
  ActivitySummaryPayload,
  ArtifactMeta,
  ArtifactPreview,
  DiffResult,
} from "../chat/types";

type Props = {
  artifact: ArtifactMeta;
  authToken?: string;
  onClose: () => void;
};

export default function ArtifactSidebar({
  artifact,
  authToken,
  onClose,
}: Props) {
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [activity, setActivity] = useState<ActivitySummaryPayload | null>(null);
  const [compareId, setCompareId] = useState("");
  const [others, setOthers] = useState<ArtifactMeta[]>([]);
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [diffError, setDiffError] = useState("");
  const [diffLoading, setDiffLoading] = useState(false);


  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError("");

      try {
        const response = await fetch(
          `/api/artifacts/${encodeURIComponent(artifact.id)}/preview`,
          {
            headers: authToken
              ? { Authorization: `Bearer ${authToken}` }
              : {},
          },
        );

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const payload = (await response.json()) as ArtifactPreview;

        if (!cancelled) setPreview(payload);
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "预览失败");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void load();

    return () => {
      cancelled = true;
    };
  }, [artifact.id, authToken]);

  useEffect(() => {
    if (!artifact.run_id) {
      setActivity(null);
      return;
    }

    let cancelled = false;

    async function loadSummary() {
      try {
        const response = await fetch(
          `/api/activity/${encodeURIComponent(artifact.run_id!)}/summary`,
          {
            headers: authToken
              ? { Authorization: `Bearer ${authToken}` }
              : {},
          },
        );

        if (!response.ok) return;

        const payload = (await response.json()) as ActivitySummaryPayload;

        if (!cancelled) setActivity(payload);
      } catch {
        // 摘要失败不影响产物本身
      }
    }

    void loadSummary();

    return () => {
      cancelled = true;
    };
  }, [artifact.run_id, authToken]);


  useEffect(() => {
    let cancelled = false;

    async function loadOthers() {
      try {
        const response = await fetch("/api/artifacts", {
          headers: authToken
            ? { Authorization: `Bearer ${authToken}` }
            : undefined,
        });

        if (!response.ok) return;

        const payload = (await response.json()) as { items?: ArtifactMeta[] };
        const items = payload.items ?? [];

        if (!cancelled) {
          setOthers(items.filter((item) => item.id !== artifact.id));
        }
      } catch {
        // 列表失败不阻塞预览
      }
    }

    void loadOthers();

    return () => {
      cancelled = true;
    };
  }, [artifact.id, authToken]);

  async function compare(target: string) {
    if (!target) return;

    setDiffLoading(true);
    setDiffError("");

    try {
      const response = await fetch(
        `/api/artifacts/${encodeURIComponent(artifact.id)}/diff/${encodeURIComponent(target)}`,
        {
          headers: authToken
            ? { Authorization: `Bearer ${authToken}` }
            : undefined,
        },
      );

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      setDiff((await response.json()) as DiffResult);
    } catch (cause) {
      setDiffError(cause instanceof Error ? cause.message : "对比失败");
      setDiff(null);
    } finally {
      setDiffLoading(false);
    }
  }

  const contentUrl = `/api/artifacts/${encodeURIComponent(artifact.id)}/content`;

  return (
    <aside
      className="artifact-sidebar"
      aria-label="产物预览"
      data-testid="artifact-sidebar"
    >
      <header className="artifact-sidebar-head">
        <div>
          <h2>{artifact.title}</h2>
          <p className="artifact-sub">
            {artifact.kind} · {formatSize(artifact.size_bytes)} ·{" "}
            <code>{artifact.sha256.slice(0, 12)}</code>
          </p>
        </div>

        <button
          type="button"
          className="sidebar-close"
          onClick={onClose}
          aria-label="关闭预览"
        >
          ✕
        </button>
      </header>

      <div className="artifact-body">
        {loading && <p className="artifact-hint">正在加载预览…</p>}

        {!loading && error && (
          <p className="artifact-hint warning">预览失败：{error}</p>
        )}

        {!loading && preview?.mode === "inline" && artifact.kind === "image" && (
          <img
            className="artifact-image"
            src={`${preview.url}?inline=1`}
            alt={artifact.title}
          />
        )}

        {!loading && preview?.mode === "inline" && artifact.kind === "pdf" && (
          <iframe
            className="artifact-pdf"
            title={artifact.title}
            src={`${preview.url}?inline=1`}
          />
        )}

        {!loading &&
          (preview?.mode === "text" || preview?.mode === "markdown") && (
            <pre className="artifact-text" data-testid="artifact-text">
              {preview.text}
              {preview.truncated ? "\n…（已截断）" : ""}
            </pre>
          )}

        {!loading && preview?.mode === "download" && (
          <p className="artifact-hint">{preview.note ?? "该类型不支持预览"}</p>
        )}


        <section className="artifact-diff" aria-label="产物对比">
          <h3>对比</h3>

          {others.length === 0 ? (
            <p className="artifact-hint">没有其他产物可对比</p>
          ) : (
            <div className="diff-controls">
              <select
                aria-label="选择对比对象"
                value={compareId}
                onChange={(event) => setCompareId(event.target.value)}
              >
                <option value="">选择产物…</option>
                {others.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.title}
                  </option>
                ))}
              </select>

              <button
                type="button"
                onClick={() => void compare(compareId)}
                disabled={!compareId || diffLoading}
              >
                {diffLoading ? "对比中…" : "对比"}
              </button>
            </div>
          )}

          {diffError && (
            <p className="artifact-hint warning">对比失败：{diffError}</p>
          )}

          {diff && diff.kind === "binary" && (
            <p className="artifact-hint" data-testid="diff-binary">
              二进制文件，{diff.changed ? "内容已改变" : "内容相同"}
              （{diff.a_size} / {diff.b_size} 字节）
            </p>
          )}

          {diff && diff.kind === "identical" && (
            <p className="artifact-hint" data-testid="diff-identical">
              两个产物内容相同
            </p>
          )}

          {diff && diff.kind === "text" && (
            <pre className="artifact-diff-body" data-testid="diff-body">
              {diff.lines.map((line, index) => (
                <span key={index} className={`diff-line ${line.kind}`}>
                  {line.kind === "add" ? "+" : line.kind === "remove" ? "-" : " "}
                  {line.text}
                  {"\n"}
                </span>
              ))}
            </pre>
          )}
        </section>

        {activity && (
          <section className="artifact-activity" aria-label="生成过程">
            <h3>生成过程</h3>
            <p data-testid="artifact-activity-text">{activity.text}</p>
          </section>
        )}
      </div>

      <footer className="artifact-sidebar-foot">
        <a
          className="artifact-download"
          href={contentUrl}
          download={artifact.title}
          aria-label={`下载 ${artifact.title}`}
        >
          下载
        </a>

        <ArtifactCard
          artifact={artifact}
          active
          onOpen={() => undefined}
        />
      </footer>
    </aside>
  );
}
