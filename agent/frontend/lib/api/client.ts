import type { ApiErrorBody } from "@/lib/types";

const DEFAULT_BASE_URL = "/api/agent";
const DIRECT_BACKEND_URL = "http://localhost:8090";
const DEFAULT_TIMEOUT_MS = 60_000;

export type ApiErrorKind =
  | "timeout"
  | "unreachable"
  | "not_found_route"
  | "http"
  | "parse";

export type ApiResult<T> =
  | { ok: true; data: T }
  | {
      ok: false;
      error: string;
      status?: number;
      kind?: ApiErrorKind;
      timeoutMs?: number;
    };

export interface ApiClientOptions {
  baseUrl?: string;
  timeoutMs?: number;
  /** Optional Bearer token for Cloud Run (local proxy usually does not need this). */
  getAuthToken?: () => string | null | undefined;
}

function resolveBaseUrl(): string {
  const fromEnv = process.env.NEXT_PUBLIC_AGENT_API_URL?.replace(/\/$/, "");
  if (fromEnv) return fromEnv;
  // Browser: same-origin server proxy (/api/agent → backend with SA token).
  if (typeof window !== "undefined") return DEFAULT_BASE_URL;
  return DIRECT_BACKEND_URL;
}

function parseErrorBody(text: string, status: number): string {
  try {
    const body = JSON.parse(text) as ApiErrorBody;
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((d) => d.msg ?? JSON.stringify(d)).join("; ");
    }
  } catch {
    // not JSON
  }
  if (text.trim()) return text.trim();
  return `HTTP ${status}`;
}

/**
 * Typed fetch wrapper. Errors are returned, not thrown — a failed diagnostic
 * must render as a visible error message, never as a silent empty state that
 * looks like "not found".
 */
export async function apiFetch<T>(
  path: string,
  init?: RequestInit & { params?: Record<string, string> },
  options?: ApiClientOptions,
): Promise<ApiResult<T>> {
  const baseUrl = options?.baseUrl ?? resolveBaseUrl();
  const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  let url = `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
  if (init?.params) {
    const qs = new URLSearchParams(init.params);
    url += `?${qs.toString()}`;
  }

  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  const token = options?.getAuthToken?.();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const { params: _params, ...fetchInit } = init ?? {};
    const res = await fetch(url, {
      ...fetchInit,
      headers,
      signal: controller.signal,
    });

    const text = await res.text();
    if (!res.ok) {
      const kind =
        res.status === 404
          ? "not_found_route"
          : ("http" as const);
      return {
        ok: false,
        error: parseErrorBody(text, res.status),
        status: res.status,
        kind,
      };
    }

    if (!text.trim()) {
      return { ok: false, error: "Empty response body", status: res.status };
    }

    try {
      return { ok: true, data: JSON.parse(text) as T };
    } catch {
      return {
        ok: false,
        error: "Response was not valid JSON",
        status: res.status,
      };
    }
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      return {
        ok: false,
        error: `Request timed out after ${timeoutMs / 1000}s`,
        kind: "timeout",
        timeoutMs,
      };
    }
    const message = err instanceof Error ? err.message : String(err);
    const unreachable =
      /failed to fetch|networkerror|network request failed|load failed/i.test(
        message,
      );
    return {
      ok: false,
      error: message,
      kind: unreachable ? "unreachable" : "http",
    };
  } finally {
    clearTimeout(timer);
  }
}

export { DEFAULT_BASE_URL, DEFAULT_TIMEOUT_MS, resolveBaseUrl };
