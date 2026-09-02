"use client";

import { Badge } from "@/components/ui/Badge";
import { useHealth } from "@/lib/hooks/useHealth";
import { formatTimestampUtc } from "@/lib/utils/format";
import { buildStatusBarPills, sourcePillState, STATUS_BAR_SOURCES } from "@/lib/utils/health";

const POLL_MS = 30_000;

function worstVariant(
  pills: ReturnType<typeof buildStatusBarPills>,
): "success" | "warning" | "error" | "neutral" {
  if (pills.some((p) => p.variant === "error")) return "error";
  if (pills.some((p) => p.variant === "warning")) return "warning";
  if (pills.every((p) => p.variant === "success")) return "success";
  return "neutral";
}

function mobileSummary(
  pills: ReturnType<typeof buildStatusBarPills>,
): string {
  const ok = pills.filter((p) => p.variant === "success").length;
  const bad = pills.filter((p) => p.variant === "error").length;
  const other = pills.length - ok - bad;
  const parts = [`${ok} ok`];
  if (bad > 0) parts.push(`${bad} error`);
  if (other > 0) parts.push(`${other} other`);
  return parts.join(" · ");
}

export function StatusBar() {
  const { initialLoading, refreshing, result, lastCheckedAt } = useHealth(POLL_MS);

  const pills = result?.ok
    ? buildStatusBarPills(result.data)
    : STATUS_BAR_SOURCES.map((id) =>
        sourcePillState(id, undefined),
      ).map((p) => ({
        ...p,
        variant: "error" as const,
        statusLabel: result?.ok === false ? "fetch failed" : "unknown",
        message: result?.ok === false ? result.error : "No data yet",
      }));

  const summaryVariant = worstVariant(pills);
  const checkedLabel = lastCheckedAt
    ? formatTimestampUtc(lastCheckedAt)
    : "—";

  return (
    <footer
      className="sticky bottom-0 z-10 border-t border-border bg-surface/95 backdrop-blur-sm"
      aria-label="Source health"
    >
      <div className="mx-auto flex max-w-6xl flex-col gap-2 px-4 py-2.5 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        {/* Mobile: single summary pill — never blocks the page */}
        <div className="flex min-w-0 items-center gap-2 sm:hidden">
          <Badge variant={summaryVariant} className="max-w-full truncate font-mono-data">
            Sources · {mobileSummary(pills)}
          </Badge>
        </div>

        {/* Tablet+ : full pill row — stale data stays visible while polling */}
        <div className="hidden min-w-0 flex-wrap items-center gap-2 sm:flex">
          {pills.map((pill) => (
            <Badge
              key={pill.id}
              variant={pill.variant}
              title={pill.message}
              className="font-mono-data shrink-0"
            >
              <span>{pill.label}</span>
              <span className="opacity-70">·</span>
              <span>{pill.statusLabel}</span>
            </Badge>
          ))}
        </div>

        <p className="font-mono-data shrink-0 text-xs text-muted">
          Last checked: {checkedLabel}
          {refreshing ? " (refreshing…)" : initialLoading && !lastCheckedAt ? " (loading…)" : ""}
        </p>
      </div>
    </footer>
  );
}
