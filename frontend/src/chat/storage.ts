// 聊天的浏览器缓存读写（从 App.tsx 拆出，W4）
//
// 只做 sessionStorage 读写的容错封装：任何异常都退回默认值，
// 缓存失败不该影响聊天本身。

import type { Message } from "./types";

const CHAT_KEY = "personal-agent-dev-chat";
const DRAFT_KEY = "personal-agent-dev-draft";

// 已登录用户使用独立的浏览器缓存。
export function chatStorageKey(userId?: string): string {
  return userId ? `personal-agent-user-${userId}-chat` : CHAT_KEY;
}

export function draftStorageKey(userId?: string): string {
  return userId ? `personal-agent-user-${userId}-draft` : DRAFT_KEY;
}

export function saveMessages(messages: Message[], userId?: string): void {
  try {
    sessionStorage.setItem(
      chatStorageKey(userId),
      JSON.stringify(messages.slice(-100)),
    );
  } catch {
    // 缓存失败不影响聊天
  }
}

export function loadMessages(userId?: string): Message[] {
  try {
    const raw = sessionStorage.getItem(chatStorageKey(userId));

    if (!raw) return [];

    const parsed: unknown = JSON.parse(raw);

    if (!Array.isArray(parsed)) return [];

    return parsed
      .filter(
        (item): item is Message =>
          typeof item === "object" &&
          item !== null &&
          typeof (item as Message).id === "string" &&
          typeof (item as Message).content === "string" &&
          ((item as Message).role === "user" ||
            (item as Message).role === "assistant") &&
          [
            "pending",
            "sent",
            "unknown",
            "rejected",
            "cancelled",
          ].includes((item as Message).status),
      )
      .slice(-100)
      .map((item) => ({
        ...item,
        status:
          item.status === "pending" && !item.requestId
            ? "unknown"
            : item.status,
      }));
  } catch {
    return [];
  }
}

export function loadDraft(userId?: string): string {
  try {
    return sessionStorage.getItem(draftStorageKey(userId)) ?? "";
  } catch {
    return "";
  }
}
