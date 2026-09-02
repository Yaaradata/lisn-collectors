import { cn } from "@/lib/utils/cn";

export const SUGGESTED_QUESTIONS = [
  "Was incident IN26081800000000027963 collected?",
  "How many incidents did we collect between 20 and 23 August?",
  "Why is there a gap on 21 August?",
  "Which pages failed in the last 24 hours?",
  "Were the workers running yesterday?",
] as const;

export interface SuggestedQuestionsProps {
  onSelect: (question: string) => void;
  disabled?: boolean;
  className?: string;
}

export function SuggestedQuestions({
  onSelect,
  disabled = false,
  className,
}: SuggestedQuestionsProps) {
  return (
    <div className={cn("space-y-3", className)}>
      <p className="text-sm font-medium text-foreground">
        Try one of these questions
      </p>
      <ul className="grid gap-2 sm:grid-cols-2">
        {SUGGESTED_QUESTIONS.map((question) => (
          <li key={question}>
            <button
              type="button"
              disabled={disabled}
              onClick={() => onSelect(question)}
              className={cn(
                "w-full rounded-lg border border-border bg-surface px-3 py-2.5 text-left text-sm leading-snug text-foreground transition-colors",
                "hover:border-accent/50 hover:bg-surface-raised",
                "disabled:cursor-not-allowed disabled:opacity-50",
              )}
            >
              {question}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
