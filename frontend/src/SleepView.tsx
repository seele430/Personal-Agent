import { useCallback, useEffect, useRef, useState } from "react";

import "./SleepView.css";

type SleepRun = {
  id: number;
  created_at: string;
  mode: string;
  ab_variant: string;
  ops_count: number;
  applied_count: number;
  execution_status: string;
  gate_status: string;
  record_status: string;
  recall_before: number | null;
  recall_after: number | null;
  mrr_before: number | null;
  mrr_after: number | null;
};

type SleepRunDetail = SleepRun & {
  base_snapshot_id: number | null;
  candidate_id: string | null;
  error_code: string;
  record_error: string;
  ops: Array<{
    op: string;
    reason: string;
    item_id: number;
    item_key: string;
    evidence_ids: number[];
    evidence_keys: string[];
  }>;
  reports: Record<string, unknown>;
  metrics: Record<string, unknown>;
};

const MODE_TEXT: Record<string, string> = {
  plan: "计划",
  publish: "发布",
  ab: "A/B",
};

const EXEC_TEXT: Record<string, string> = {
  succeeded: "执行成功",
  failed: "执行失败",
  partial: "部分完成",
};

const GATE_TEXT: Record<string, string> = {
  not_applicable: "不适用",
  not_run: "未运行",
  passed: "门槛通过",
  failed: "门槛未通过",
};

