import type { InputHTMLAttributes } from "react";

import { cn } from "@/lib/utils/cn";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
}

export function Input({ label, className, id, ...props }: InputProps) {
  const inputId = id ?? label?.toLowerCase().replace(/\s+/g, "-");
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      {label ? (
        <span className="font-medium text-muted">{label}</span>
      ) : null}
      <input
        id={inputId}
        className={cn(
          "font-mono-data rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground",
          "placeholder:text-muted/70 focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent",
          className,
        )}
        {...props}
      />
    </label>
  );
}
