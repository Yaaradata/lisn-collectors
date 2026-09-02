import type { ApiResult } from "@/lib/api/client";
import { DEFAULT_TIMEOUT_MS } from "@/lib/api/client";

export type ApiErrorContext = "incident" | "range" | "chat" | "generic";

export type ApiErrorKind =
  | "timeout"
  | "unreachable"
  | "not_found_route"
  | "http"
  | "parse";

export interface FormattedApiError {
  kind: ApiErrorKind;
  title: string;
  message: string;
  hint?: string;
  showRetry: boolean;
}

const LOCALHOST_PROXY_HINT =
  "Start the backend locally on port 8090, or reach the deployed service via:\n" +
  "gcloud run services proxy collector-agent --port 8090";

export function getApiBaseUrlForDisplay(): string {
  if (typeof window !== "undefined") {
    const fromEnv = process.env.NEXT_PUBLIC_AGENT_API_URL?.replace(/\/$/, "");
    if (fromEnv) return fromEnv;
    return `${window.location.origin}/api/agent`;
  }
  return (
    process.env.NEXT_PUBLIC_AGENT_API_URL?.replace(/\/$/, "") ??
    "http://localhost:8090"
  );
}

export function isLocalhostApiUrl(url: string): boolean {
  return /localhost|127\.0\.0\.1/.test(url);
}

export function classifyApiError(
  result: Extract<ApiResult<unknown>, { ok: false }>,
  context: ApiErrorContext = "generic",
): FormattedApiError {
  const url = getApiBaseUrlForDisplay();
  const err = result.error ?? "Unknown error";
  const status = result.status;
  const kind = result.kind ?? inferErrorKind(err, status);

  if (kind === "timeout") {
    const seconds = Math.round(
      (result.timeoutMs ?? DEFAULT_TIMEOUT_MS) / 1000,
    );
    return {
      kind,
      title: "Request timed out",
      message: `This took longer than ${seconds} seconds.`,
      showRetry: true,
    };
  }

  if (kind === "unreachable") {
    return {
      kind,
      title: "Backend unreachable",
      message: `Cannot reach the agent backend at ${url}.`,
      hint: isLocalhostApiUrl(url) ? LOCALHOST_PROXY_HINT : undefined,
      showRetry: true,
    };
  }

  if (kind === "not_found_route" || status === 404) {
    if (context === "incident") {
      return {
        kind: "not_found_route",
        title: "Backend route not found",
        message:
          "The agent backend returned HTTP 404 for the diagnose endpoint — the API route is missing or the proxy URL is wrong.",
        hint:
          "This is not the same as an incident id that does not exist at the source. Unknown ids return a NOT_AT_SOURCE verdict with HTTP 200.",
        showRetry: true,
      };
    }
    return {
      kind: "not_found_route",
      title: "Not found",
      message: err || "The requested resource was not found (HTTP 404).",
      showRetry: false,
    };
  }

  return {
    kind: kind === "parse" ? "parse" : "http",
    title: "Request failed",
    message: err,
    showRetry: true,
  };
}

function inferErrorKind(
  error: string,
  status?: number,
): ApiErrorKind {
  const lower = error.toLowerCase();
  if (lower.includes("timed out") || lower.includes("timeout")) {
    return "timeout";
  }
  if (
    lower.includes("failed to fetch") ||
    lower.includes("networkerror") ||
    lower.includes("network request failed") ||
    lower.includes("load failed")
  ) {
    return "unreachable";
  }
  if (status === 404) return "not_found_route";
  if (lower.includes("not valid json") || lower.includes("empty response")) {
    return "parse";
  }
  return "http";
}
