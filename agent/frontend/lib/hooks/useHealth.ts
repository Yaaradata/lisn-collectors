"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchHealthSources } from "@/lib/api/health";
import type { ApiResult } from "@/lib/api/client";
import type { HealthSourcesResponse } from "@/lib/types";

export interface UseHealthState {
  /** True only on the first load — never blocks subsequent polls. */
  initialLoading: boolean;
  /** True while a background poll is in flight. */
  refreshing: boolean;
  result: ApiResult<HealthSourcesResponse> | null;
  lastCheckedAt: Date | null;
  refresh: () => void;
}

/** Poll /health/sources — background refresh must not block the page. */
export function useHealth(pollMs?: number): UseHealthState {
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [result, setResult] = useState<ApiResult<HealthSourcesResponse> | null>(
    null,
  );
  const [lastCheckedAt, setLastCheckedAt] = useState<Date | null>(null);
  const hasLoaded = useRef(false);

  const refresh = useCallback((background = false) => {
    if (background) {
      setRefreshing(true);
    } else if (!hasLoaded.current) {
      setInitialLoading(true);
    }

    void fetchHealthSources().then((res) => {
      setResult(res);
      setLastCheckedAt(new Date());
      hasLoaded.current = true;
      setInitialLoading(false);
      setRefreshing(false);
    });
  }, []);

  useEffect(() => {
    refresh(false);
    if (!pollMs || pollMs <= 0) return;
    const id = setInterval(() => refresh(true), pollMs);
    return () => clearInterval(id);
  }, [pollMs, refresh]);

  return {
    initialLoading,
    refreshing,
    result,
    lastCheckedAt,
    refresh: () => refresh(true),
  };
}
