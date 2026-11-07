// 中文活动摘要卡：只展示后端算好的事实，不在前端"补故事"。

import type { ActivitySummaryPayload } from "../chat/types";

type Props = {
  summary: ActivitySummaryPayload | null;
  loading?: boolean;
  error?: string;
};

export default function ActivitySummary({ summary, loading, error }: Props) {
  if (error) {
    return (
      <div className="activity-summary error" role="status">
        活动摘要加载失败：{error}
      </div>
    );
  }

  if (loading) {
    return (
      <div className="activity-summary" role="status">
        正在整理活动摘要…
      </div>
    );
  }

  if (!summary) return null;

  return (
    <div
      className={`activity-summary${summary.error_count > 0 ? " warning" : ""}`}
      role="status"
      data-testid="activity-summary"
    >
      <span className="activity-text">{summary.text}</span>
    </div>
  );
}
