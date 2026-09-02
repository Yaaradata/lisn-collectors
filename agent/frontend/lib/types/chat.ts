/**
 * Mirrors agent/backend/app/main.py ChatRequest/ChatResponse and
 * app/graph/session.py history() rows.
 */

/** POST /v1/chat body (ChatRequest). */
export interface ChatRequest {
  session_id: string;
  message: string;
}

/** Tool invocation returned with a chat turn (from graph _extract_reply_and_tools). */
export interface ToolCall {
  id?: string | null;
  name: string;
  args?: Record<string, unknown>;
  /** Present when the tool ran; absent if the round cap blocked execution. */
  result?: unknown;
}

/** POST /v1/chat response (ChatResponse). */
export interface ChatResponse {
  session_id: string;
  reply: string;
  tool_calls: ToolCall[];
  stopped_early: boolean;
  model_provider: string;
}

export type ChatMessageRole = "human" | "ai" | "tool" | "system";

/** GET /v1/chat/{session_id}/history message row. */
export interface ChatHistoryMessage {
  message_id: number;
  role: ChatMessageRole;
  content: string;
  created_at: string | null;
  tool_calls?: unknown[];
  tool_call_id?: string;
  name?: string;
}

export interface ChatHistoryResponse {
  session_id: string;
  messages: ChatHistoryMessage[];
}

export interface ChatDeleteResponse {
  session_id: string;
  deleted: true;
}
