/**
 * Mirrors agent/backend/app/diagnostics.py — backend is source of truth.
 */

/** Exact IncidentVerdict union from diagnostics.py (verified 2026-09-02). */
export type IncidentVerdict =
  | "COLLECTED"
  | "NOT_AT_SOURCE"
  | "GAP_NOT_SCHEDULED"
  | "GAP_TRUNCATED"
  | "DISCOVERY_FAILED"
  | "DISCOVERY_RUNNING"
  | "UNEXPLAINED"
  | "ENRICHMENT_DEAD_LETTERED"
  | "ENRICHMENT_FAILED"
  | "AWAITING_ENRICHMENT"
  | "IN_PROGRESS"
  | "DISCOVERED_NOT_QUEUED"
  | "ENRICHMENT_DONE_MISSING_WAREHOUSE";

/** Not in backend: DISCOVERED_NOT_ENRICHED — use DISCOVERED_NOT_QUEUED / pipeline verdicts. */

export type DiagnosisSystem =
  | "bigquery"
  | "cloud_sql_collector"
  | "cloud_sql_source"
  | "cloud_run";

/** One explicit check in a diagnostic chain — empty result ≠ failure. */
export interface DiagnosisStep {
  step: number;
  name: string;
  system: DiagnosisSystem;
  query: string;
  params: Record<string, unknown>;
  row_count: number;
  result: unknown;
  note?: string | null;
}

export interface IncidentDiagnosis {
  incident_id: string;
  verdict: IncidentVerdict;
  summary: string;
  steps: DiagnosisStep[];
  collected_at?: string | null;
  request_id?: string | null;
  thread_rows?: number | null;
  source_updated_on?: string | null;
  last_error?: string | null;
  gap_from?: string | null;
  gap_to?: string | null;
  covering_window_id?: string | null;
  covering_window_id_count?: number | null;
}

export interface RangeDiagnosis {
  range_from: string;
  range_to: string;
  source_count: number;
  warehouse_count: number;
  discovered_count: number;
  missing: number;
  windows: Record<string, unknown>[];
  gaps: Record<string, unknown>[];
  partial_windows: Record<string, unknown>[];
  failed_pages: Record<string, unknown>[];
  steps: DiagnosisStep[];
}

export type GapCause =
  | "never_scheduled"
  | "truncated"
  | "failed_discovery"
  | "no_workers"
  | "mixed"
  | "unknown";

export interface GapExplanation {
  gap_from: string;
  gap_to: string;
  cause: GapCause;
  summary: string;
  windows: Record<string, unknown>[];
  gaps: Record<string, unknown>[];
  failed_windows: Record<string, unknown>[];
  worker_heartbeats: Record<string, unknown>[];
  cloud_run_executions: Record<string, unknown>[];
  steps: DiagnosisStep[];
}
