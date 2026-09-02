import type { BadgeVariant } from "@/components/ui/Badge";
import type { IncidentDiagnosis, IncidentVerdict } from "@/lib/types";
import { formatTimestampUtc } from "@/lib/utils/format";

export interface VerdictPresentation {
  variant: BadgeVariant;
  /** Large badge label */
  label: string;
  /** Plain-language line under the badge */
  subtitle: string;
}

export function verdictPresentation(
  diagnosis: IncidentDiagnosis,
): VerdictPresentation {
  const ts = formatTimestampUtc(
    diagnosis.source_updated_on ?? diagnosis.gap_from ?? diagnosis.collected_at,
  );
  const req = diagnosis.request_id ?? "—";

  switch (diagnosis.verdict) {
    case "COLLECTED":
      return {
        variant: "success",
        label: "COLLECTED",
        subtitle: `Collected on ${formatTimestampUtc(diagnosis.collected_at)} by request ${req}`,
      };
    case "IN_PROGRESS":
      return {
        variant: "info",
        label: "IN_PROGRESS",
        subtitle: "Currently being collected",
      };
    case "AWAITING_ENRICHMENT":
      return {
        variant: "info",
        label: "AWAITING_ENRICHMENT",
        subtitle: "Discovered, waiting for a worker",
      };
    case "NOT_AT_SOURCE":
      return {
        variant: "neutral",
        label: "NOT_AT_SOURCE",
        subtitle: "This incident does not exist at the source",
      };
    case "GAP_NOT_SCHEDULED":
      return {
        variant: "warning",
        label: "GAP_NOT_SCHEDULED",
        subtitle: `Never collected — no discovery window covered ${ts}`,
      };
    case "GAP_TRUNCATED":
      return {
        variant: "warning",
        label: "GAP_TRUNCATED",
        subtitle: `Never collected — the window covering ${ts} stopped at its limit`,
      };
    case "DISCOVERY_FAILED":
      return {
        variant: "error",
        label: "DISCOVERY_FAILED",
        subtitle: diagnosis.summary,
      };
    case "DISCOVERY_RUNNING":
      return {
        variant: "info",
        label: "DISCOVERY_RUNNING",
        subtitle: "Discovery is still running for the covering window",
      };
    case "ENRICHMENT_FAILED":
      return {
        variant: "error",
        label: "ENRICHMENT_FAILED",
        subtitle: diagnosis.last_error
          ? `Enrichment failed: ${diagnosis.last_error}`
          : diagnosis.summary,
      };
    case "ENRICHMENT_DEAD_LETTERED":
      return {
        variant: "error",
        label: "ENRICHMENT_DEAD_LETTERED",
        subtitle: diagnosis.last_error
          ? `Dead-lettered: ${diagnosis.last_error}`
          : diagnosis.summary,
      };
    case "DISCOVERED_NOT_QUEUED":
      return {
        variant: "error",
        label: "DISCOVERED_NOT_QUEUED",
        subtitle: "Discovered in landing table but never queued for enrichment",
      };
    case "ENRICHMENT_DONE_MISSING_WAREHOUSE":
      return {
        variant: "error",
        label: "ENRICHMENT_DONE_MISSING_WAREHOUSE",
        subtitle: "Enrichment marked done but incident is not in the warehouse",
      };
    case "UNEXPLAINED":
      return {
        variant: "error",
        label: "UNEXPLAINED",
        subtitle:
          "It should have been collected. This needs investigation.",
      };
    default: {
      const _exhaustive: never = diagnosis.verdict;
      return {
        variant: "error",
        label: String(_exhaustive),
        subtitle: diagnosis.summary,
      };
    }
  }
}

/** Canonical incident chain — steps not present were skipped by short-circuit. */
export const INCIDENT_CHAIN_STEP_NAMES = [
  "warehouse_incidents_current",
  "discovered_ids",
  "source_sentinel_incident",
  "discovery_window_covering_updated_on",
  "gap_containing_updated_on",
  "collector_job_for_incident",
] as const;

export function humanStepName(name: string): string {
  const labels: Record<string, string> = {
    warehouse_incidents_current: "Warehouse (incidents_current)",
    discovered_ids: "Discovery landing (discovered_ids)",
    source_sentinel_incident: "Source (sentinel_incident)",
    discovery_window_covering_updated_on: "Discovery window covering updated_on",
    gap_containing_updated_on: "Boundary gap containing updated_on",
    collector_job_for_incident: "Enrichment job (collector_job)",
  };
  return labels[name] ?? name;
}

export function stepOutcomeSummary(step: {
  row_count: number;
  note?: string | null;
}): string {
  if (step.row_count === 0) {
    return "No rows returned";
  }
  return `${step.row_count} row${step.row_count === 1 ? "" : "s"}`;
}
