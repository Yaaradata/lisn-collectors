import { EntryPoints, HealthSummary } from "@/components/layout/HealthSummary";

export default function Home() {
  return (
    <div className="flex flex-col gap-8">
      <section>
        <h2 className="mb-2 text-base font-semibold text-foreground">
          What do you need?
        </h2>
        <p className="mb-5 max-w-2xl text-sm leading-relaxed text-muted">
          Start with a direct diagnosis when you have an incident id. Use chat
          when the question is open-ended or spans several systems.
        </p>
        <EntryPoints />
      </section>

      <HealthSummary />
    </div>
  );
}
