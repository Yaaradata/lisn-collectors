"use client";

import type { KeyboardEvent } from "react";

import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Textarea } from "@/components/ui/Textarea";
import {
  formatElapsedLabel,
  showElapsedCounter,
} from "@/lib/hooks/useElapsed";
import { cn } from "@/lib/utils/cn";

export interface ChatInputProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  loading?: boolean;
  elapsedMs?: number;
  disabled?: boolean;
  className?: string;
}

export function ChatInput({
  value,
  onChange,
  onSend,
  loading = false,
  elapsedMs = 0,
  disabled = false,
  className,
}: ChatInputProps) {
  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!loading && value.trim()) onSend();
    }
  }

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <Textarea
        label="Message"
        placeholder="Ask about collection status, gaps, or failures…"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={disabled || loading}
        rows={3}
        className="min-h-[88px] text-base"
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-muted">
          Enter to send · Shift+Enter for a new line
        </p>
        <div className="flex items-center gap-3">
          {loading && showElapsedCounter(elapsedMs) ? (
            <span
              className="flex items-center gap-2 text-xs text-muted"
              aria-live="polite"
            >
              <Spinner size="sm" />
              Working… {formatElapsedLabel(elapsedMs)}
            </span>
          ) : loading ? (
            <span className="flex items-center gap-2 text-xs text-muted">
              <Spinner size="sm" />
              Working…
            </span>
          ) : null}
          <Button
            type="button"
            onClick={onSend}
            loading={loading}
            disabled={disabled || loading || !value.trim()}
          >
            Send
          </Button>
        </div>
      </div>
    </div>
  );
}
