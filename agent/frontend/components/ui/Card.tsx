import type { ReactNode } from "react";

import { cn } from "@/lib/utils/cn";

export interface CardProps {
  title?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function Card({ title, action, children, className }: CardProps) {
  return (
    <section
      className={cn(
        "rounded-lg border border-border bg-surface",
        className,
      )}
    >
      {title || action ? (
        <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3 sm:px-5">
          {title ? (
            <h2 className="text-sm font-semibold tracking-tight text-foreground">
              {title}
            </h2>
          ) : (
            <span />
          )}
          {action ? <div className="shrink-0">{action}</div> : null}
        </header>
      ) : null}
      <div className="px-4 py-4 sm:px-5 sm:py-5">{children}</div>
    </section>
  );
}
