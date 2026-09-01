# Platform logs → SigNoz — decision (pilot)

**Status:** decided for the pilot. **Do not build either option without an
explicit go-ahead** beyond this recommendation.

## The gap

Direct OTLP from the process cannot capture what Cloud Run says **after the
process is dead**. The lines that explained the four-day outage live in the
platform log stream, not in application stdout:

- `Terminating task because it has reached the maximum timeout of 86400 seconds`
- `Container called exit(1)`

OTLP dies with the container. Those messages never become SigNoz log records
unless something else ships Cloud Logging → SigNoz.

## Option 1 — Pub/Sub + OTel collector (SigNoz docs path)

A Log Router sink filters `resource.type="cloud_run_job"` (and optionally
`cloud_run_revision` for services), publishes to a Pub/Sub topic, and a
permanently running GCE VM (or Cloud Run service) runs an OpenTelemetry
Collector that pulls Pub/Sub and exports OTLP to SigNoz.

| | |
|---|---|
| Coverage | Complete — platform lines land in SigNoz next to app logs |
| Cost / ops | A VM (or always-on collector) for a handful of lines per day |
| Complexity | Sink + topic + IAM + collector config + upgrades |

## Option 2 — Infer from metrics + keep Cloud Logging as evidence

`lisn.workers.live` already goes to zero when every task dies, **whatever the
cause** (86400s ceiling, `exit(1)`, OOM, cancel). Pair it with:

- The **CRITICAL `workers.live == 0` for 5 minutes** alert in SigNoz (Part B)
- Optionally a **Cloud Monitoring** alert on Cloud Run job execution failures /
  exited tasks, emailing or webhooking without Pub/Sub

When someone investigates, the exact platform message is still in Cloud Logging
under the job execution — one click from the GCP console.

| | |
|---|---|
| Coverage | Detects the outage condition; does not mirror the GCP log line into SigNoz |
| Cost / ops | Near zero beyond alerts already required |
| Complexity | Alert rules only |

## Recommendation (pilot): Option 2

The metric detects the same condition that mattered on 27/29 August. The alert
is what would have saved four days. The exact platform string is available in
Cloud Logging when a human digs in.

**Revisit Option 1** only if the org requires SigNoz as the single pane of glass
for platform + app logs (compliance, on-call that never opens GCP, etc.).

## What we will not do without asking

- Create a Log Router sink / Pub/Sub topic / collector VM (Option 1)
- Create Cloud Monitoring alert policies (Option 2 enhancement) — optional later;
  SigNoz `workers.live` is the primary page for the pilot
