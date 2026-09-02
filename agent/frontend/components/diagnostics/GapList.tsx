import { Timestamp } from "@/components/ui/Timestamp";
import { formatDurationSeconds } from "@/lib/utils/format";
import { cn } from "@/lib/utils/cn";

export interface GapRow {
  gap_from?: string;
  gap_to?: string;
  reason?: string;
  gap_duration?: number | string;
  [key: string]: unknown;
}

export interface GapListProps {
  gaps: GapRow[];
  className?: string;
}

function reasonLabel(reason: string | undefined): string {
  switch (reason) {
    case "not_scheduled":
      return "Never scheduled — no discovery window covers this interval";
    case "truncated":
      return "Truncated — window stopped at id_count limit before reaching the end";
    default:
      return reason ?? "unknown";
  }
}

function formatGapDuration(raw: unknown): string | null {
  if (raw == null) return null;
  if (typeof raw === "number") {
    return formatDurationSeconds(raw);
  }
  if (typeof raw === "string") {
    const n = Number(raw);
    if (!Number.isNaN(n)) return formatDurationSeconds(n);
  }
  return null;
}

export function GapList({ gaps, className }: GapListProps) {
  if (gaps.length === 0) {
    return (
      <p className="text-sm text-muted">
        No boundary gaps recorded in this range.
      </p>
    );
  }

  return (
    <ul className={cn("space-y-3", className)}>
      {gaps.map((gap, i) => {
        const duration = formatGapDuration(gap.gap_duration);
        return (
          <li
            key={`${gap.gap_from}-${gap.gap_to}-${i}`}
            className="rounded-lg border border-warning/30 bg-warning/5 px-4 py-3"
          >
            <p className="text-sm text-foreground">
              <Timestamp value={gap.gap_from as string | undefined} />
              {" → "}
              <Timestamp value={gap.gap_to as string | undefined} />
            </p>
            {duration ? (
              <p className="font-mono-data mt-1 text-xs text-muted">
                Duration: {duration}
              </p>
            ) : null}
            <p className="mt-1 text-sm text-muted">
              {reasonLabel(gap.reason as string | undefined)}
            </p>
          </li>
        );
      })}
    </ul>
  );
}
