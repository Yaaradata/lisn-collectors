import { Button } from "@/components/ui/Button";
import type { ApiResult } from "@/lib/api/client";
import {
  classifyApiError,
  type ApiErrorContext,
} from "@/lib/utils/errors";
import { cn } from "@/lib/utils/cn";

export interface ApiErrorPanelProps {
  result: Extract<ApiResult<unknown>, { ok: false }>;
  context?: ApiErrorContext;
  onRetry?: () => void;
  className?: string;
}

export function ApiErrorPanel({
  result,
  context = "generic",
  onRetry,
  className,
}: ApiErrorPanelProps) {
  const formatted = classifyApiError(result, context);

  return (
    <div
      role="alert"
      className={cn(
        "rounded-lg border border-error/40 bg-error/10 px-4 py-3",
        className,
      )}
    >
      <p className="text-sm font-semibold text-error">{formatted.title}</p>
      <p className="mt-1 text-sm leading-relaxed text-foreground">
        {formatted.message}
      </p>
      {formatted.hint ? (
        <pre className="font-mono-data mt-3 whitespace-pre-wrap rounded border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted">
          {formatted.hint}
        </pre>
      ) : null}
      {formatted.showRetry && onRetry ? (
        <Button variant="secondary" className="mt-3" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}
