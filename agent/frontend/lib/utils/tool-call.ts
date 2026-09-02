import type { ToolCall } from "@/lib/types";

export interface ParsedToolResult {
  rowCount: number | null;
  query: string | null;
  data: unknown;
  error: string | null;
}

export function parseToolResult(result: unknown): ParsedToolResult {
  if (result == null) {
    return { rowCount: null, query: null, data: null, error: null };
  }
  if (typeof result === "string") {
    const isError = /error/i.test(result);
    return {
      rowCount: null,
      query: null,
      data: isError ? null : result,
      error: isError ? result : null,
    };
  }
  if (typeof result === "object" && "error" in (result as object)) {
    const r = result as Record<string, unknown>;
    return {
      rowCount: typeof r.row_count === "number" ? r.row_count : null,
      query: typeof r.query === "string" ? r.query : null,
      data: r.data ?? null,
      error: typeof r.error === "string" ? r.error : null,
    };
  }
  if (typeof result === "object") {
    const r = result as Record<string, unknown>;
    return {
      rowCount: typeof r.row_count === "number" ? r.row_count : null,
      query: typeof r.query === "string" ? r.query : null,
      data: r.data ?? result,
      error: null,
    };
  }
  return { rowCount: null, query: null, data: result, error: null };
}

/** One-line args for collapsed tool card — readable without expanding. */
export function formatToolArgs(args: Record<string, unknown> | undefined): string {
  if (!args || Object.keys(args).length === 0) return "";
  const parts = Object.entries(args).map(([key, value]) => {
    if (value == null) return `${key}=null`;
    if (typeof value === "string") return `${key}=${value}`;
    if (typeof value === "number" || typeof value === "boolean") {
      return `${key}=${String(value)}`;
    }
    return `${key}=${JSON.stringify(value)}`;
  });
  return parts.join(", ");
}

export function toolRowCountLabel(call: ToolCall): string {
  const parsed = parseToolResult(call.result);
  if (parsed.error) return "error";
  if (parsed.rowCount != null) {
    const n = parsed.rowCount;
    return `${n} row${n === 1 ? "" : "s"}`;
  }
  if (call.result == null) {
    return "no result";
  }
  return "—";
}
