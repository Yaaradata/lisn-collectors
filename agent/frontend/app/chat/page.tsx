"use client";

import { useState } from "react";

import {
  ChatInput,
  MessageList,
  SuggestedQuestions,
} from "@/components/chat";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { useChat } from "@/lib/hooks/useChat";
import { useHealth } from "@/lib/hooks/useHealth";

function ChatHistorySkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading conversation">
      <Skeleton className="ml-auto h-12 w-2/3 max-w-md" />
      <Skeleton className="h-24 w-full max-w-lg" />
      <Skeleton className="ml-auto h-10 w-1/2 max-w-xs" />
    </div>
  );
}

export default function ChatPage() {
  const [draft, setDraft] = useState("");
  const { result: health } = useHealth(30_000);
  const {
    sessionId,
    messages,
    historyLoading,
    sending,
    elapsedMs,
    sendMessage,
    retry,
    newConversation,
  } = useChat();

  const chatReady = health?.ok ? health.data.chat_ready : true;
  const empty = !historyLoading && messages.length === 0;

  async function handleSend() {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    await sendMessage(text);
  }

  function handleSuggested(question: string) {
    void sendMessage(question);
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-foreground">Chat</h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-muted">
            Natural-language questions over live collector data. Every answer
            shows which tools ran — expand a tool call to see the query and
            result.
          </p>
        </div>
        <Button
          variant="secondary"
          onClick={() => void newConversation()}
          disabled={sending}
          className="shrink-0"
        >
          New conversation
        </Button>
      </div>

      {!chatReady ? (
        <Card>
          <p className="text-sm text-warning">
            Chat model is not initialised on the backend. Direct diagnosis at{" "}
            <a href="/diagnose" className="text-accent hover:underline">
              /diagnose
            </a>{" "}
            still works.
          </p>
        </Card>
      ) : null}

      <Card className="min-h-[320px]">
        {historyLoading ? (
          <ChatHistorySkeleton />
        ) : empty ? (
          <SuggestedQuestions
            onSelect={handleSuggested}
            disabled={sending || !chatReady}
          />
        ) : (
          <MessageList messages={messages} onRetry={(msg) => void retry(msg)} />
        )}
      </Card>

      <Card>
        <ChatInput
          value={draft}
          onChange={setDraft}
          onSend={() => void handleSend()}
          loading={sending}
          elapsedMs={elapsedMs}
          disabled={!chatReady}
        />
        {sessionId ? (
          <p className="mt-3 font-mono-data text-xs text-muted">
            session {sessionId.slice(0, 8)}…
          </p>
        ) : null}
      </Card>
    </div>
  );
}
