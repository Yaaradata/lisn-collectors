import { Skeleton } from "@/components/ui/Skeleton";

/** Skeleton matching stat tiles + tables while range diagnosis runs. */
export function RangeDiagnosisSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading range diagnosis">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className="rounded-lg border border-border bg-surface p-4"
          >
            <Skeleton className="mb-2 h-3 w-24" />
            <Skeleton className="h-8 w-[9ch] min-w-[9ch]" />
          </div>
        ))}
      </div>
      <section className="rounded-lg border border-border bg-surface px-4 py-4 sm:px-5">
        <Skeleton className="mb-4 h-4 w-28" />
        <Skeleton className="mb-2 h-4 w-full max-w-lg" />
        <Skeleton className="h-4 w-3/4 max-w-md" />
      </section>
      <section className="rounded-lg border border-border bg-surface px-4 py-4 sm:px-5">
        <Skeleton className="mb-4 h-4 w-44" />
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      </section>
    </div>
  );
}
