"use client";

import { useCallback, useState } from "react";

import { Button } from "@/components/ui/Button";
import { truncateMiddle } from "@/lib/utils/format";
import { cn } from "@/lib/utils/cn";

export interface TruncatedIdProps {
  value: string;
  startChars?: number;
  endChars?: number;
  className?: string;
}

export function TruncatedId({
  value,
  startChars = 6,
  endChars = 5,
  className,
}: TruncatedIdProps) {
  const [copied, setCopied] = useState(false);
  const display = truncateMiddle(value, startChars, endChars);
  const needsTruncation = display !== value;

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }, [value]);

  return (
    <span className={cn("inline-flex items-center gap-1.5", className)}>
      <code
        className="font-mono-data text-sm text-foreground"
        title={needsTruncation ? value : undefined}
      >
        {display}
      </code>
      <Button
        type="button"
        variant="ghost"
        className="h-7 px-2 py-0 text-xs"
        onClick={() => void copy()}
        aria-label={`Copy full id ${value}`}
      >
        {copied ? "Copied" : "Copy"}
      </Button>
    </span>
  );
}
