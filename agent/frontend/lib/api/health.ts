import { apiFetch, type ApiClientOptions, type ApiResult } from "@/lib/api/client";
import type { HealthResponse, HealthSourcesResponse } from "@/lib/types";

export function fetchHealth(
  options?: ApiClientOptions,
): Promise<ApiResult<HealthResponse>> {
  return apiFetch<HealthResponse>("/health", { method: "GET" }, options);
}

export function fetchHealthSources(
  options?: ApiClientOptions,
): Promise<ApiResult<HealthSourcesResponse>> {
  return apiFetch<HealthSourcesResponse>(
    "/health/sources",
    { method: "GET" },
    options,
  );
}
