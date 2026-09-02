/**
 * Shared API types — mirrors agent/backend contracts.
 */

export type HealthStatus = "ok" | "error" | "unavailable";

export type SourcesOverallStatus = "ok" | "degraded" | "error";

/** GET /health */
export interface HealthResponse {
  status: "ok";
  service: string;
}

/** One row from GET /health/sources (HealthResult). */
export interface HealthSource {
  name: string;
  status: HealthStatus;
  message: string;
}

/** GET /health/sources */
export interface HealthSourcesResponse {
  status: SourcesOverallStatus;
  sources: HealthSource[];
  chat_ready: boolean;
  model_provider: string;
}

/** FastAPI HTTP error body. */
export interface ApiErrorBody {
  detail?: string | { msg?: string; type?: string }[];
}

export type {
  ChatDeleteResponse,
  ChatHistoryMessage,
  ChatHistoryResponse,
  ChatMessageRole,
  ChatRequest,
  ChatResponse,
  ToolCall,
} from "./chat";

export type {
  DiagnosisStep,
  DiagnosisSystem,
  GapCause,
  GapExplanation,
  IncidentDiagnosis,
  IncidentVerdict,
  RangeDiagnosis,
} from "./diagnostics";
