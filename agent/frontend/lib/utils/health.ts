import type { BadgeVariant } from "@/components/ui/Badge";
import type { HealthSource, HealthSourcesResponse } from "@/lib/types";

/** Backend HealthResult.name → operator-facing label. */
export const SOURCE_LABELS: Record<string, string> = {
  sql: "Cloud SQL",
  bigquery: "BigQuery",
  signoz: "SigNoz",
  gcp: "Cloud Run",
};

/** Status bar shows these four integrations only (not agent_sessions). */
export const STATUS_BAR_SOURCES = ["sql", "bigquery", "signoz", "gcp"] as const;

export type StatusBarSourceId = (typeof STATUS_BAR_SOURCES)[number];

export interface SourcePillState {
  id: StatusBarSourceId;
  label: string;
  variant: BadgeVariant;
  statusLabel: string;
  message: string;
}

/**
 * SigNoz may legitimately be unconfigured — grey "not configured", never red.
 * Red is reserved for sources that should work but returned error.
 */
export function sourcePillState(
  id: StatusBarSourceId,
  source: HealthSource | undefined,
): SourcePillState {
  const label = SOURCE_LABELS[id] ?? source?.name ?? id;

  if (!source) {
    return {
      id,
      label,
      variant: "error",
      statusLabel: "missing",
      message: "No health data returned for this source",
    };
  }

  if (source.status === "ok") {
    return {
      id,
      label,
      variant: "success",
      statusLabel: "ok",
      message: source.message,
    };
  }

  if (source.status === "unavailable") {
    const statusLabel =
      id === "signoz" ? "not configured" : "unavailable";
    return {
      id,
      label,
      variant: "neutral",
      statusLabel,
      message: source.message,
    };
  }

  return {
    id,
    label,
    variant: "error",
    statusLabel: "error",
    message: source.message,
  };
}

export function buildStatusBarPills(
  data: HealthSourcesResponse | undefined,
): SourcePillState[] {
  const byName = new Map(
    (data?.sources ?? []).map((s) => [s.name, s] as const),
  );
  return STATUS_BAR_SOURCES.map((id) =>
    sourcePillState(id, byName.get(id)),
  );
}

export function overallHealthLabel(
  status: HealthSourcesResponse["status"] | undefined,
): string {
  switch (status) {
    case "ok":
      return "All required sources healthy";
    case "degraded":
      return "Degraded — one or more optional sources unavailable";
    case "error":
      return "Error — a required source is down";
    default:
      return "Health unknown";
  }
}
