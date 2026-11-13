import { useCallback, useEffect, useState } from "react";

/**
 * 平台控制台（只读版）：总览 / 节点 / 沙箱 / 执行 四个区。弹性区已随弹性平台抽离（2026-11-13）。
 *
 * 数据只来自 `GET /console/snapshot` 一个入口（V2 §4.1）：
 * 每块都是 `{data, error}`，单块失败只在那一块显示错误，不让整页空白。
 * 轮询 10 秒，页面隐藏时暂停，恢复可见立即刷新（V2 §2.8）。
 */

const POLL_MS = 10_000;

const color = {
  text: "#0f172a",
  muted: "#64748b",
  line: "#e3e8ef",
  soft: "#f8fafc",
  warn: "#b45309",
  danger: "#b42318",
  ok: "#067647",
  info: "#1d4ed8",
};

type Envelope<T> = {
  data: T | null;
  error: { code: string; message: string } | null;
};

type QueueData = {
  active: number;
  queued: number;
  max_active: number;
  max_queued: number;
  max_wait_seconds: number;
};

type Capacity = {
  nodes_total: number;
  nodes_accepting: number;
  nodes_cordoned: number;
  nodes_draining: number;
  nodes_expired: number;
  slots_accepting: number;
  unit: string;
};

type NodeRow = {
  node_id: string;
  state: string;
  capacity: number;
  cordoned: boolean;
  draining: boolean;
  accepting: boolean;
  expired: boolean;
  lease_expires_at: string | null;
  seconds_since_heartbeat: number | null;
  active_sandboxes: number;
};

type NodeData = { summary: Capacity; rows: NodeRow[] };

type SandboxRow = {
  id: string;
  node_id: string | null;
  tier: string;
  state: string;
  created_at: string;
  expires_at: string;
  reclaimed_at: string | null;
  reclaim_reason: string | null;
};

type SandboxData = {
  counts: Record<string, number>;
  active_total: number;
  rows: SandboxRow[];
};

type Percentiles = {
  p50: number | null;
  p99: number | null;
  samples: number;
  insufficient_samples: boolean;
};

type ExecutionsData = {
  recent: Array<{
    id: string;
    sandbox_id: string;
    state: string;
    exit_code: number | null;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
    error_code: string | null;
  }>;
  states: Record<string, number>;
  window: {
    window_seconds: number;
    samples: number;
    terminal: Record<string, number>;
    success_rate: {
      value: number | null;
      numerator: number;
      denominator: number;
      excluded_cancelled: number;
      note: string;
    };
    queue_wait_ms: Percentiles;
    exec_ms: Percentiles;
  };
};

type ExecutionDetail = {
  exec_id: string;
  sandbox_id: string;
  state: string;
  exit_code: number | null;
  stdout: string;
  stderr: string;
  started_at: string | null;
  finished_at: string | null;
  truncated: boolean;
  result_code: string | null;
  traceparent: string | null;
};

type Snapshot = {
  generated_at: string;
  viewer?: string;
  queue: Envelope<QueueData>;
  nodes: Envelope<NodeData>;
  sandboxes: Envelope<SandboxData>;
  executions: Envelope<ExecutionsData>;
};

type Zone = "overview" | "nodes" | "sandboxes" | "execs";

const ZONES: Array<{ key: Zone; label: string }> = [
  { key: "overview", label: "总览" },
  { key: "nodes", label: "节点" },
  { key: "sandboxes", label: "沙箱" },
  { key: "execs", label: "执行" },
];

function timeText(iso: string | null | undefined): string {
  if (!iso) return "—";

  const value = new Date(iso);

  if (Number.isNaN(value.getTime())) return iso;

  return value.toLocaleString("zh-CN", { hour12: false });
}

