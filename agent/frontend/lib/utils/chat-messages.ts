import type { ChatHistoryMessage, ToolCall } from "@/lib/types";

export interface ChatUserMessage {
  id: string;
  role: "user";
  content: string;
  createdAt?: string | null;
}

export interface ChatAssistantMessage {
  id: string;
  role: "assistant";
  content: string;
  toolCalls: ToolCall[];
  stoppedEarly?: boolean;
  createdAt?: string | null;
}

export interface ChatErrorMessage {
  id: string;
  role: "error";
  error: string;
  retryMessage: string;
}

export type ChatUiMessage =
  | ChatUserMessage
  | ChatAssistantMessage
  | ChatErrorMessage;

function toolCallId(call: ToolCall, fallback: string): string {
  return call.id ?? fallback;
}

function parseToolContent(content: string): unknown {
  try {
    return JSON.parse(content) as unknown;
  } catch {
    return content;
  }
}

/** Convert GET /history rows into renderable thread messages. */
export function historyToUiMessages(raw: ChatHistoryMessage[]): ChatUiMessage[] {
  const out: ChatUiMessage[] = [];
  const turnTools = new Map<string, ToolCall>();
  const turnToolOrder: string[] = [];

  function resetTurn() {
    turnTools.clear();
    turnToolOrder.length = 0;
  }

  function flushAssistant(content: string, msg: ChatHistoryMessage) {
    const toolCalls = turnToolOrder
      .map((id) => turnTools.get(id))
      .filter((c): c is ToolCall => c != null);
    if (content.trim() || toolCalls.length > 0) {
      out.push({
        id: String(msg.message_id),
        role: "assistant",
        content,
        toolCalls,
        createdAt: msg.created_at,
      });
    }
    resetTurn();
  }

  for (const msg of raw) {
    if (msg.role === "system") continue;

    if (msg.role === "human") {
      resetTurn();
      out.push({
        id: String(msg.message_id),
        role: "user",
        content: msg.content,
        createdAt: msg.created_at,
      });
      continue;
    }

    if (msg.role === "ai") {
      const calls = (msg.tool_calls as ToolCall[] | undefined) ?? [];
      for (const call of calls) {
        const id = toolCallId(call, `${msg.message_id}-${call.name}`);
        turnTools.set(id, { ...call, id });
        if (!turnToolOrder.includes(id)) turnToolOrder.push(id);
      }
      if (msg.content?.trim()) {
        flushAssistant(msg.content, msg);
      }
      continue;
    }

    if (msg.role === "tool" && msg.tool_call_id) {
      const existing = turnTools.get(msg.tool_call_id);
      if (existing) {
        turnTools.set(msg.tool_call_id, {
          ...existing,
          result: parseToolContent(msg.content),
        });
      }
    }
  }

  return out;
}

export function nextMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `msg-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}
