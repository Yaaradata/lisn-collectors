import os

import procrastinate

# Identity is derived from the source plus a STABLE Cloud Run value, never from
# a hostname or a UUID. With jobs, CLOUD_RUN_TASK_INDEX is deterministic across
# executions, so a restarted task 0 has the same identity as the task 0 that
# died. That is what lets Procrastinate's own recovery find its stranded jobs at
# startup instead of relying entirely on the sweeper.
task_index = os.environ.get("CLOUD_RUN_TASK_INDEX")  # jobs
instance = os.environ.get("CLOUD_RUN_WORKER_POOL_REVISION")  # pools, if set
source = os.environ.get("COLLECTOR_SOURCE", "local")

if task_index is not None:
    WORKER_ID = f"{source}-task{task_index}"
elif instance:
    WORKER_ID = f"{source}-{instance}"
else:
    WORKER_ID = f"{source}-local"

# PsycopgConnector is the async psycopg3 connector and the current default.
# SyncPsycopgConnector exists for purely synchronous callers;
# Psycopg2Connector and AiopgConnector are legacy.
#
# COLLECTOR_DSN has two shapes and both work unchanged:
#   local    postgresql://postgres:PW@127.0.0.1:5432/collector
#   deployed postgresql://postgres:PW@/collector?host=/cloudsql/<CONN>
# The deployed form is what lives in the collector-dsn secret.
app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(
        conninfo=os.environ["COLLECTOR_DSN"],
    ),
    import_paths=["collector.tasks"],
)
app.WORKER_ID = WORKER_ID
