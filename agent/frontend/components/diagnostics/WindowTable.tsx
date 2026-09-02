import { Badge } from "@/components/ui/Badge";
import { Timestamp } from "@/components/ui/Timestamp";
import { formatInteger } from "@/lib/utils/format";
import { cn } from "@/lib/utils/cn";

export interface DiscoveryWindowRow {
  window_id?: string;
  window_from?: string;
  window_to?: string;
  id_count?: number;
  status?: string;
  [key: string]: unknown;
}

export interface WindowTableProps {
  windows: DiscoveryWindowRow[];
  className?: string;
}

const PARTIAL_TOOLTIP =
  "This window hit its id_count cap and covered only part of its range, even though its calendar boundaries look continuous.";

function WindowCard({ row }: { row: DiscoveryWindowRow }) {
  const partial = row.status === "partial";
  return (
    <li
      className={cn(
        "rounded-lg border border-border px-4 py-3",
        partial && "border-warning/40 bg-warning/10",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">
          Window
        </span>
        {partial ? (
          <Badge variant="warning" title={PARTIAL_TOOLTIP}>
            partial
          </Badge>
        ) : (
          <span className="font-mono-data text-xs">{String(row.status ?? "—")}</span>
        )}
      </div>
      <dl className="mt-2 space-y-1.5 text-sm">
        <div>
          <dt className="text-xs text-muted">From</dt>
          <dd>
            <Timestamp value={row.window_from as string | undefined} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">To</dt>
          <dd>
            <Timestamp value={row.window_to as string | undefined} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">id_count</dt>
          <dd className="font-mono-data tabular-nums">
            {row.id_count != null ? formatInteger(Number(row.id_count)) : "—"}
          </dd>
        </div>
      </dl>
    </li>
  );
}

export function WindowTable({ windows, className }: WindowTableProps) {
  if (windows.length === 0) {
    return (
      <p className="text-sm text-muted">
        No discovery windows overlap this range.
      </p>
    );
  }

  return (
    <div className={className}>
      {/* Mobile: card list */}
      <ul className="space-y-3 md:hidden">
        {windows.map((row, i) => (
          <WindowCard key={String(row.window_id ?? i)} row={row} />
        ))}
      </ul>

      {/* Tablet+: horizontal scroll table */}
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full min-w-[640px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted">
              <th className="px-3 py-2 font-medium">From</th>
              <th className="px-3 py-2 font-medium">To</th>
              <th className="px-3 py-2 font-medium">id_count</th>
              <th className="px-3 py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {windows.map((row, i) => {
              const partial = row.status === "partial";
              return (
                <tr
                  key={String(row.window_id ?? i)}
                  className={cn(
                    "border-b border-border/60",
                    partial && "bg-warning/10",
                  )}
                  title={partial ? PARTIAL_TOOLTIP : undefined}
                >
                  <td className="px-3 py-2.5 text-xs">
                    <Timestamp value={row.window_from as string | undefined} />
                  </td>
                  <td className="px-3 py-2.5 text-xs">
                    <Timestamp value={row.window_to as string | undefined} />
                  </td>
                  <td className="font-mono-data px-3 py-2.5 tabular-nums">
                    {row.id_count != null
                      ? formatInteger(Number(row.id_count))
                      : "—"}
                  </td>
                  <td className="px-3 py-2.5">
                    {partial ? (
                      <Badge variant="warning" title={PARTIAL_TOOLTIP}>
                        partial
                      </Badge>
                    ) : (
                      <span className="font-mono-data text-xs">
                        {String(row.status ?? "—")}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
