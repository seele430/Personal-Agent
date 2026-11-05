import { useCallback, useEffect, useState } from "react";

type RouteRow = {
  at?: string;
  intent?: string | null;
  decision?: string;
  confidence?: number;
  risk?: string;
  requires_approval?: boolean;
};

type NodeRow = {
  node_id: string;
  capacity: number;
  state: string;
  cordoned_at: string | null;
};

type ExecRow = {
  id: string;
  state: string;
  exit_code: number | null;
  started_at: string | null;
  finished_at: string | null;
};

type Overview = {
  generated_at: string;
  viewer?: string;
  routes: RouteRow[];
  queue: { active: number; queued: number };
  nodes: NodeRow[];
  sandboxes: Record<string, number>;
  execs: ExecRow[];
};

const card: React.CSSProperties = {
  border: "1px solid #e3e8ef",
  borderRadius: 12,
  padding: "14px 16px",
  background: "#fff",
  marginBottom: 14,
};

const h2: React.CSSProperties = {
  fontSize: 15,
  margin: "0 0 10px",
};

const row: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: 12,
  padding: "6px 0",
  borderBottom: "1px dashed #eef1f5",
  fontSize: 13,
};

export default function PlatformView({
  authToken,
}: {
  authToken?: string;
}) {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!authToken) {
      return;
    }

    setLoading(true);
    setError("");

    try {
      const response = await fetch("/api/console/overview", {
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${authToken}`,
        },
        cache: "no-store",
      });

      if (!response.ok) {
        setError(`控制台接口返回 ${response.status}`);
        return;
      }

      const contentType = response.headers.get("content-type") || "";

      if (!contentType.includes("application/json")) {
        setError("控制台接口返回了非 JSON，请检查代理配置");
        return;
      }

      setData((await response.json()) as Overview);
    } catch {
      setError("无法连接控制台接口");
    } finally {
      setLoading(false);
    }
  }, [authToken]);

  useEffect(() => {
    if (!authToken) {
      return;
    }

    void load();

    const timer = window.setInterval(() => {
      void load();
    }, 10000);

    return () => {
      window.clearInterval(timer);
    };
  }, [authToken, load]);

  return (
    <section
      aria-label="平台控制台"
      style={{ padding: "20px 24px", maxWidth: 1100 }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 12,
          // 右上角是固定的账号/退出胶囊，留出空间避免遮挡「刷新」。
          paddingRight: 220,
        }}
      >
        <div>
          <h2 style={{ margin: 0 }}>平台控制台</h2>
          <p style={{ margin: "4px 0 0", color: "#64748b", fontSize: 13 }}>
            路由决策、队列、节点与最近执行；每 10 秒刷新。
          </p>
        </div>

        <button type="button" onClick={() => void load()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && (
        <div style={{ ...card, color: "#b42318" }} role="alert">
          {error}
        </div>
      )}

      {!data && !error && (
        <div style={card}>加载中…</div>
      )}

      {data && (
        <>
          <div style={card}>
            <h2 style={h2}>队列</h2>
            <div style={row}>
              <span>活跃</span>
              <strong>{data.queue.active}</strong>
            </div>
            <div style={row}>
              <span>排队</span>
              <strong>{data.queue.queued}</strong>
            </div>
            <div style={{ ...row, borderBottom: "none" }}>
              <span>沙箱</span>
              <strong>
                {Object.entries(data.sandboxes)
                  .map(([state, count]) => `${state}:${count}`)
                  .join("  ") || "无"}
              </strong>
            </div>
          </div>

          <div style={card}>
            <h2 style={h2}>最近路由决策</h2>

            {data.routes.length === 0 && <p>暂无</p>}

            {data.routes.map((route, index) => (
              <div key={`${route.at}-${index}`} style={row}>
                <span>
                  {route.intent ?? route.decision} · conf{" "}
                  {route.confidence ?? "-"}
                </span>
                <span>
                  risk={route.risk ?? "-"} · 审批=
                  {String(route.requires_approval ?? false)}
                </span>
              </div>
            ))}
          </div>

          <div style={card}>
            <h2 style={h2}>节点</h2>

            {data.nodes.length === 0 && <p>暂无</p>}

            {data.nodes.map((node) => (
              <div key={node.node_id} style={row}>
                <span>{node.node_id}</span>
                <span>
                  capacity={node.capacity} state={node.state} cordoned=
                  {String(Boolean(node.cordoned_at))}
                </span>
              </div>
            ))}
          </div>

          <div style={card}>
            <h2 style={h2}>最近执行</h2>

            {data.execs.length === 0 && <p>暂无</p>}

            {data.execs.map((exec) => (
              <div key={exec.id} style={row}>
                <span>{exec.id}</span>
                <span>
                  {exec.state} · exit={exec.exit_code ?? "-"}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
