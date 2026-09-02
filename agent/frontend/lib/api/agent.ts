import { apiFetch, type ApiClientOptions, type ApiResult } from "@/lib/api/client";
import type {
  ChatDeleteResponse,
  ChatHistoryResponse,
  ChatRequest,
  ChatResponse,
} from "@/lib/types";

/** POST /v1/chat */
export function postChat(
  body: ChatRequest,
  options?: ApiClientOptions,
): Promise<ApiResult<ChatResponse>> {
  return apiFetch<ChatResponse>(
    "/v1/chat",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    options,
  );
}

/** GET /v1/chat/{session_id}/history */
export function fetchChatHistory(
  sessionId: string,
  options?: ApiClientOptions,
): Promise<ApiResult<ChatHistoryResponse>> {
  const id = encodeURIComponent(sessionId.trim());
  return apiFetch<ChatHistoryResponse>(
    `/v1/chat/${id}/history`,
    { method: "GET" },
    options,
  );
}

/** DELETE /v1/chat/{session_id} */
export function deleteChatSession(
  sessionId: string,
  options?: ApiClientOptions,
): Promise<ApiResult<ChatDeleteResponse>> {
  const id = encodeURIComponent(sessionId.trim());
  return apiFetch<ChatDeleteResponse>(
    `/v1/chat/${id}`,
    { method: "DELETE" },
    options,
  );
}
