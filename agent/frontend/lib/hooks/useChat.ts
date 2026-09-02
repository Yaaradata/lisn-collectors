"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  deleteChatSession,
  fetchChatHistory,
  postChat,
} from "@/lib/api/agent";
import { DEFAULT_TIMEOUT_MS } from "@/lib/api/client";
import type { ToolCall } from "@/lib/types";
import {
  historyToUiMessages,
  nextMessageId,
  type ChatUiMessage,
} from "@/lib/utils/chat-messages";
import { classifyApiError } from "@/lib/utils/errors";

const SESSION_STORAGE_KEY = "collector-agent-chat-session-id";
const CHAT_TIMEOUT_MS = Math.max(DEFAULT_TIMEOUT_MS, 120_000);

function loadSessionId(): string {
  if (typeof window === "undefined") return "";
  const stored = localStorage.getItem(SESSION_STORAGE_KEY);
  if (stored?.trim()) return stored.trim();
  const id = nextMessageId();
  localStorage.setItem(SESSION_STORAGE_KEY, id);
  return id;
}

function createSessionId(): string {
  const id = nextMessageId();
  if (typeof window !== "undefined") {
    localStorage.setItem(SESSION_STORAGE_KEY, id);
  }
  return id;
}

export interface UseChatResult {
  sessionId: string;
  messages: ChatUiMessage[];
  historyLoading: boolean;
  sending: boolean;
  elapsedMs: number;
  sendMessage: (text: string) => Promise<void>;
  retry: (text: string) => Promise<void>;
  newConversation: () => Promise<void>;
}

export function useChat(): UseChatResult {
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState<ChatUiMessage[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);
  const sessionRef = useRef("");
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    const id = loadSessionId();
    sessionRef.current = id;
    setSessionId(id);
  }, []);

  useEffect(() => {
    if (!sessionId) return;

    let cancelled = false;
    setHistoryLoading(true);

    void (async () => {
      const result = await fetchChatHistory(sessionId);
      if (cancelled) return;
      setHistoryLoading(false);
      if (result.ok) {
        setMessages(historyToUiMessages(result.data.messages));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  useEffect(() => {
    if (!sending) {
      setElapsedMs(0);
      if (timerRef.current != null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
      return;
    }

    const started = Date.now();
    timerRef.current = window.setInterval(() => {
      setElapsedMs(Date.now() - started);
    }, 250);

    return () => {
      if (timerRef.current != null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [sending]);

  const sendMessage = useCallback(async (text: string) => {
    const message = text.trim();
    if (!message || sending) return;

    const sid = sessionRef.current || loadSessionId();
    sessionRef.current = sid;
    setSessionId(sid);

    const userId = nextMessageId();
    setMessages((prev) => [
      ...prev.filter((m) => m.role !== "error"),
      { id: userId, role: "user", content: message },
    ]);
    setSending(true);

    const result = await postChat(
      { session_id: sid, message },
      { timeoutMs: CHAT_TIMEOUT_MS },
    );

    setSending(false);

    if (!result.ok) {
      const formatted = classifyApiError(result, "chat");
      setMessages((prev) => [
        ...prev,
        {
          id: nextMessageId(),
          role: "error",
          error: `${formatted.title}: ${formatted.message}`,
          retryMessage: message,
        },
      ]);
      return;
    }

    const { reply, tool_calls, stopped_early } = result.data;
    setMessages((prev) => [
      ...prev,
      {
        id: nextMessageId(),
        role: "assistant",
        content: reply?.trim() ? reply : "",
        toolCalls: (tool_calls ?? []) as ToolCall[],
        stoppedEarly: stopped_early,
      },
    ]);
  }, [sending]);

  const retry = useCallback(
    async (text: string) => {
      setMessages((prev) => prev.filter((m) => m.role !== "error"));
      await sendMessage(text);
    },
    [sendMessage],
  );

  const newConversation = useCallback(async () => {
    const oldId = sessionRef.current;
    if (oldId) {
      const del = await deleteChatSession(oldId);
      if (!del.ok && del.status !== 404) {
        setMessages((prev) => [
          ...prev,
          {
            id: nextMessageId(),
            role: "error",
            error: del.error,
            retryMessage: "",
          },
        ]);
        return;
      }
    }

    const newId = createSessionId();
    sessionRef.current = newId;
    setSessionId(newId);
    setMessages([]);
  }, []);

  return {
    sessionId,
    messages,
    historyLoading,
    sending,
    elapsedMs,
    sendMessage,
    retry,
    newConversation,
  };
}
