export function Header() {
  return (
    <header className="border-b border-border bg-surface">
      <div className="mx-auto flex max-w-6xl flex-col gap-1 px-4 py-4 sm:px-6 sm:py-5">
        <h1 className="text-lg font-semibold tracking-tight text-foreground sm:text-xl">
          LiSN Collector — Ops Console
        </h1>
        <p className="max-w-2xl text-sm leading-relaxed text-muted">
          Read-only diagnostics over collector state, warehouse, logs, and job
          history. This tool cannot collect, reset, or modify data.
        </p>
      </div>
    </header>
  );
}
