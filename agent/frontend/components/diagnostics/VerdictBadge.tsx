import { Badge } from "@/components/ui/Badge";
import type { IncidentDiagnosis } from "@/lib/types";
import { cn } from "@/lib/utils/cn";
import { verdictPresentation } from "@/lib/utils/verdict";

export interface VerdictBadgeProps {
  diagnosis: IncidentDiagnosis;
  className?: string;
}

const sizeClasses: Record<string, string> = {
  success: "text-success border-success/50 bg-success/15",
  warning: "text-warning border-warning/50 bg-warning/15",
  error: "text-error border-error/50 bg-error/15",
  info: "text-info border-info/50 bg-info/15",
  neutral: "text-muted border-border bg-surface-raised",
};

export function VerdictBadge({ diagnosis, className }: VerdictBadgeProps) {
  const { variant, label, subtitle } = verdictPresentation(diagnosis);

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      <Badge
        variant={variant}
        className={cn(
          "w-fit rounded-md px-4 py-2 text-base font-semibold tracking-wide",
          sizeClasses[variant],
        )}
      >
        {label}
      </Badge>
      <p className="max-w-2xl text-base leading-relaxed text-foreground">
        {subtitle}
      </p>
      {diagnosis.summary && diagnosis.verdict !== "COLLECTED" ? (
        <p className="font-mono-data text-sm leading-relaxed text-muted">
          {diagnosis.summary}
        </p>
      ) : null}
    </div>
  );
}
