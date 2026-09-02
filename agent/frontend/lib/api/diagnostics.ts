import { apiFetch, type ApiClientOptions, type ApiResult } from "@/lib/api/client";
import type {
  GapExplanation,
  IncidentDiagnosis,
  RangeDiagnosis,
} from "@/lib/types";

function toIsoParam(value: string | Date): string {
  return value instanceof Date ? value.toISOString() : value;
}

/** GET /v1/diagnose/incident/{incident_id} */
export function diagnoseIncident(
  incidentId: string,
  options?: ApiClientOptions,
): Promise<ApiResult<IncidentDiagnosis>> {
  const id = encodeURIComponent(incidentId.trim());
  return apiFetch<IncidentDiagnosis>(
    `/v1/diagnose/incident/${id}`,
    { method: "GET" },
    options,
  );
}

/** GET /v1/diagnose/range?from=&to= */
export function diagnoseRange(
  from: string | Date,
  to: string | Date,
  options?: ApiClientOptions,
): Promise<ApiResult<RangeDiagnosis>> {
  return apiFetch<RangeDiagnosis>(
    "/v1/diagnose/range",
    {
      method: "GET",
      params: { from: toIsoParam(from), to: toIsoParam(to) },
    },
    options,
  );
}

/** GET /v1/diagnose/gap?from=&to= */
export function explainGap(
  from: string | Date,
  to: string | Date,
  options?: ApiClientOptions,
): Promise<ApiResult<GapExplanation>> {
  return apiFetch<GapExplanation>(
    "/v1/diagnose/gap",
    {
      method: "GET",
      params: { from: toIsoParam(from), to: toIsoParam(to) },
    },
    options,
  );
}
