const ISO_RE =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/;

export function parseIsoDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** UTC timestamp — primary display for discovery windows. */
export function formatTimestampUtc(
  value: string | Date | null | undefined,
): string {
  const d =
    typeof value === "string"
      ? parseIsoDate(value)
      : value instanceof Date
        ? value
        : null;
  if (!d) return "—";
  return d.toLocaleString("en-GB", {
    timeZone: "UTC",
    dateStyle: "medium",
    timeStyle: "short",
    hour12: false,
  }).replace(",", "") + " UTC";
}

/** Local timestamp — shown in tooltips alongside UTC. */
export function formatTimestampLocal(
  value: string | Date | null | undefined,
): string {
  const d =
    typeof value === "string"
      ? parseIsoDate(value)
      : value instanceof Date
        ? value
        : null;
  if (!d) return "—";
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    d.toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }) + ` (${tz})`
  );
}

/** @deprecated Prefer Timestamp component — UTC primary with local tooltip. */
export function formatDateTime(
  value: string | Date | null | undefined,
  options?: Intl.DateTimeFormatOptions,
): string {
  const d =
    typeof value === "string"
      ? parseIsoDate(value)
      : value instanceof Date
        ? value
        : null;
  if (!d) return "—";
  return d.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    ...options,
  });
}

/** Compact relative time for status bars. */
export function formatRelativeTime(value: string | Date | null | undefined): string {
  const d =
    typeof value === "string"
      ? parseIsoDate(value)
      : value instanceof Date
        ? value
        : null;
  if (!d) return "—";
  const sec = Math.round((d.getTime() - Date.now()) / 1000);
  const abs = Math.abs(sec);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (abs < 60) return rtf.format(sec, "second");
  if (abs < 3600) return rtf.format(Math.round(sec / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(sec / 3600), "hour");
  return rtf.format(Math.round(sec / 86400), "day");
}

export function formatDurationMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return formatDurationSeconds(Math.round(ms / 1000));
}

/** Human-readable duration: "2 days, 11 hours" not raw seconds. */
export function formatDurationSeconds(totalSeconds: number): string {
  const abs = Math.abs(Math.round(totalSeconds));
  if (abs < 60) return `${abs} second${abs === 1 ? "" : "s"}`;

  const days = Math.floor(abs / 86400);
  const hours = Math.floor((abs % 86400) / 3600);
  const minutes = Math.floor((abs % 3600) / 60);
  const seconds = abs % 60;

  const parts: string[] = [];
  if (days > 0) parts.push(`${days} day${days === 1 ? "" : "s"}`);
  if (hours > 0) parts.push(`${hours} hour${hours === 1 ? "" : "s"}`);
  if (minutes > 0 && days === 0) {
    parts.push(`${minutes} minute${minutes === 1 ? "" : "s"}`);
  }
  if (seconds > 0 && days === 0 && hours === 0) {
    parts.push(`${seconds} second${seconds === 1 ? "" : "s"}`);
  }
  return parts.join(", ") || "0 seconds";
}

/** 1_234_567 → "1.2M" etc. */
export function formatCompactNumber(n: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n);
}

/** 3234265 → "3,234,265" */
export function formatInteger(n: number): string {
  return new Intl.NumberFormat(undefined).format(n);
}

/**
 * Middle truncation keeps both ends — IN2608…27963 is how operators
 * recognise ids at a glance.
 */
export function truncateMiddle(
  value: string,
  startChars = 6,
  endChars = 5,
): string {
  if (value.length <= startChars + endChars + 1) return value;
  return `${value.slice(0, startChars)}…${value.slice(-endChars)}`;
}

export function isIsoDateString(value: unknown): value is string {
  return typeof value === "string" && ISO_RE.test(value);
}
