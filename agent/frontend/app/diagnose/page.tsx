"use client";

import { FormEvent, useState } from "react";

import {
  DiagnosisSteps,
  IncidentDiagnosisSkeleton,
  VerdictBadge,
} from "@/components/diagnostics";
import { ApiErrorPanel } from "@/components/ui/ApiErrorPanel";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { TruncatedId } from "@/components/ui/TruncatedId";
import { diagnoseIncident } from "@/lib/api/diagnostics";
import {
  formatElapsedLabel,
  showElapsedCounter,
  useElapsed,
} from "@/lib/hooks/useElapsed";
import type { ApiResult } from "@/lib/api/client";
import type { IncidentDiagnosis } from "@/lib/types";
import { validateIncidentIdShape } from "@/lib/utils/incident-id";

export default function DiagnoseIncidentPage() {
  const [incidentId, setIncidentId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Extract<
    ApiResult<IncidentDiagnosis>,
    { ok: false }
  > | null>(null);
  const [diagnosis, setDiagnosis] = useState<IncidentDiagnosis | null>(null);
  const [lastSubmittedId, setLastSubmittedId] = useState("");
  const elapsedMs = useElapsed(loading);

  const shape = incidentId.trim()
    ? validateIncidentIdShape(incidentId)
    : { ok: true, warning: null };

  async function runDiagnosis(id: string) {
    setLoading(true);
    setError(null);
    setDiagnosis(null);
    setLastSubmittedId(id);

    const result = await diagnoseIncident(id);
    setLoading(false);

    if (!result.ok) {
      setError(result);
      return;
    }
    setDiagnosis(result.data);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const id = incidentId.trim();
    if (!id) return;
    await runDiagnosis(id);
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="text-lg font-semibold text-foreground">
          Diagnose an incident
        </h2>
        <p className="mt-1 max-w-2xl text-sm leading-relaxed text-muted">
          Enter a Sentinel incident id for a deterministic verdict and the full
          query chain. Direct path — id in, verdict out.
        </p>
      </div>

      <div className="xl:grid xl:grid-cols-[minmax(280px,360px)_1fr] xl:items-start xl:gap-6">
        <Card className="xl:sticky xl:top-4">
          <form onSubmit={onSubmit} className="flex flex-col gap-4">
            <Input
              label="Incident id"
              placeholder="IN26081800000000027963"
              value={incidentId}
              onChange={(e) => setIncidentId(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              className="text-base"
            />
            <p className="text-xs leading-relaxed text-muted">
              Format:{" "}
              <code className="font-mono-data">IN</code> + date digits + sequence
              (e.g.{" "}
              <code className="font-mono-data">IN26081800000000027963</code>).
            </p>
            {shape.warning ? (
              <p
                role="status"
                className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning"
              >
                {shape.warning}
              </p>
            ) : null}
            <Button type="submit" loading={loading} disabled={!incidentId.trim()}>
              Diagnose
            </Button>
            {loading && showElapsedCounter(elapsedMs) ? (
              <p className="font-mono-data text-xs text-muted" aria-live="polite">
                Running diagnostic chain… {formatElapsedLabel(elapsedMs)}
              </p>
            ) : null}
          </form>
        </Card>

        <div className="flex min-w-0 flex-col gap-6">
          {loading ? <IncidentDiagnosisSkeleton /> : null}

          {error ? (
            <ApiErrorPanel
              result={error}
              context="incident"
              onRetry={() => void runDiagnosis(lastSubmittedId || incidentId.trim())}
            />
          ) : null}

          {diagnosis ? (
            <>
              <Card title="Verdict">
                <p className="mb-3 text-sm text-muted">
                  Incident{" "}
                  <TruncatedId value={diagnosis.incident_id} />
                </p>
                <VerdictBadge diagnosis={diagnosis} />
              </Card>
              <Card>
                <DiagnosisSteps
                  steps={diagnosis.steps}
                  verdictStepName={
                    diagnosis.steps[diagnosis.steps.length - 1]?.name
                  }
                />
              </Card>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
