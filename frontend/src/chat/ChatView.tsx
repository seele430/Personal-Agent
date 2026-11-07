// 聊天视图（从 App.tsx 拆出，W4）
//
// 只负责渲染：消息列表、工具审批卡、生成中提示、输入框。
// 状态与请求仍留在 App 里 —— 拆视图是为了后续加产物卡片与摘要卡，
// 不是为了顺手重写聊天逻辑。

import type {
  FormEvent,
  KeyboardEvent,
  RefObject,
} from "react";

import ArtifactCard from "../components/ArtifactCard";
import ActivitySummary from "../components/ActivitySummary";
import type {
  ActivitySummaryPayload,
  ArtifactMeta,
  Message,
} from "./types";

type PendingApproval = {
  tool: string;
  preview: string;
};

type ChatMessagesProps = {
  messages: Message[];
  pendingApproval: PendingApproval | null;
  decideApproval: (decision: "approve" | "reject") => void;
  sending: boolean;
  activeTool: string | null;
  finishedTool: string | null;
  checkingId: string | null;
  queryStatus: (requestId: string) => void;
  bottomRef: RefObject<HTMLDivElement | null>;
  artifacts: ArtifactMeta[];
  activeArtifactId: string | null;
  onOpenArtifact: (artifactId: string) => void;
  activity: ActivitySummaryPayload | null;
};

export function ChatMessages({
  messages,
  pendingApproval,
  decideApproval,
  sending,
  activeTool,
  finishedTool,
  checkingId,
  queryStatus,
  bottomRef,
  artifacts,
  activeArtifactId,
  onOpenArtifact,
  activity,
}: ChatMessagesProps) {
  return (
    <>
      <section className="chat-area">
        {messages.length === 0 && (
          <div className="welcome">
            <div className="welcome-logo">✳</div>

            <h1>今天有什么新想法？</h1>

            <p>
              欢迎来到 Personal Agent。
              <br />
              你的智能工作空间，从一次对话开始。
            </p>

            <div className="welcome-tip">
              一个简洁、可靠、不断成长的 AI 助手
            </div>
          </div>
        )}

        <ActivitySummary summary={activity} />

        {artifacts.length > 0 && (
          <section
            className="artifact-strip"
            aria-label="本次产物"
            data-testid="artifact-strip"
          >
            <header className="artifact-strip-head">
              <span>本次产物 · {artifacts.length}</span>
            </header>

            <div className="artifact-cards">
              {artifacts.map((artifact) => (
                <ArtifactCard
                  key={artifact.id}
                  artifact={artifact}
                  active={artifact.id === activeArtifactId}
                  onOpen={onOpenArtifact}
                />
              ))}
            </div>
          </section>
        )}

        <div className="message-container">
          {messages.map((message) => (
            <article className={`message ${message.role}`} key={message.id}>
              <div className="message-avatar">
                {message.role === "assistant" ? "✦" : "我"}
              </div>

              <div className="message-body">
                <div className="message-name">
                  {message.role === "assistant" ? "Personal Agent" : "你"}
                </div>

                <div className="message-content">{message.content}</div>

                {message.status === "pending" && (
                  <div className="message-status">等待回复…</div>
                )}

                {message.status === "unknown" && (
                  <div className="message-status warning">请求结果尚未确认</div>
                )}

                {message.status === "rejected" && (
                  <div className="message-status warning">
                    未发送成功 · AI 未执行
                  </div>
                )}

                {message.status === "cancelled" && (
                  <div className="message-status">已取消本次回复</div>
                )}

                {message.role === "user" &&
                  message.requestId &&
                  (message.status === "pending" ||
                    message.status === "unknown") && (
                    <button
                      type="button"
                      onClick={() => void queryStatus(message.requestId!)}
                      disabled={checkingId === message.requestId}
                      aria-label="查询结果"
                    >
                      {checkingId === message.requestId ? "查询中…" : "查询结果"}
                    </button>
                  )}
              </div>
            </article>
          ))}

          {pendingApproval && (
            <div
              className="approval-card"
              role="alertdialog"
              aria-label="工具审批"
            >
              <div className="approval-title">
                工具「{pendingApproval.tool}」需要你的批准
              </div>
              <pre className="approval-preview">{pendingApproval.preview}</pre>
              <div className="approval-actions">
                <button type="button" onClick={() => decideApproval("approve")}>
                  批准执行
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => decideApproval("reject")}
                >
                  拒绝
                </button>
              </div>
            </div>
          )}

          {sending && (
            <div className="loading" role="status">
              <div className="spinner" />

              <span>
                {pendingApproval
                  ? "等待你批准工具调用…"
                  : activeTool
                    ? `正在执行工具：${activeTool}（Esc 停止）`
                    : finishedTool
                      ? `工具 ${finishedTool} 已完成，正在生成回答…`
                      : "助手正在生成，按 Esc 停止…"}
              </span>
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      </section>
    </>
  );
}

type ComposerProps = {
  error: string;
  input: string;
  setInput: (value: string) => void;
  sending: boolean;
  handleKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  sendMessage: (event: FormEvent<HTMLFormElement>) => void;
  cancelActiveRun: () => void;
};

export function Composer({
  error,
  input,
  setInput,
  sending,
  handleKeyDown,
  sendMessage,
  cancelActiveRun,
}: ComposerProps) {
  return (
    <div className="input-section">
      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}

      <form className="input-box" onSubmit={sendMessage}>
        <textarea
          aria-label="输入聊天消息"
          placeholder="向 Personal Agent 发送消息..."
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={sending}
          maxLength={8000}
          rows={2}
        />

        <div className="input-actions">
          <span>Enter 发送 · Shift + Enter 换行</span>

          {sending ? (
            <button type="button" onClick={cancelActiveRun} aria-label="停止生成">
              停止 Esc
            </button>
          ) : (
            <button
              type="submit"
              disabled={!input.trim()}
              aria-label="发送 ↑"
            >
              发送 ↑
            </button>
          )}
        </div>
      </form>

      <p className="footer-note">
        当前为本地开发版 · 聊天记录暂存于当前浏览器标签页
      </p>
    </div>
  );
}
