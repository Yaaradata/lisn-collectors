"use client";

import Link from "next/link";

import { ApiErrorPanel } from "@/components/ui/ApiErrorPanel";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { useHealth } from "@/lib/hooks/useHealth";
import {
  overallHealthLabel,
  SOURCE_LABELS,
  sourcePillState,
  type StatusBarSourceId,
} from "@/lib/utils/health";

export function HealthSummary() {
  const { initialLoading, result, refresh } = useHealth(30_000);

  if (initialLoading && !result) {
    return (
      <Card title="Source health">
        <div className="space-y-3" aria-busy="true">
          <Skeleton className="h-4 w-48" />
          {[1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      </Card>
    );
  }

  if (!result?.ok) {
    if (!result) {
      return (
        <Card title="Source health">
          <p className="text-sm text-error">Could not load source health.</p>
        </Card>
      );
    }
    return (
      <Card title="Source health">
        <ApiErrorPanel result={result} onRetry={refresh} />
      </Card>
    );
  }

  const { data } = result;

  return (
    <Card
      title="Source health"
      action={
        data.chat_ready ? (
          <Badge variant="success">Chat ready</Badge>
        ) : (
          <Badge variant="warning">Chat unavailable</Badge>
        )
      }
    >
      <p className="mb-4 text-sm text-muted">{overallHealthLabel(data.status)}</p>
      <ul className="space-y-3">
        {data.sources.map((source) => {
          const pill = sourcePillState(
            source.name as StatusBarSourceId,
            source,
          );
          const label = SOURCE_LABELS[source.name] ?? source.name;
          return (
            <li
              key={source.name}
              className="flex flex-col gap-1 border-b border-border pb-3 last:border-0 last:pb-0 md:flex-row md:items-start md:justify-between"
            >
              <div className="flex items-center gap-2">
                <Badge variant={pill.variant}>{pill.statusLabel}</Badge>
                <span className="text-sm font-medium">{label}</span>
              </div>
              <p className="font-mono-data text-xs leading-relaxed text-muted md:max-w-md md:text-right">
                {source.message}
              </p>
            </li>
          );
        })}
      </ul>
      <p className="mt-4 font-mono-data text-xs text-muted">
        model_provider={data.model_provider}
      </p>
    </Card>
  );
}

export function EntryPoints() {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card title="Diagnose an incident">
        <p className="mb-4 text-sm leading-relaxed text-muted">
          Enter an incident id and get a deterministic verdict with the full
          query chain — warehouse, source, windows, jobs.
        </p>
        <Link
          href="/diagnose"
          className="inline-flex items-center justify-center rounded-md bg-accent px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-accent-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Open incident diagnosis
        </Link>
      </Card>

      <Card title="Ask a question">
        <p className="mb-4 text-sm leading-relaxed text-muted">
          Natural-language chat when the question spans systems or you need
          narration over tool results.
        </p>
        <Link
          href="/chat"
          className="inline-flex items-center justify-center rounded-md border border-border bg-surface-raised px-4 py-2 text-sm font-medium text-foreground transition-colors hover:border-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Open chat
        </Link>
      </Card>
    </div>
  );
}
