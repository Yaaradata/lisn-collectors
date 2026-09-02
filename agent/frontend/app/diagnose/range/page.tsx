"use client";

import { FormEvent, useMemo, useRef, useState } from "react";

import {
  GapList,
  RangeDiagnosisSkeleton,
  StatTile,
  WindowTable,
  type DiscoveryWindowRow,
  type GapRow,
} from "@/components/diagnostics";
import { ApiErrorPanel } from "@/components/ui/ApiErrorPanel";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Timestamp } from "@/components/ui/Timestamp";
import { diagnoseRange } from "@/lib/api/diagnostics";
import type { ApiResult } from "@/lib/api/client";
import type { RangeDiagnosis } from "@/lib/types";
import {
  formatElapsedLabel,
  showElapsedCounter,
  useElapsed,
} from "@/lib/hooks/useElapsed";
import { formatInteger } from "@/lib/utils/format";

function defaultRange(): { from: string; to: string } {
  const to = new Date();
  const from = new Date(to.getTime() - 24 * 60 * 60 * 1000);
  return {
    from: toLocalInputValue(from),
    to: toLocalInputValue(to),
  };
}

function toLocalInputValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function toIsoFromLocalInput(value: string): string {
  return new Date(value).toISOString();
}

export default function DiagnoseRangePage() {
  const defaults = useMemo(() => defaultRange(), []);
  const [fromLocal, setFromLocal] = useState(defaults.from);
  const [toLocal, setToLocal] = useState(defaults.to);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Extract<
    ApiResult<RangeDiagnosis>,
    { ok: false }
  > | null>(null);
  const [data, setData] = useState<RangeDiagnosis | null>(null);
  const [lastRange, setLastRange] = useState<{ from: string; to: string } | null>(
    null,
  );
  const gapsRef = useRef<HTMLElement>(null);
  const elapsedMs = useElapsed(loading);

  async function runDiagnosis(from: string, to: string) {
    setLoading(true);
    setError(null);
    setData(null);
    setLastRange({ from, to });

    const result = await diagnoseRange(from, to);
    setLoading(false);

    if (!result.ok) {
      setError(result);
      return;
    }
    setData(result.data);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    await runDiagnosis(
      toIsoFromLocalInput(fromLocal),
      toIsoFromLocalInput(toLocal),
    );
  }

  const windows = (data?.windows ?? []) as DiscoveryWindowRow[];
  const gaps = (data?.gaps ?? []) as GapRow[];
  const emptySource = data != null && data.source_count === 0;
  const emptyWarehouse =
    data != null && data.warehouse_count === 0 && data.source_count > 0;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="text-lg font-semibold text-foreground">
          Time range diagnosis
        </h2>
        <p className="mt-1 max-w-2xl text-sm leading-relaxed text-muted">
          Compare source, warehouse, and discovery counts for an{" "}
          <code className="font-mono-data">updated_on</code> window. Distinct
          incident ids, not thread-exploded row counts.
        </p>
      </div>

      <Card>
        <form onSubmit={onSubmit} className="grid gap-4 md:grid-cols-2">
          <Input
            label="From"
            type="datetime-local"
            value={fromLocal}
            onChange={(e) => setFromLocal(e.target.value)}
          />
          <Input
            label="To"
            type="datetime-local"
            value={toLocal}
            onChange={(e) => setToLocal(e.target.value)}
          />
          <div className="md:col-span-2 flex flex-wrap items-center gap-3">
            <Button type="submit" loading={loading}>
              Diagnose range
            </Button>
            {loading && showElapsedCounter(elapsedMs) ? (
              <p className="font-mono-data text-xs text-muted" aria-live="polite">
                Querying source, warehouse, and windows…{" "}
                {formatElapsedLabel(elapsedMs)}
              </p>
            ) : null}
          </div>
        </form>
      </Card>

      {loading ? <RangeDiagnosisSkeleton /> : null}

      {error ? (
        <ApiErrorPanel
          result={error}
          context="range"
          onRetry={() => {
            if (lastRange) void runDiagnosis(lastRange.from, lastRange.to);
          }}
        />
      ) : null}

      {data ? (
        <>
          {emptySource ? (
            <Card title="No data in range">
              <p className="text-sm text-foreground">
                No incidents match this range — the source returned zero rows
                with <code className="font-mono-data">updated_on</code> in this
                window.
              </p>
            </Card>
          ) : null}

          {emptyWarehouse ? (
            <div
              role="note"
              className="rounded-lg border border-info/40 bg-info/10 px-4 py-3 text-sm leading-relaxed text-foreground"
            >
              <p className="font-medium text-info">Warehouse appears empty</p>
              <p className="mt-1 text-muted">
                The warehouse has zero incidents for this range while the source
                has {formatInteger(data.source_count)}. After a warehouse reset,
                everything can report as not collected even when the source still
                has the data — check whether incidents predate the reset before
                concluding data was lost.
              </p>
            </div>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile label="Source count" value={data.source_count} />
            <StatTile label="Warehouse count" value={data.warehouse_count} />
            <StatTile label="Discovered count" value={data.discovered_count} />
            <StatTile
              label="Missing"
              value={data.missing}
              highlight={data.missing > 0 ? "warning" : "none"}
              onClick={
                data.missing > 0
                  ? () =>
                      gapsRef.current?.scrollIntoView({
                        behavior: "smooth",
                        block: "start",
                      })
                  : undefined
              }
              footer={
                data.missing > 0 ? "Click to see gaps below" : undefined
              }
            />
          </div>

          <div className="grid gap-6 xl:grid-cols-2">
            <Card title="Summary">
              <p className="font-mono-data text-sm text-muted">
                <Timestamp value={data.range_from} />
                {" → "}
                <Timestamp value={data.range_to} />
              </p>
              <p className="mt-2 text-sm text-foreground">
                {formatInteger(data.source_count)} at source,{" "}
                {formatInteger(data.warehouse_count)} in warehouse,{" "}
                <span
                  className={
                    data.missing > 0 ? "font-medium text-warning" : undefined
                  }
                >
                  {formatInteger(data.missing)} missing
                </span>
                .
              </p>
            </Card>

            <Card title="Gaps">
              <section ref={gapsRef}>
                <GapList gaps={gaps} />
              </section>
            </Card>
          </div>

          <Card title="Discovery windows in range">
            <WindowTable windows={windows} />
            {data.partial_windows.length > 0 ? (
              <p className="mt-4 text-xs leading-relaxed text-warning">
                {data.partial_windows.length} partial window(s) also reported
                separately — capped at id_count before covering the full range.
              </p>
            ) : null}
          </Card>

          {data.failed_pages.length > 0 ? (
            <Card title="Failed pages">
              <ul className="space-y-3">
                {data.failed_pages.map((page, i) => (
                  <li
                    key={i}
                    className="rounded-lg border border-error/30 bg-error/5 px-4 py-3"
                  >
                    <pre className="font-mono-data overflow-x-auto text-xs text-foreground">
                      {JSON.stringify(page, null, 2)}
                    </pre>
                    {"last_error" in page && page.last_error ? (
                      <p className="mt-2 text-sm text-error">
                        {String(page.last_error)}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
