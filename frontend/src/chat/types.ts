// 聊天与产物的共享类型（从 App.tsx 拆出，W4）

export type MessageStatus =
  | "pending"
  | "sent"
  | "unknown"
  | "rejected"
  | "cancelled";

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: MessageStatus;
  requestId?: string;
  replyTo?: string;
};

export type ChatResponse = {
  request_id?: string;
  status?: string;
  reply?: string | null;
  code?: string;
  detail?: string;
};

/** 产物元数据（与后端 /api/artifacts 返回一致） */
export type ArtifactMeta = {
  id: string;
  title: string;
  kind: "image" | "text" | "markdown" | "pdf" | "other";
  media_type: string;
  size_bytes: number;
  sha256: string;
  run_id: string;
  exec_id: string;
  created_at: string;
};

/** /api/artifacts/{id}/preview 返回 */
export type ArtifactPreview = {
  id: string;
  title: string;
  kind: ArtifactMeta["kind"];
  media_type: string;
  size_bytes: number;
  sha256: string;
  previewable: boolean;
  mode: "inline" | "text" | "markdown" | "download";
  url?: string;
  text?: string;
  truncated?: boolean;
  note?: string;
};

/** /api/activity/{trace}/summary 返回 */
export type ActivitySummaryPayload = {
  trace_id: string;
  span_count: number;
  llm_calls: number;
  tool_calls: number;
  command_calls: number;
  tts_calls: number;
  other_calls: number;
  error_count: number;
  duration_ms: number;
  text: string;
};


export type TimelineEvent = {
  ts: string;
  source: string;
  event_id: string;
  title: string;
  status?: string;
  detail?: string;
};


export type DiffLine = {
  kind: "add" | "remove" | "context";
  text: string;
};

export type DiffResult = {
  kind: "text" | "binary" | "identical";
  a_name: string;
  b_name: string;
  added: number;
  removed: number;
  truncated: boolean;
  changed: boolean;
  a_size: number;
  b_size: number;
  lines: DiffLine[];
};
