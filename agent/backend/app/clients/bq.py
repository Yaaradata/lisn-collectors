"""BigQuery client — read-only, bytes-billed capped.

Why maximum_bytes_billed is mandatory:
  sentinel_raw.incidents_v2 held ~3.2 million rows before the last reset.
  BigQuery bills by bytes scanned. An unbounded SELECT from a vague
  operational question is a real cost, not a theoretical one. Every query
  through this client must set the job config limit from Settings; helpers
  refuse to run without it.
"""

from __future__ import annotations

import logging
from typing import Any

from google.cloud import bigquery

from app.clients import HealthResult

logger = logging.getLogger(__name__)


class BigQueryClient:
    def __init__(
        self,
        *,
        project: str,
        location: str,
        max_bytes_billed: int,
        raw_dataset: str,
        core_dataset: str,
        landing_table: str,
    ) -> None:
        if max_bytes_billed <= 0:
            raise ValueError("max_bytes_billed must be a positive int")
        self.project = project
        self.location = location
        self.max_bytes_billed = max_bytes_billed
        self.raw_dataset = raw_dataset
        self.core_dataset = core_dataset
        self.landing_table = landing_table
        # location pinned: asia-south1 is where the pilot datasets live.
        self._client = bigquery.Client(project=project, location=location)
        # Cost meter — tests / operators reset and read this around a question.
        self.bytes_scanned_total = 0
        self.query_count = 0

    def reset_cost_meter(self) -> None:
        self.bytes_scanned_total = 0
        self.query_count = 0

    def close(self) -> None:
        self._client.close()

    @property
    def landing_table_fqn(self) -> str:
        return f"`{self.project}.{self.raw_dataset}.{self.landing_table}`"

    def _job_config(self, maximum_bytes_billed: int | None) -> bigquery.QueryJobConfig:
        # Refuse to run without an explicit bytes ceiling — never rely on a
        # project-wide default that might be unset.
        limit = (
            maximum_bytes_billed
            if maximum_bytes_billed is not None
            else self.max_bytes_billed
        )
        if limit is None or limit <= 0:
            raise ValueError(
                "refusing BigQuery query without maximum_bytes_billed "
                "(sentinel_raw.incidents_v2 can be multi-million-row; "
                "unbounded scans are a real bill)"
            )
        return bigquery.QueryJobConfig(
            maximum_bytes_billed=limit,
            use_query_cache=True,
            dry_run=False,
        )

    def query(
        self,
        sql: str,
        *,
        maximum_bytes_billed: int | None = None,
        job_config: bigquery.QueryJobConfig | None = None,
        params: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        if job_config is None:
            job_config = self._job_config(maximum_bytes_billed)
        else:
            # google-cloud-bigquery's property can raise ValueError when the
            # underlying prop is missing/None — treat that as unset.
            try:
                billed = job_config.maximum_bytes_billed
            except (TypeError, ValueError):
                billed = None
            if billed is None or billed <= 0:
                raise ValueError(
                    "refusing BigQuery query: job_config.maximum_bytes_billed "
                    "must be set to a positive int"
                )
        if params:
            job_config.query_parameters = params
        job = self._client.query(sql, job_config=job_config, location=self.location)
        rows = list(job.result())
        scanned = int(getattr(job, "total_bytes_processed", 0) or 0)
        self.bytes_scanned_total += scanned
        self.query_count += 1
        return [dict(row.items()) for row in rows]

    def health_check(self) -> HealthResult:
        try:
            # Dry-run against the landing table so we verify dataset access
            # without scanning rows. Still set maximum_bytes_billed.
            sql = f"SELECT 1 AS ok FROM {self.landing_table_fqn} LIMIT 0"
            config = bigquery.QueryJobConfig(
                maximum_bytes_billed=self.max_bytes_billed,
                dry_run=True,
                use_query_cache=False,
            )
            job = self._client.query(sql, job_config=config, location=self.location)
            # dry_run populates total_bytes_processed without executing.
            scanned = getattr(job, "total_bytes_processed", None)
            return HealthResult(
                name="bigquery",
                status="ok",
                message=(
                    f"project={self.project} location={self.location} "
                    f"table={self.raw_dataset}.{self.landing_table} "
                    f"max_bytes_billed={self.max_bytes_billed} "
                    f"dry_run_bytes={scanned}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("bigquery health_check failed")
            return HealthResult(name="bigquery", status="error", message=str(exc))
