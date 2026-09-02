import { MarkdownBody } from "@/components/chat/MarkdownBody";
import { ToolCallCard } from "@/components/chat/ToolCallCard";
import { Button } from "@/components/ui/Button";
import type { ChatUiMessage } from "@/lib/utils/chat-messages";

export interface MessageBubbleProps {
  message: ChatUiMessage;
  onRetry?: (retryMessage: string) => void;
}

export function MessageBubble({ message, onRetry }: MessageBubbleProps) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-lg bg-accent/20 px-4 py-2.5 text-sm leading-relaxed text-foreground">
          {message.content}
        </div>
      </div>
    );
  }

  if (message.role === "error") {
    return (
      <div className="flex justify-start">
        <div className="max-w-[95%] rounded-lg border border-error/40 bg-error/10 px-4 py-3">
          <p className="text-sm font-medium text-error">Request failed</p>
          <p className="mt-1 text-sm text-foreground/90">{message.error}</p>
          {onRetry ? (
            <Button
              variant="secondary"
              className="mt-3"
              onClick={() => onRetry(message.retryMessage)}
            >
              Retry
            </Button>
          ) : null}
        </div>
      </div>
    );
  }

  const hasTools = message.toolCalls.length > 0;

  return (
    <div className="flex justify-start">
      <div className="max-w-[95%] space-y-3">
        {hasTools ? (
          <div className="space-y-2">
            <p className="text-xs font-medium uppercase tracking-wide text-muted">
              Tool calls
            </p>
            {message.toolCalls.map((call, i) => (
              <ToolCallCard key={call.id ?? `${call.name}-${i}`} call={call} />
            ))}
          </div>
        ) : (
          <p className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
            Answered without checking any source
          </p>
        )}

        {message.stoppedEarly ? (
          <p className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            Stopped at the iteration cap — the model may not have finished
            calling tools.
          </p>
        ) : null}

        {message.content.trim() ? (
          <div className="rounded-lg border border-border bg-surface px-4 py-3">
            <MarkdownBody content={message.content} />
          </div>
        ) : (
          <p className="text-sm italic text-muted">No reply text returned.</p>
        )}
      </div>
    </div>
  );
}
