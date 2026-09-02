import { MessageBubble } from "@/components/chat/MessageBubble";
import type { ChatUiMessage } from "@/lib/utils/chat-messages";
import { cn } from "@/lib/utils/cn";

export interface MessageListProps {
  messages: ChatUiMessage[];
  onRetry?: (retryMessage: string) => void;
  className?: string;
}

export function MessageList({ messages, onRetry, className }: MessageListProps) {
  if (messages.length === 0) return null;

  return (
    <div
      className={cn("flex flex-col gap-4", className)}
      aria-live="polite"
      aria-relevant="additions"
      role="log"
      aria-label="Chat messages"
    >
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} onRetry={onRetry} />
      ))}
    </div>
  );
}
