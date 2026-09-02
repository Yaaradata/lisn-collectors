import { cn } from "@/lib/utils/cn";

export interface SpinnerProps {
  className?: string;
  size?: "sm" | "md";
}

export function Spinner({ className, size = "md" }: SpinnerProps) {
  const dim = size === "sm" ? "h-3.5 w-3.5 border" : "h-5 w-5 border-2";
  return (
    <span
      role="status"
      aria-label="Loading"
      className={cn(
        "inline-block animate-spin rounded-full border-muted border-t-accent",
        dim,
        className,
      )}
    />
  );
}