function formatTime(value: string): string {
  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatMetric(value: number | null): string {
  return value === null || value === undefined ? "—" : value.toFixed(3);
}

function reasonText(reason: string): { title: string; evidence: string } {
  if (reason === "exact_dedupe") {
    return { title: "过期重复条目", evidence: "保留依据" };
  }

  if (reason === "explicit_supersede") {
    return { title: "过期旧条目", evidence: "新版本依据" };
  }

  return { title: reason, evidence: "依据" };
}

export default function SleepView({ authToken }: { authToken?: string }) {
  const token = authToken ?? "";
  const [runs, setRuns] = useState<SleepRun[]>([]);
  const [total, setTotal] = useState(0);
  const [mode, setMode] = useState("");
  const [execution, setExecution] = useState("");
  const [gate, setGate] = useState("");
  const [detail, setDetail] = useState<SleepRunDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setLoading(true);
    setError("");

    try {
      const params = new URLSearchParams({ limit: "50" });

      if (mode) params.set("mode", mode);
      if (execution) params.set("execution_status", execution);
      if (gate) params.set("gate_status", gate);

      const response = await fetch(`/api/sleep/runs?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (requestRef.current !== requestId) return;

      if (!response.ok) {
        throw new Error(`加载失败（${response.status}）`);
      }

      const data = await response.json();

      if (requestRef.current !== requestId) return;

      setRuns(data.items ?? []);
      setTotal(data.total ?? 0);
    } catch (cause) {
      if (requestRef.current !== requestId) return;

      setError(cause instanceof Error ? cause.message : "加载失败");
    } finally {
      if (requestRef.current === requestId) {
        setLoading(false);
      }
    }
  }, [token, mode, execution, gate]);

  useEffect(() => {
    void load();
  }, [load]);

  async function openDetail(id: number) {
    setError("");

    try {
      const response = await fetch(`/api/sleep/runs/${id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (!response.ok) {
        throw new Error(`详情加载失败（${response.status}）`);
      }

      setDetail(await response.json());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "详情加载失败");
    }
  }

  return (
    <section className="sleep-panel" aria-label="睡眠整理记录">
      <header className="sleep-head">
        <div>
          <h2>睡眠整理</h2>
          <p className="sleep-hint">
            查看每次 plan / publish / A/B 的结果；这里只读，不会触发写入。
          </p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </button>
      </header>

      <div className="sleep-filters">
        <label>
          模式
          <select value={mode} onChange={(event) => setMode(event.target.value)}>
            <option value="">全部</option>
            <option value="plan">计划</option>
            <option value="publish">发布</option>
            <option value="ab">A/B</option>
          </select>
        </label>
        <label>
          执行
          <select value={execution} onChange={(event) => setExecution(event.target.value)}>
            <option value="">全部</option>
            <option value="succeeded">成功</option>
            <option value="failed">失败</option>
            <option value="partial">部分</option>
          </select>
        </label>
        <label>
          门槛
          <select value={gate} onChange={(event) => setGate(event.target.value)}>
            <option value="">全部</option>
            <option value="not_applicable">不适用</option>
            <option value="not_run">未运行</option>
            <option value="passed">通过</option>
            <option value="failed">未通过</option>
          </select>
        </label>
        <span className="sleep-total">共 {total} 条</span>
      </div>

      {error && (
        <div className="sleep-error">
          {error}
          <button type="button" onClick={() => void load()}>重试</button>
        </div>
      )}

      {!loading && runs.length === 0 ? (
        <p className="sleep-empty">还没有睡眠记录，先运行 memory_sleep.py plan。</p>
      ) : (
        <table className="sleep-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>模式</th>
              <th>执行</th>
              <th>门槛</th>
              <th>记录</th>
              <th>ops</th>
              <th>applied</th>
              <th>recall 前 → 后</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr
                key={run.id}
                onClick={() => void openDetail(run.id)}
                className={detail?.id === run.id ? "selected" : ""}
              >
                <td>{formatTime(run.created_at)}</td>
                <td>{MODE_TEXT[run.mode] ?? run.mode}</td>
                <td>{EXEC_TEXT[run.execution_status] ?? run.execution_status}</td>
                <td>{GATE_TEXT[run.gate_status] ?? run.gate_status}</td>
                <td>{run.record_status === "degraded" ? "摘要已截断" : "完整"}</td>
                <td>{run.ops_count}</td>
                <td>{run.applied_count}</td>
                <td>
                  {formatMetric(run.recall_before)} → {formatMetric(run.recall_after)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {detail && (
        <aside className="sleep-detail">
          <header>
            <h3>记录 #{detail.id}</h3>
            <button type="button" onClick={() => setDetail(null)}>关闭</button>
          </header>
          <p>
            模式：{MODE_TEXT[detail.mode] ?? detail.mode}
            {detail.ab_variant !== "none" ? ` / ${detail.ab_variant}` : ""}
          </p>
          <p>
            执行：{EXEC_TEXT[detail.execution_status] ?? detail.execution_status}；
            门槛：{GATE_TEXT[detail.gate_status] ?? detail.gate_status}
          </p>
          {detail.gate_status === "not_run" && <p className="sleep-note">仅基线，未运行门槛。</p>}
          {detail.gate_status === "failed" && (
            <p className="sleep-note">执行完成，门槛未通过。</p>
          )}
          {detail.record_status === "degraded" && (
            <p className="sleep-note">摘要已截断，ops 总数不受影响。</p>
          )}

          <h4>候选操作</h4>
          {detail.ops.length === 0 ? (
            <p>没有候选操作。</p>
          ) : (
            <ul className="sleep-ops">
              {detail.ops.map((op, index) => {
                const text = reasonText(op.reason);

                return (
                  <li key={`${op.item_id}-${index}`}>
                    <strong>{text.title}</strong> #{op.item_id}
                    {op.item_key ? `（${op.item_key}）` : ""}
                    <span className="sleep-arrow">→</span>
                    {text.evidence} #{op.evidence_ids.join(", ")}
                    {op.evidence_keys.length > 0 ? `（${op.evidence_keys.join(", ")}）` : ""}
                  </li>
                );
              })}
            </ul>
          )}

          <h4>指标</h4>
          <pre>{JSON.stringify(detail.metrics, null, 2)}</pre>

          {(detail.error_code || detail.record_error) && (
            <p className="sleep-error-code">
              错误码：{detail.error_code || "—"}；记录：{detail.record_error || "—"}
            </p>
          )}
        </aside>
      )}
    </section>
  );
}
