// 活动时间线面板：只读展示 spans / execs / artifacts 的合并事件。
//
// 设计取舍：
// - **按需加载**：打开面板才发请求，避免所有页面加载时多一次调用；
// - **错误可见**：请求失败显示错误，不静默变成空列表；
// - **无操作按钮**：这是只读视图。

import { useEffect, useState } from "react";

import type { TimelineEvent } from "../chat/types";

type Props = {
  authToken?: string;
  onClose: () => void;
};

type Payload = { events: TimelineEvent[]; count: number };

const SOURCE_LABEL: Record<string, string> = {
  span: "链路",
  exec: "执行",
  artifact: "产物",
};

function formatTime(value: string): string {
  if (!value) return "";

  return value.replace("T", " ").slice(0, 16);
}

export default function ActivityTimeline({ authToken, onClose }: Props) {
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError("");

      try {
        const response = await fetch("/api/activity/timeline?limit=30", {
          headers: authToken
            ? { Authorization: `Bearer ${authToken}` }
            : undefined,
        });

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const payload = (await response.json()) as Payload;

        if (!cancelled) {
          setEvents(payload.events ?? []);
        }
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : "未知错误");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void load();

    return () => {
      cancelled = true;
    };
  }, [authToken]);

  return (
    <aside className="activity-timeline" data-testid="activity-timeline">
      <header>
        <h3>最近活动</h3>
        <button type="button" onClick={onClose} aria-label="关闭活动时间线">
          关闭
        </button>
      </header>

      {loading && <p role="status">正在加载活动…</p>}

      {error && (
        <p className="error" role="status">
          活动加载失败：{error}
        </p>
      )}

      {!loading && !error && events.length === 0 && (
        <p role="status">暂无活动记录</p>
      )}

      {!loading && !error && events.length > 0 && (
        <ul>
          {events.map((event) => (
            <li key={`${event.source}-${event.event_id}`}>
              <span className="timeline-time">{formatTime(event.ts)}</span>
              <span className="timeline-source">
                {SOURCE_LABEL[event.source] ?? event.source}
              </span>
              <span className="timeline-title">{event.title}</span>
              {event.status && (
                <span className="timeline-status">{event.status}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
