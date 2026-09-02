import { Skeleton } from "@/components/ui/Skeleton";

/** Skeleton matching verdict + chain layout while incident diagnosis runs. */
export function IncidentDiagnosisSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading diagnosis">
      <section className="rounded-lg border border-border bg-surface px-4 py-4 sm:px-5">
        <Skeleton className="mb-3 h-4 w-16" />
        <Skeleton className="mb-3 h-10 w-48 rounded-md" />
        <Skeleton className="h-4 w-full max-w-xl" />
        <Skeleton className="mt-2 h-4 w-2/3 max-w-md" />
      </section>
      <section className="rounded-lg border border-border bg-surface px-4 py-4 sm:px-5">
        <Skeleton className="mb-4 h-4 w-36" />
        <div className="space-y-2">
          {[1, 2, 3, 4].map((i) => (
            <div
              key={i}
              className="rounded-lg border border-border px-4 py-3"
            >
              <Skeleton className="mb-2 h-4 w-56" />
              <Skeleton className="h-3 w-40" />
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
