"use client";

import { useState } from "react";

import { CodeBlock } from "@/components/ui/CodeBlock";
import type { ToolCall } from "@/lib/types";
import { cn } from "@/lib/utils/cn";
import {
  formatToolArgs,
  parseToolResult,
  toolRowCountLabel,
} from "@/lib/utils/tool-call";

export interface ToolCallCardProps {
  call: ToolCall;
  className?: string;
}

/**
 * Collapsed-but-visible by default: tool name, args, and row count are readable
 * without clicking. Expanding reveals query and full result.
 */
export function ToolCallCard({ call, className }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);
  const parsed = parseToolResult(call.result);
  const argsLine = formatToolArgs(call.args);

  return (
    <div
      className={cn(
        "rounded-md border border-border bg-surface/80 text-sm",
        className,
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-left hover:bg-surface-raised/60"
      >
        <span className="font-mono-data text-xs text-muted">
          {expanded ? "▼" : "▶"}
        </span>
        <span className="font-mono-data font-medium text-accent">
          {call.name}
        </span>
        {argsLine ? (
          <span className="font-mono-data min-w-0 flex-1 truncate text-xs text-foreground/90">
            {argsLine}
          </span>
        ) : null}
        <span className="font-mono-data shrink-0 text-xs tabular-nums text-muted">
          {toolRowCountLabel(call)}
        </span>
      </button>

      {expanded ? (
        <div className="space-y-3 border-t border-border px-3 py-3">
          {call.args && Object.keys(call.args).length > 0 ? (
            <div>
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                Arguments
              </p>
              <pre className="font-mono-data overflow-x-auto rounded border border-border bg-background p-2 text-xs">
                {JSON.stringify(call.args, null, 2)}
              </pre>
            </div>
          ) : null}
          {parsed.query ? (
            <div>
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                Query
              </p>
              <CodeBlock code={parsed.query} />
            </div>
          ) : null}
          {parsed.error ? (
            <p className="text-sm text-error">{parsed.error}</p>
          ) : null}
          {call.result != null && !parsed.error ? (
            <div>
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                Result
              </p>
              <pre className="font-mono-data max-h-64 overflow-auto rounded border border-border bg-background p-2 text-xs leading-relaxed">
                {typeof call.result === "string"
                  ? call.result
                  : JSON.stringify(call.result, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