function durationText(ms: number | null): string {
  if (ms === null) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.round(ms)} ms`;
}

function rateText(value: number | null): string {
  if (value === null) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function countText(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : String(value);
}

const card: React.CSSProperties = {
  border: `1px solid ${color.line}`,
  borderRadius: 12,
  padding: "14px 16px",
  background: "#fff",
  marginBottom: 14,
};

const h2: React.CSSProperties = { fontSize: 15, margin: "0 0 10px" };
const muted: React.CSSProperties = { color: color.muted, fontSize: 12.5 };
const row: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: 12,
  padding: "6px 0",
  borderBottom: `1px dashed ${color.line}`,
  fontSize: 13,
};
const mono: React.CSSProperties = { fontFamily: "ui-monospace, Consolas, monospace" };

const th: React.CSSProperties = {
  textAlign: "left",
  fontSize: 12,
  color: color.muted,
  fontWeight: 600,
  padding: "6px 10px 6px 0",
  borderBottom: `1px solid ${color.line}`,
  whiteSpace: "nowrap",
};

const td: React.CSSProperties = {
  fontSize: 13,
  padding: "6px 10px 6px 0",
  borderBottom: `1px solid ${color.soft}`,
  whiteSpace: "nowrap",
};

function Card({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section style={card}>
      <h2 style={h2}>{title}</h2>
      {subtitle ? <p style={{ ...muted, margin: "0 0 10px" }}>{subtitle}</p> : null}
      {children}
    </section>
  );
}

function Chip({
  label,
  value,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  tone?: string;
}) {
  return (
    <div
      style={{
        border: `1px solid ${color.line}`,
        borderRadius: 10,
        padding: "8px 10px",
        minWidth: 96,
        flex: "1 1 96px",
      }}
    >
      <div style={{ ...muted, marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 600, color: tone ?? color.text }}>
        {value}
      </div>
    </div>
  );
}

function BlockError({ error }: { error: { code: string; message: string } | null }) {
  if (!error) return null;

  return (
    <div
      role="alert"
      style={{
        border: `1px solid #fecdca`,
        background: "#fff6f5",
        color: color.danger,
        borderRadius: 8,
        padding: "8px 10px",
        fontSize: 12.5,
      }}
    >
      区块加载失败（{error.code}）：{error.message}
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <p style={{ ...muted, margin: "8px 0 0" }}>{text}</p>;
}

function Scroll({ children }: { children: React.ReactNode }) {
  return <div style={{ overflowX: "auto", width: "100%" }}>{children}</div>;
}

type Notice = { tone: "ok" | "error"; text: string } | null;

function NoticeBar({ notice }: { notice: Notice }) {
  if (!notice) return null;

  return (
    <div
      data-testid="console-notice"
      role="status"
      style={{
        border: `1px solid ${notice.tone === "ok" ? "#abefc6" : "#fecdca"}`,
        background: notice.tone === "ok" ? "#ecfdf3" : "#fff6f5",
        color: notice.tone === "ok" ? color.ok : color.danger,
        borderRadius: 8,
        padding: "8px 10px",
        fontSize: 12.5,
        marginBottom: 12,
      }}
    >
      {notice.text}
    </div>
  );
}

