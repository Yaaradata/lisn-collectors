import type { ReactNode } from "react";

import { cn } from "@/lib/utils/cn";
import { formatInteger } from "@/lib/utils/format";

export interface StatTileProps {
  label: string;
  value: number;
  highlight?: "none" | "warning" | "error";
  onClick?: () => void;
  className?: string;
  footer?: ReactNode;
}

const highlightClasses = {
  none: "border-border bg-surface",
  warning: "border-warning/50 bg-warning/10",
  error: "border-error/50 bg-error/10",
};

export function StatTile({
  label,
  value,
  highlight = "none",
  onClick,
  className,
  footer,
}: StatTileProps) {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag
      type={onClick ? "button" : undefined}
      onClick={onClick}
      className={cn(
        "flex flex-col gap-1 rounded-lg border p-4 text-left transition-colors",
        highlightClasses[highlight],
        onClick && "cursor-pointer hover:border-accent/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
        className,
      )}
    >
      <span className="text-xs font-medium uppercase tracking-wide text-muted">
        {label}
      </span>
      <span className="font-mono-data min-w-[9ch] text-2xl font-semibold tabular-nums text-foreground">
        {formatInteger(value)}
      </span>
      {footer ? (
        <span className="text-xs text-muted">{footer}</span>
      ) : null}
    </Tag>
  );
}
