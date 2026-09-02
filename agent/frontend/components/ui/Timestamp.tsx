import {
  formatTimestampLocal,
  formatTimestampUtc,
  parseIsoDate,
} from "@/lib/utils/format";
import { cn } from "@/lib/utils/cn";

export interface TimestampProps {
  value: string | Date | null | undefined;
  className?: string;
}

/**
 * UTC primary (discovery windows are UTC); local timezone in tooltip for IST
 * operators and others.
 */
export function Timestamp({ value, className }: TimestampProps) {
  const d =
    typeof value === "string"
      ? parseIsoDate(value)
      : value instanceof Date
        ? value
        : null;

  if (!d) {
    return <span className={cn("font-mono-data text-muted", className)}>—</span>;
  }

  const utc = formatTimestampUtc(d);
  const local = formatTimestampLocal(d);

  return (
    <time
      dateTime={d.toISOString()}
      title={`Local: ${local}`}
      className={cn("font-mono-data cursor-help text-foreground", className)}
    >
      {utc}
    </time>
  );
}
