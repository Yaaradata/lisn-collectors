"use client";

import { useState } from "react";

import { CodeBlock } from "@/components/ui/CodeBlock";
import type { DiagnosisStep } from "@/lib/types";
import { cn } from "@/lib/utils/cn";
import {
  humanStepName,
  INCIDENT_CHAIN_STEP_NAMES,
  stepOutcomeSummary,
} from "@/lib/utils/verdict";

export interface DiagnosisStepsProps {
  steps: DiagnosisStep[];
  /** Step name that produced the final verdict (typically the last executed step). */
  verdictStepName?: string;
  /** When true, show canonical chain slots for steps the backend skipped. */
  showSkipped?: boolean;
}

type RowState = "executed" | "verdict" | "skipped";

interface StepRow {
  name: string;
  state: RowState;
  step?: DiagnosisStep;
}

function buildRows(
  steps: DiagnosisStep[],
  verdictStepName: string | undefined,
  showSkipped: boolean,
): StepRow[] {
  const byName = new Map(steps.map((s) => [s.name, s]));
  const verdictName =
    verdictStepName ?? steps[steps.length - 1]?.name ?? undefined;

  if (!showSkipped) {
    return steps.map((step) => ({
      name: step.name,
      state: step.name === verdictName ? "verdict" : "executed",
      step,
    }));
  }

  const rows: StepRow[] = [];
  for (const name of INCIDENT_CHAIN_STEP_NAMES) {
    const step = byName.get(name);
    if (step) {
      rows.push({
        name,
        state: name === verdictName ? "verdict" : "executed",
        step,
      });
    } else {
      rows.push({ name, state: "skipped" });
    }
  }
  return rows;
}

function StepQuery({ query, defaultOpen }: { query: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen ?? false);
  if (!query.trim()) return null;
  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="font-mono-data text-xs text-accent hover:underline"
      >
        {open ? "Hide query" : "Show query"}
      </button>
      {open ? (
        <div className="mt-2">
          <CodeBlock code={query} />
        </div>
      ) : null}
    </div>
  );
}

export function DiagnosisSteps({
  steps,
  verdictStepName,
  showSkipped = true,
}: DiagnosisStepsProps) {
  const rows = buildRows(steps, verdictStepName, showSkipped);

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold text-foreground">
        Diagnostic chain
      </h3>
      {/* This section is what makes the answer trustworthy. Do not hide it
          behind a top-level "details" toggle — collapsed queries only. */}
      <ol className="space-y-2">
        {rows.map((row, idx) => {
          const skipped = row.state === "skipped";
          const isVerdict = row.state === "verdict";
          const step = row.step;

          return (
            <li
              key={`${row.name}-${idx}`}
              className={cn(
                "rounded-lg border border-border px-4 py-3",
                skipped && "border-dashed bg-surface/50 opacity-60",
                isVerdict && "border-accent/40 bg-accent/5",
              )}
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <p
                    className={cn(
                      "font-mono-data text-sm font-medium",
                      skipped ? "text-muted" : "text-foreground",
                    )}
                  >
                    {step?.step ?? "—"}. {humanStepName(row.name)}
                  </p>
                  {step ? (
                    <p className="mt-1 text-xs text-muted">
                      {step.system} · {stepOutcomeSummary(step)}
                    </p>
                  ) : (
                    <p className="mt-1 text-xs italic text-muted">
                      Skipped — chain short-circuited before this check
                    </p>
                  )}
                </div>
                {isVerdict ? (
                  <span className="shrink-0 rounded border border-accent/40 bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
                    verdict
                  </span>
                ) : null}
              </div>
              {step?.note ? (
                <p className="mt-2 text-xs leading-relaxed text-muted">
                  {step.note}
                </p>
              ) : null}
              {step && step.row_count > 0 && step.result != null ? (
                <pre className="font-mono-data mt-2 max-h-40 overflow-auto rounded border border-border bg-background p-2 text-xs text-foreground/80">
                  {JSON.stringify(step.result, null, 2)}
                </pre>
              ) : null}
              {step ? <StepQuery query={step.query} /> : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