function ConfirmDialog({
  title,
  message,
  confirmText,
  busy,
  onCancel,
  onConfirm,
}: {
  title: string;
  message: string;
  confirmText: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(15, 23, 42, 0.35)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
        zIndex: 50,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        style={{
          background: "#fff",
          borderRadius: 12,
          padding: 18,
          maxWidth: 440,
          width: "100%",
          boxShadow: "0 12px 32px rgba(15, 23, 42, 0.18)",
        }}
      >
        <h3 style={{ margin: "0 0 8px", fontSize: 16 }}>{title}</h3>
        <p
          style={{
            margin: "0 0 14px",
            fontSize: 13,
            color: color.muted,
            whiteSpace: "pre-wrap",
          }}
        >
          {message}
        </p>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button type="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            style={{ color: color.danger }}
          >
            {busy ? "执行中…" : confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}

function ExecDetailDialog({
  detail,
  onClose,
}: {
  detail: ExecutionDetail;
  onClose: () => void;
}) {
  const block: React.CSSProperties = {
    background: color.soft,
    border: `1px solid ${color.line}`,
    borderRadius: 8,
    padding: 10,
    fontSize: 12,
    fontFamily: "ui-monospace, Consolas, monospace",
    whiteSpace: "pre-wrap",
    wordBreak: "break-all",
    maxHeight: 160,
    overflow: "auto",
    margin: "0 0 10px",
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(15, 23, 42, 0.35)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
        zIndex: 50,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="执行详情"
        style={{
          background: "#fff",
          borderRadius: 12,
          padding: 18,
          maxWidth: 560,
          width: "100%",
          maxHeight: "85vh",
          overflowY: "auto",
          boxShadow: "0 12px 32px rgba(15, 23, 42, 0.18)",
        }}
      >
        <h3 style={{ margin: "0 0 10px", fontSize: 16, ...mono }}>
          {detail.exec_id}
        </h3>

        <div style={row}>
          <span>状态</span>
          <strong>
            {detail.state} · exit {detail.exit_code ?? "—"}
            {detail.truncated ? " · 输出被截断" : ""}
          </strong>
        </div>
        <div style={row}>
          <span>结果码</span>
          <span style={mono}>{detail.result_code ?? "—"}</span>
        </div>
        <div style={row}>
          <span>开始 / 结束</span>
          <span>
            {timeText(detail.started_at)} / {timeText(detail.finished_at)}
          </span>
        </div>
        <div style={{ ...row, borderBottom: "none" }}>
          <span>traceparent</span>
          <span style={{ ...mono, fontSize: 11 }}>
            {detail.traceparent ?? "—"}
          </span>
        </div>

        <p style={{ ...muted, margin: "10px 0 4px" }}>stdout</p>
        <pre style={block}>{detail.stdout || "（空）"}</pre>

        <p style={{ ...muted, margin: "0 0 4px" }}>stderr</p>
        <pre style={block}>{detail.stderr || "（空）"}</pre>

        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <button type="button" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}

export default function PlatformView({ authToken }: { authToken?: string }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [zone, setZone] = useState<Zone>("overview");
  const [updatedAt, setUpdatedAt] = useState<string>("");

  // P1：写操作只对 owner 开放；角色从 /api/auth/me 取，不靠前端猜。
  const [role, setRole] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState<{
    title: string;
    message: string;
    confirmText: string;
    run: () => Promise<void>;
  } | null>(null);

  const [workspaceId, setWorkspaceId] = useState("console-demo");
  const [ttlSeconds, setTtlSeconds] = useState("600");
  const [execSandbox, setExecSandbox] = useState("");
  const [execCommand, setExecCommand] = useState("cmd /c echo hello");

  // 执行详情下钻
  const [execDetail, setExecDetail] = useState<ExecutionDetail | null>(null);
  const [execDetailBusy, setExecDetailBusy] = useState(false);

  const load = useCallback(async () => {
    if (!authToken) return;

    setLoading(true);
    setError("");

    try {
      const response = await fetch("/api/console/snapshot?limit=10", {
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${authToken}`,
        },
        cache: "no-store",
      });

      if (response.status === 401) {
        setError("登录已过期，请重新登录后再打开控制台");
        return;
      }

      if (!response.ok) {
        setError(`控制台接口返回 ${response.status}`);
        return;
      }

      if (!(response.headers.get("content-type") ?? "").includes("application/json")) {
        setError("控制台接口返回了非 JSON，请检查代理配置");
        return;
      }

      setSnapshot((await response.json()) as Snapshot);
      setUpdatedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    } catch {
      setError("无法连接控制台接口");
    } finally {
      setLoading(false);
    }
  }, [authToken]);

  useEffect(() => {
    if (!authToken) return;

    void load();

    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, POLL_MS);

    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };

    document.addEventListener("visibilitychange", onVisible);

    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [authToken, load]);

  // 角色从后端取：前端不靠“藏按钮”当权限，后端每个写接口仍会校验 owner。
  useEffect(() => {
    if (!authToken) return;

    let alive = true;

    fetch("/api/auth/me", {
      headers: { Authorization: `Bearer ${authToken}` },
      cache: "no-store",
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((data: { user?: { role?: string } } | null) => {
        if (alive && data?.user?.role) setRole(data.user.role);
      })
      .catch(() => undefined);

    return () => {
      alive = false;
    };
  }, [authToken]);

  const canWrite = role === "owner";

  const post = useCallback(
    async (path: string, body?: unknown) => {
      if (!authToken) {
        return { ok: false as const, message: "缺少登录凭据" };
      }

      try {
        const response = await fetch(`/api${path}`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${authToken}`,
          },
          body: body === undefined ? undefined : JSON.stringify(body),
        });

        const payload = (await response.json().catch(() => null)) as {
          error?: { message?: string };
          detail?: string;
        } | null;

        if (!response.ok) {
          return {
            ok: false as const,
            message:
              payload?.error?.message ??
              payload?.detail ??
              `请求失败（${response.status}）`,
          };
        }

        return { ok: true as const, data: payload };
      } catch {
        return { ok: false as const, message: "无法连接后端" };
      }
    },
    [authToken],
  );

  const runAction = useCallback(
    async (
      label: string,
      action: () => Promise<
        { ok: true; data: unknown } | { ok: false; message: string }
      >,
    ) => {
      setBusy(label);
      setNotice(null);

      try {
        const result = await action();

        if (!result.ok) {
          setNotice({ tone: "error", text: `${label}：${result.message}` });
          return;
        }

        setNotice({ tone: "ok", text: `${label}：已完成` });
        await load();
      } finally {
        setBusy("");
      }
    },
    [load],
  );

  const openExecDetail = useCallback(
    async (execId: string) => {
      if (!authToken) return;

      setExecDetailBusy(true);
      setNotice(null);

      try {
        const response = await fetch(`/api/console/executions/${execId}`, {
          headers: { Authorization: `Bearer ${authToken}` },
          cache: "no-store",
        });
        const data = (await response.json().catch(() => null)) as
          | ExecutionDetail
          | { error?: { message?: string } }
          | null;

        if (!response.ok) {
          const message =
            data && "error" in data ? data.error?.message : null;

          setNotice({
            tone: "error",
            text: `执行详情：${message ?? `请求失败（${response.status}）`}`,
          });
          return;
        }

        setExecDetail(data as ExecutionDetail);
      } catch {
        setNotice({ tone: "error", text: "执行详情：无法连接后端" });
      } finally {
        setExecDetailBusy(false);
      }
    },
    [authToken],
  );

  const queue = snapshot?.queue.data ?? null;
  const nodes = snapshot?.nodes.data ?? null;
  const sandboxes = snapshot?.sandboxes.data ?? null;
  const executions = snapshot?.executions.data ?? null;

  if (!authToken) {
    return (
      <section style={{ padding: 20 }}>
        <p style={muted}>需要登录后才能查看平台控制台。</p>
      </section>
    );
  }

  return (
    <section
      aria-label="平台控制台"
      style={{ padding: "18px 20px", maxWidth: 1180, width: "100%", boxSizing: "border-box" }}
    >
      <header
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 12,
          alignItems: "flex-end",
          justifyContent: "space-between",
          marginBottom: 12,
          paddingRight: 4,
        }}
      >
        <div style={{ minWidth: 240 }}>
          <h2 style={{ margin: 0 }}>平台控制台</h2>
          <p style={{ ...muted, margin: "4px 0 0" }}>
            队列 / 节点 / 沙箱 / 执行结果，每 {POLL_MS / 1000} 秒刷新，
            {updatedAt ? ` 最近 ${updatedAt}` : " 尚未取到数据"}。
          </p>
        </div>

        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button type="button" onClick={() => void load()} disabled={loading}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </header>

      {error ? (
        <div style={{ ...card, color: color.danger }} role="alert">
          {error}
        </div>
      ) : null}

      {confirm ? (
        <ConfirmDialog
          title={confirm.title}
          message={confirm.message}
          confirmText={confirm.confirmText}
          busy={busy !== ""}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            const action = confirm.run;
            setConfirm(null);
            void action();
          }}
        />
      ) : null}

      {execDetail ? (
        <ExecDetailDialog
          detail={execDetail}
          onClose={() => setExecDetail(null)}
        />
      ) : null}

      {!snapshot && !error ? <div style={card}>加载中…</div> : null}

      {snapshot ? (
        <>
          <NoticeBar notice={notice} />

          <nav
            style={{
              display: "flex",
              gap: 6,
              flexWrap: "wrap",
              marginBottom: 14,
            }}
          >
            {ZONES.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setZone(item.key)}
                aria-pressed={zone === item.key}
                style={{
                  padding: "6px 12px",
                  borderRadius: 999,
                  border: `1px solid ${zone === item.key ? color.info : color.line}`,
                  background: zone === item.key ? "#eff4ff" : "#fff",
                  color: zone === item.key ? color.info : color.text,
                  fontSize: 13,
                  cursor: "pointer",
                }}
              >
                {item.label}
              </button>
            ))}
          </nav>

          {zone === "overview" ? (
            <>
              <Card
                title="当前状态"
                subtitle="先回答：队列堵不堵、执行成功率如何。"
              >
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  <Chip
                    label="活跃 / 排队"
                    value={`${countText(queue?.active)} / ${countText(queue?.queued)}`}
                    tone={
                      queue && queue.queued > 0 ? color.warn : color.text
                    }
                  />
                  <Chip
                    label="成功率（15m）"
                    value={rateText(executions?.window.success_rate.value ?? null)}
                    tone={color.text}
                  />
                  <Chip
                    label="节点"
                    value={`${countText(nodes?.summary.nodes_accepting)} / ${countText(nodes?.summary.nodes_total)} 可用`}
                  />
                  <Chip
                    label="活跃沙箱"
                    value={countText(sandboxes?.active_total)}
                  />
                </div>

                <BlockError error={snapshot.queue.error} />
              </Card>
            </>
          ) : null}

          {zone === "nodes" ? (
            <Card
              title="节点"
              subtitle="只有 accepting 的节点计入实际容量；cordon / drain / 过期单独标注。"
            >
              <BlockError error={snapshot.nodes.error} />

              {nodes ? (
                <>
                  <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
                    <Chip label="总数" value={nodes.summary.nodes_total} />
                    <Chip
                      label="可用"
                      value={nodes.summary.nodes_accepting}
                      tone={color.ok}
                    />
                    <Chip
                      label="cordon"
                      value={nodes.summary.nodes_cordoned}
                      tone={color.warn}
                    />
                    <Chip label="draining" value={nodes.summary.nodes_draining} />
                    <Chip label="租约过期" value={nodes.summary.nodes_expired} />
                    <Chip label="可调度槽位" value={nodes.summary.slots_accepting} />
                  </div>

                  {nodes.rows.length === 0 ? (
                    <Empty text="还没有注册节点。用 /v1/nodes 注册，或打开控制器让 apply 模式自动扩容。" />
                  ) : (
                    <Scroll>
                      <table style={{ borderCollapse: "collapse", width: "100%" }}>
                        <thead>
                          <tr>
                            <th style={th}>节点</th>
                            <th style={th}>状态</th>
                            <th style={th}>容量</th>
                            <th style={th}>活跃沙箱</th>
                            <th style={th}>可接新任务</th>
                            <th style={th}>心跳</th>
                            <th style={th}>操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          {nodes.rows.map((node) => (
                            <tr key={node.node_id}>
                              <td style={{ ...td, ...mono }}>{node.node_id}</td>
                              <td style={td}>
                                {node.state}
                                {node.cordoned ? " · cordon" : ""}
                                {node.draining ? " · drain" : ""}
                                {node.expired ? " · 过期" : ""}
                              </td>
                              <td style={td}>{node.capacity}</td>
                              <td style={td}>{node.active_sandboxes}</td>
                              <td style={{ ...td, color: node.accepting ? color.ok : color.muted }}>
                                {node.accepting ? "是" : "否"}
                              </td>
                              <td style={td}>
                                {node.seconds_since_heartbeat === null
                                  ? "—"
                                  : `${node.seconds_since_heartbeat}s 前`}
                              </td>
                              <td style={td}>
                                {canWrite ? (
                                  <span style={{ display: "inline-flex", gap: 6 }}>
                                    <button
                                      type="button"
                                      aria-label={`cordon ${node.node_id}`}
                                      disabled={busy !== "" || node.cordoned}
                                      onClick={() =>
                                        setConfirm({
                                          title: "停止接新任务（cordon）",
                                          message: `节点：${node.node_id}\n当前活跃沙箱：${node.active_sandboxes}\n影响：不再接收新沙箱；已有沙箱与执行照常跑。`,
                                          confirmText: "确认 cordon",
                                          run: () =>
                                            runAction("cordon 节点", () =>
                                              post(
                                                `/console/actions/nodes/${node.node_id}/cordon`,
                                              ),
                                            ),
                                        })
                                      }
                                    >
                                      cordon
                                    </button>
                                    <button
                                      type="button"
                                      aria-label={`drain ${node.node_id}`}
                                      disabled={busy !== "" || node.draining}
                                      onClick={() =>
                                        setConfirm({
                                          title: "排空节点（drain）",
                                          message: `节点：${node.node_id}\n当前活跃沙箱：${node.active_sandboxes}\n影响：不再接新沙箱；存量沙箱回收后才允许下线。`,
                                          confirmText: "确认 drain",
                                          run: () =>
                                            runAction("drain 节点", () =>
                                              post(
                                                `/console/actions/nodes/${node.node_id}/drain`,
                                              ),
                                            ),
                                        })
                                      }
                                    >
                                      drain
                                    </button>
                                  </span>
                                ) : (
                                  "—"
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </Scroll>
                  )}
                </>
              ) : null}
            </Card>
          ) : null}

          {zone === "sandboxes" ? (
            <Card title="沙箱" subtitle="状态、归属节点、创建与到期时间。">
              <BlockError error={snapshot.sandboxes.error} />

              {canWrite ? (
                <div
                  style={{
                    display: "flex",
                    gap: 10,
                    flexWrap: "wrap",
                    alignItems: "flex-end",
                    marginBottom: 12,
                  }}
                >
                  <label style={muted}>
                    工作区
                    <input
                      data-testid="sandbox-workspace"
                      value={workspaceId}
                      onChange={(event) => setWorkspaceId(event.target.value)}
                      style={{ display: "block", marginTop: 4, padding: "5px 8px" }}
                    />
                  </label>
                  <label style={muted}>
                    TTL（秒）
                    <input
                      data-testid="sandbox-ttl"
                      type="number"
                      min={1}
                      value={ttlSeconds}
                      onChange={(event) => setTtlSeconds(event.target.value)}
                      style={{ display: "block", marginTop: 4, padding: "5px 8px", width: 110 }}
                    />
                  </label>
                  <button
                    data-testid="sandbox-submit"
                    type="button"
                    disabled={busy !== ""}
                    onClick={() =>
                      runAction("创建沙箱", () =>
                        post("/console/actions/sandboxes", {
                          tier: "tier1",
                          workspace_id: workspaceId,
                          ttl_seconds: Number(ttlSeconds) || 600,
                        }),
                      )
                    }
                  >
                    创建沙箱
                  </button>
                </div>
              ) : (
                <p style={{ ...muted, margin: "0 0 10px" }}>
                  只读：写操作需要站长权限。
                </p>
              )}

              {sandboxes ? (
                <>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
                    {Object.keys(sandboxes.counts).length === 0 ? (
                      <span style={muted}>暂无沙箱</span>
                    ) : (
                      Object.entries(sandboxes.counts).map(([state, value]) => (
                        <span
                          key={state}
                          style={{
                            border: `1px solid ${color.line}`,
                            borderRadius: 999,
                            padding: "4px 10px",
                            fontSize: 12.5,
                          }}
                        >
                          {state} {value}
                        </span>
                      ))
                    )}
                  </div>

                  {sandboxes.rows.length === 0 ? (
                    <Empty text="还没有沙箱。" />
                  ) : (
                    <Scroll>
                      <table style={{ borderCollapse: "collapse", width: "100%" }}>
                        <thead>
                          <tr>
                            <th style={th}>ID</th>
                            <th style={th}>状态</th>
                            <th style={th}>节点</th>
                            <th style={th}>层级</th>
                            <th style={th}>创建</th>
                            <th style={th}>到期</th>
                            <th style={th}>操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sandboxes.rows.map((item) => (
                            <tr key={item.id}>
                              <td style={{ ...td, ...mono }}>{item.id}</td>
                              <td style={td}>{item.state}</td>
                              <td style={{ ...td, ...mono }}>{item.node_id ?? "—"}</td>
                              <td style={td}>{item.tier}</td>
                              <td style={td}>{timeText(item.created_at)}</td>
                              <td style={td}>{timeText(item.expires_at)}</td>
                              <td style={td}>
                                {canWrite && item.state !== "reclaimed" ? (
                                  <button
                                    type="button"
                                    aria-label={`销毁 ${item.id}`}
                                    disabled={busy !== ""}
                                    onClick={() =>
                                      setConfirm({
                                        title: "销毁沙箱",
                                        message: `对象：${item.id}\n当前状态：${item.state}\n销毁后沙箱不可恢复，正在执行的命令会被终止。`,
                                        confirmText: "确认销毁",
                                        run: () =>
                                          runAction("销毁沙箱", () =>
                                            post(
                                              `/console/actions/sandboxes/${item.id}/destroy`,
                                            ),
                                          ),
                                      })
                                    }
                                  >
                                    销毁
                                  </button>
                                ) : (
                                  "—"
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </Scroll>
                  )}
                </>
              ) : null}
            </Card>
          ) : null}

          {zone === "execs" ? (
            <>
              <Card
                title={`执行（窗口 ${executions?.window.window_seconds ?? 0} 秒）`}
                subtitle="成功率分母不含取消；百分位样本不足时不出数。"
              >
                <BlockError error={snapshot.executions.error} />

                {executions ? (
                  <>
                    <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
                      <Chip
                        label="成功率"
                        value={rateText(executions.window.success_rate.value)}
                      />
                      <Chip
                        label="成功 / 分母"
                        value={`${executions.window.success_rate.numerator} / ${executions.window.success_rate.denominator}`}
                      />
                      <Chip
                        label="取消（不计入）"
                        value={executions.window.success_rate.excluded_cancelled}
                      />
                      <Chip label="窗口样本" value={executions.window.samples} />
                    </div>

                    <div style={row}>
                      <span>队列等待 p50 / p99</span>
                      <span>
                        {durationText(executions.window.queue_wait_ms.p50)} /{" "}
                        {durationText(executions.window.queue_wait_ms.p99)}
                        <span style={muted}>
                          {" "}
                          （样本 {executions.window.queue_wait_ms.samples}
                          {executions.window.queue_wait_ms.insufficient_samples
                            ? "，样本不足"
                            : ""}）
                        </span>
                      </span>
                    </div>
                    <div style={{ ...row, borderBottom: "none" }}>
                      <span>执行时延 p50 / p99</span>
                      <span>
                        {durationText(executions.window.exec_ms.p50)} /{" "}
                        {durationText(executions.window.exec_ms.p99)}
                        <span style={muted}>
                          {" "}
                          （样本 {executions.window.exec_ms.samples}
                          {executions.window.exec_ms.insufficient_samples
                            ? "，样本不足"
                            : ""}）
                        </span>
                      </span>
                    </div>

                    <p style={{ ...muted, margin: "10px 0 0" }}>
                      {executions.window.success_rate.note}
                    </p>
                  </>
                ) : null}
              </Card>

              {canWrite ? (
                <Card title="提交执行" subtitle="命令按空格切分；P1 不解析引号。">
                  <div
                    style={{
                      display: "flex",
                      gap: 10,
                      flexWrap: "wrap",
                      alignItems: "flex-end",
                    }}
                  >
                    <label style={muted}>
                      沙箱
                      <select
                        data-testid="exec-sandbox"
                        value={execSandbox}
                        onChange={(event) => setExecSandbox(event.target.value)}
                        style={{ display: "block", marginTop: 4, padding: "5px 8px" }}
                      >
                        <option value="">选择沙箱</option>
                        {(sandboxes?.rows ?? []).map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.id}（{item.state}）
                          </option>
                        ))}
                      </select>
                    </label>
                    <label style={muted}>
                      命令
                      <input
                        data-testid="exec-command"
                        value={execCommand}
                        onChange={(event) => setExecCommand(event.target.value)}
                        style={{
                          display: "block",
                          marginTop: 4,
                          padding: "5px 8px",
                          minWidth: 260,
                        }}
                      />
                    </label>
                    <button
                      data-testid="exec-submit"
                      type="button"
                      disabled={execSandbox === "" || busy !== ""}
                      onClick={() =>
                        runAction("提交执行", () =>
                          post("/console/actions/executions", {
                            sandbox_id: execSandbox,
                            command: execCommand
                              .trim()
                              .split(/\s+/)
                              .filter(Boolean),
                          }),
                        )
                      }
                    >
                      提交执行
                    </button>
                  </div>
                </Card>
              ) : null}

              <Card
                title="执行详情"
                subtitle="最近 10 条；点「详情」按 ID 读取 stdout / stderr / trace。"
              >
                {executions && executions.recent.length > 0 ? (
                  <Scroll>
                    <table style={{ borderCollapse: "collapse", width: "100%" }}>
                      <thead>
                        <tr>
                          <th style={th}>ID</th>
                          <th style={th}>状态</th>
                          <th style={th}>exit</th>
                          <th style={th}>创建</th>
                          <th style={th}>结束</th>
                          <th style={th}>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {executions.recent.map((item) => (
                          <tr key={item.id}>
                            <td style={{ ...td, ...mono }}>{item.id}</td>
                            <td style={td}>{item.state}</td>
                            <td style={td}>{item.exit_code ?? "—"}</td>
                            <td style={td}>{timeText(item.created_at)}</td>
                            <td style={td}>{timeText(item.finished_at)}</td>
                            <td style={td}>
                              <span style={{ display: "inline-flex", gap: 6 }}>
                                <button
                                  type="button"
                                  aria-label={`详情 ${item.id}`}
                                  disabled={execDetailBusy}
                                  onClick={() => void openExecDetail(item.id)}
                                >
                                  详情
                                </button>
                                {canWrite &&
                                (item.state === "queued" ||
                                  item.state === "running") ? (
                                  <button
                                    type="button"
                                    aria-label={`取消 ${item.id}`}
                                    disabled={busy !== ""}
                                    onClick={() =>
                                      setConfirm({
                                        title: "取消执行",
                                        message: `对象：${item.id}\n当前状态：${item.state}\n取消会终止正在运行的进程；已终态的执行重复取消不会产生第二条结果。`,
                                        confirmText: "确认取消",
                                        run: () =>
                                          runAction("取消执行", () =>
                                            post(
                                              `/console/actions/executions/${item.id}/cancel`,
                                            ),
                                          ),
                                      })
                                    }
                                  >
                                    取消
                                  </button>
                                ) : null}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </Scroll>
                ) : (
                  <Empty text="窗口内没有执行记录。" />
                )}
              </Card>
            </>
          ) : null}

        </>
      ) : null}
    </section>
  );
}
