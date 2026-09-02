import type { TextareaHTMLAttributes } from "react";

import { cn } from "@/lib/utils/cn";

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: string;
}

export function Textarea({ label, className, id, ...props }: TextareaProps) {
  const inputId = id ?? label?.toLowerCase().replace(/\s+/g, "-");
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      {label ? (
        <span className="font-medium text-muted">{label}</span>
      ) : null}
      <textarea
        id={inputId}
        className={cn(
          "min-h-[100px] resize-y rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground",
          "placeholder:text-muted/70 focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent",
          className,
        )}
        {...props}
      />
    </label>
  );
}
