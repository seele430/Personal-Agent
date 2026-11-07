// 产物卡片：一个文件夹里的"成果"，点开进侧栏预览。

import type { ArtifactMeta } from "../chat/types";

const KIND_ICON: Record<ArtifactMeta["kind"], string> = {
  image: "🖼",
  markdown: "📝",
  text: "📄",
  pdf: "📕",
  other: "📦",
};

const KIND_TEXT: Record<ArtifactMeta["kind"], string> = {
  image: "图片",
  markdown: "Markdown",
  text: "文本",
  pdf: "PDF",
  other: "其他",
};

export function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;

  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

type Props = {
  artifact: ArtifactMeta;
  active: boolean;
  onOpen: (artifactId: string) => void;
};

export default function ArtifactCard({ artifact, active, onOpen }: Props) {
  return (
    <button
      type="button"
      className={`artifact-card${active ? " active" : ""}`}
      onClick={() => onOpen(artifact.id)}
      aria-label={`打开产物 ${artifact.title}`}
      data-artifact-id={artifact.id}
    >
      <span className="artifact-icon" aria-hidden="true">
        {KIND_ICON[artifact.kind] ?? KIND_ICON.other}
      </span>

      <span className="artifact-text">
        <span className="artifact-title">{artifact.title}</span>
        <span className="artifact-meta">
          {KIND_TEXT[artifact.kind] ?? "其他"} · {formatSize(artifact.size_bytes)}
        </span>
      </span>
    </button>
  );
}
