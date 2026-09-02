"use client";

import { useCallback, useState } from "react";

import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/utils/cn";

export interface CodeBlockProps {
  code: string;
  className?: string;
}

export function CodeBlock({ code, className }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }, [code]);

  return (
    <div
      className={cn(
        "relative rounded-md border border-border bg-background",
        className,
      )}
    >
      <div className="absolute right-2 top-2">
        <Button variant="ghost" className="px-2 py-1 text-xs" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre className="font-mono-data overflow-x-auto whitespace-pre p-4 pr-20 text-xs leading-relaxed text-foreground/90 sm:text-sm">
        <code>{code}</code>
      </pre>
    </div>
  );
}
