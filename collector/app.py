import os

import procrastinate

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
