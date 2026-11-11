import { useCallback, useEffect, useRef, useState } from "react";

import "./MemoryView.css";

type MemoryItem = {
  id: number;
  key: string;
  kind: string;
  layer: string;
  text: string;
  trust: string;
  valid_from: string;
  valid_to: string | null;
  supersedes_id: number | null;
  created_at: string;
};

type HistoryRow = MemoryItem & {
  deleted_at: string | null;
  is_current: boolean;
  version_status: string;
};

type History = {
  anchor_id: number;
  current_id: number | null;
  items: HistoryRow[];
  count: number;
  total_count: number;
  truncated: boolean;
  window_has_current: boolean;
};

const STATUS_TEXT: Record<string, string> = {
  current: "当前",
  superseded: "已被取代",
  expired: "已过期",
  future: "未生效",
  deleted: "已删除",
};

const TRUST_TEXT: Record<string, string> = {
  user_confirmed: "用户确认",
  model_inferred: "模型推断",
  external: "外部来源",
  unverified: "未验证",
};

export default function MemoryView({ authToken }: { authToken?: string }) {
  const token = authToken ?? "";
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [mode, setMode] = useState<"list" | "search">("list");
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<MemoryItem | null>(null);
  const [history, setHistory] = useState<History | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [busy, setBusy] = useState(false);
  const requestRef = useRef(0);
  const detailRef = useRef(0);

  const load = useCallback(async (nextMode: "list" | "search", nextQuery: string, nextOffset: number) => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setLoading(true);
    setError("");

    try {
      const params = new URLSearchParams({ limit: "20" });

      if (nextMode === "search") {
        params.set("query", nextQuery);
        params.set("limit", "50");
      } else {
        params.set("offset", String(nextOffset));
      }

      const response = await fetch(`/api/memory/items?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (requestRef.current !== requestId) return;

      if (!response.ok) {
        throw new Error(`加载失败（${response.status}）`);
      }

      const data = await response.json();

      if (requestRef.current !== requestId) return;

      setItems(data.items ?? []);
      setTotal(data.mode === "search" ? data.count ?? 0 : data.total ?? 0);
      setMode(data.mode === "search" ? "search" : "list");
    } catch (cause) {
      if (requestRef.current !== requestId) return;

      setError(cause instanceof Error ? cause.message : "加载失败");
    } finally {
      if (requestRef.current === requestId) setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load("list", "", 0);
  }, [load]);

  async function openDetail(item: MemoryItem) {
    const requestId = detailRef.current + 1;
    detailRef.current = requestId;
    setDetail(item);
    setHistory(null);
    setHistoryError("");
    setEditing(false);
    setEditText(item.text);

    try {
      const [detailResponse, historyResponse] = await Promise.all([
        fetch(`/api/memory/items/${item.id}`, { headers: { Authorization: `Bearer ${token}` } }),
        fetch(`/api/memory/items/${item.id}/history`, { headers: { Authorization: `Bearer ${token}` } }),
      ]);

      if (detailRef.current !== requestId) return;

      if (detailResponse.ok) {
        setDetail(await detailResponse.json());
      }

      if (historyResponse.ok) {
        setHistory(await historyResponse.json());
      } else {
        setHistoryError("历史链异常或过长，无法完整展示");
      }
    } catch {
      setHistoryError("历史加载失败");
    }
  }

  async function saveEdit() {
    if (!detail) return;
    setBusy(true);

    try {
      const response = await fetch("/api/memory/items", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          text: editText,
          key: detail.key,
          kind: detail.kind,
          layer: detail.layer,
        }),
      });

      if (!response.ok) {
        throw new Error(`保存失败（${response.status}）`);
      }

      const created = await response.json();
      setEditing(false);
      setDetail({ ...detail, ...created, text: editText });
      const historyResponse = await fetch(`/api/memory/items/${created.id}/history`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (historyResponse.ok) {
        setHistory(await historyResponse.json());
      }

      void load("list", "", 0);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function removeItem() {
    if (!detail) return;

    if (!window.confirm(`确认删除记忆 #${detail.id}？`)) return;

    setBusy(true);

    try {
      const response = await fetch(`/api/memory/items/${detail.id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });

      if (!response.ok) {
        throw new Error(`删除失败（${response.status}）`);
      }

      setDetail(null);
      setHistory(null);
      void load("list", "", 0);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "删除失败");
    } finally {
      setBusy(false);
    }
  }

  function submitSearch() {
    const text = draft.trim();
    setOffset(0);

    if (!text) {
      setQuery("");
      void load("list", "", 0);
      return;
    }

    setQuery(text);
    void load("search", text, 0);
  }

  function clearSearch() {
    setDraft("");
    setQuery("");
    setOffset(0);
    void load("list", "", 0);
  }

  return (
    <section className="memory-panel" aria-label="记忆">
      <header className="memory-head">
        <div>
          <h2>记忆</h2>
          <p className="memory-hint">当前事实列表；旧版本从历史链进入。可编辑或软删除。</p>
        </div>
        <button type="button" onClick={() => void load(mode, query, offset)} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </button>
      </header>

      <div className="memory-search">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => event.key === "Enter" && submitSearch()}
          placeholder="搜索记忆"
          aria-label="搜索记忆"
        />
        <button type="button" onClick={submitSearch}>搜索</button>
        <button type="button" onClick={clearSearch}>清除</button>
        <span className="memory-total">
          {mode === "search" ? `搜索到 ${total} 条（最多 50）` : `共 ${total} 条`}
        </span>
      </div>

      {error && (
        <div className="memory-error">
          {error}
          <button type="button" onClick={() => void load(mode, query, offset)}>重试</button>
        </div>
      )}

      {!loading && items.length === 0 ? (
        <p className="memory-empty">{mode === "search" ? "没有找到相关记忆" : "还没有记忆"}</p>
      ) : (
        <table className="memory-table">
          <thead>
            <tr>
              <th>key</th>
              <th>内容</th>
              <th>信任</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id} onClick={() => void openDetail(item)} className={detail?.id === item.id ? "selected" : ""}>
                <td>{item.key || "无 key"}</td>
                <td>{item.text.length > 80 ? `${item.text.slice(0, 80)}…` : item.text}</td>
                <td>{TRUST_TEXT[item.trust] ?? item.trust}</td>
                <td>{new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {mode === "list" && (
        <div className="memory-pager">
          <button type="button" disabled={offset === 0} onClick={() => {
            const next = Math.max(0, offset - 20);
            setOffset(next);
            void load("list", "", next);
          }}>上一页</button>
          <span>{offset + 1}–{Math.min(offset + 20, total)} / {total}</span>
          <button type="button" disabled={offset + 20 >= total} onClick={() => {
            const next = offset + 20;
            setOffset(next);
            void load("list", "", next);
          }}>下一页</button>
        </div>
      )}

      {detail && (
        <aside className="memory-detail">
          <header>
            <h3>记忆 #{detail.id}</h3>
            <div className="memory-actions">
              <button type="button" onClick={() => { setEditing(true); setEditText(detail.text); }} disabled={busy || !detail.key} title={detail.key ? "" : "无 key 的记忆不能编辑版本"}>编辑</button>
              <button type="button" onClick={() => void removeItem()} disabled={busy}>删除</button>
              <button type="button" onClick={() => { setDetail(null); setHistory(null); }}>关闭</button>
            </div>
          </header>
          {editing ? (
            <div className="memory-edit">
              <textarea value={editText} onChange={(event) => setEditText(event.target.value)} rows={4} aria-label="编辑记忆内容" />
              <div className="memory-actions">
                <button type="button" onClick={() => void saveEdit()} disabled={busy || !editText.trim()}>保存</button>
                <button type="button" onClick={() => setEditing(false)} disabled={busy}>取消</button>
              </div>
            </div>
          ) : (
            <p className="memory-full">{detail.text}</p>
          )}
          {!detail.key && <p className="memory-hint">无 key 的记忆不能编辑版本；如需更新，请重新记住一条。</p>}
          <p>key：{detail.key || "无"}；类型：{detail.kind}；层：{detail.layer}</p>
          <p>信任：{TRUST_TEXT[detail.trust] ?? detail.trust}</p>
          <p>生效：{detail.valid_from}{detail.valid_to ? ` → ${detail.valid_to}` : ""}</p>

          <h4>历史链（旧 → 新）</h4>
          {historyError && <p className="memory-error-text">{historyError}</p>}
          {history && (
            <>
              {history.truncated && (
                <p className="memory-hint">历史较长，仅显示锚点附近的最多 50 个版本。</p>
              )}
              {!history.window_has_current && (
                <p className="memory-hint">当前版本不在本窗口。</p>
              )}
              <ol className="memory-history">
                {history.items.map((row) => (
                  <li key={row.id} className={row.id === history.anchor_id ? "anchor" : ""}>
                    <strong>#{row.id}</strong> {STATUS_TEXT[row.version_status] ?? row.version_status}
                    <span> {row.text.length > 60 ? `${row.text.slice(0, 60)}…` : row.text}</span>
                  </li>
                ))}
              </ol>
            </>
          )}
        </aside>
      )}
    </section>
  );
}
