"use client";

import { useHealth } from "@/lib/hooks/useHealth";
import { SOURCE_LABELS } from "@/lib/utils/health";

/**
 * Shown when the backend is up but a required source is unhealthy — answers
 * may be incomplete.
 */
export function SourceHealthBanner() {
  const { result } = useHealth(30_000);

  if (!result?.ok) return null;

  const unhealthy = result.data.sources.filter(
    (s) =>
      s.status === "error" ||
      (s.status !== "ok" && s.status !== "unavailable" && s.name !== "signoz"),
  );

  if (unhealthy.length === 0 && result.data.status !== "error") {
    return null;
  }

  const names = unhealthy
    .map((s) => SOURCE_LABELS[s.name] ?? s.name)
    .join(", ");

  return (
    <div
      role="status"
      className="mb-6 rounded-lg border border-warning/50 bg-warning/10 px-4 py-3 text-sm leading-relaxed text-foreground"
    >
      <p className="font-medium text-warning">
        {unhealthy.length > 0
          ? `Source unhealthy: ${names}`
          : "Collector health degraded"}
      </p>
      <p className="mt-1 text-muted">
        The agent backend is reachable, but one or more data sources returned an
        error. Diagnosis and chat answers may be incomplete until the source
        recovers.
      </p>
    </div>
  );
}
