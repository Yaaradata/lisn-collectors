"""Cloud Run Jobs execution history — read-only.

This is the only source that knows a task was terminated at the 24-hour
task-timeout ceiling (col-sentinel / discovery / maintenance use
--task-timeout=86400s). Status and termination condition come from the
Executions API; this client never creates, updates, or cancels executions.
"""

from __future__ import annotations

import logging
from typing import Any

from google.cloud import run_v2

from app.clients import HealthResult

logger = logging.getLogger(__name__)


class GcpRunClient:
    def __init__(
        self,
        *,
        project: str,
        region: str,
        job_names: tuple[str, ...],
    ) -> None:
        self.project = project
        self.region = region
        self.job_names = job_names
        # ExecutionsClient lists history; JobsClient is only used for a light
        # health probe (get), never for create/update/run.
        self._executions = run_v2.ExecutionsClient()
        self._jobs = run_v2.JobsClient()

    def close(self) -> None:
        # GAPIC clients hold gRPC channels; close() is best-effort.
        for client in (self._executions, self._jobs):
            transport = getattr(client, "transport", None)
            if transport is not None and hasattr(transport, "close"):
                transport.close()

    def _job_parent(self, job_name: str) -> str:
        return (
            f"projects/{self.project}/locations/{self.region}/jobs/{job_name}"
        )

    def list_executions(
        self, job_name: str, *, page_size: int = 20
    ) -> list[dict[str, Any]]:
        """List recent executions for one Cloud Run Job (newest first)."""
        if job_name not in self.job_names:
            raise ValueError(
                f"unknown job {job_name!r}; allowed={list(self.job_names)}"
            )
        request = run_v2.ListExecutionsRequest(
            parent=self._job_parent(job_name),
            page_size=page_size,
        )
        out: list[dict[str, Any]] = []
        for execution in self._executions.list_executions(request=request):
            out.append(_execution_summary(execution))
            if len(out) >= page_size:
                break
        return out

    def health_check(self) -> HealthResult:
        try:
            # Probe the first known worker job — get is read-only.
            job_name = self.job_names[0]
            job = self._jobs.get_job(name=self._job_parent(job_name))
            # Also list one execution page to confirm executions.list IAM.
            executions = self.list_executions(job_name, page_size=1)
            latest = executions[0]["name"] if executions else "(none)"
            return HealthResult(
                name="gcp",
                status="ok",
                message=(
                    f"job={job.name} uid={job.uid} "
                    f"latest_execution={latest}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("gcp health_check failed")
            return HealthResult(name="gcp", status="error", message=str(exc))


def _execution_summary(execution: Any) -> dict[str, Any]:
    """Flatten the fields ops care about: status + why it stopped."""
    condition_msgs: list[str] = []
    for cond in getattr(execution, "conditions", []) or []:
        # type/state/message — Completed with reason DeadlineExceeded = 24h kill
        condition_msgs.append(
            f"{getattr(cond, 'type_', getattr(cond, 'type', ''))}="
            f"{getattr(cond, 'state', '')}:"
            f"{getattr(cond, 'message', '') or getattr(cond, 'reason', '')}"
        )
    return {
        "name": execution.name,
        "uid": execution.uid,
        "create_time": _ts(execution.create_time),
        "completion_time": _ts(getattr(execution, "completion_time", None)),
        "observed_generation": getattr(execution, "observed_generation", None),
        "succeeded_count": getattr(execution, "succeeded_count", None),
        "failed_count": getattr(execution, "failed_count", None),
        "cancelled_count": getattr(execution, "cancelled_count", None),
        "retried_count": getattr(execution, "retried_count", None),
        "task_count": getattr(execution, "task_count", None),
        "conditions": condition_msgs,
        "reconciling": getattr(execution, "reconciling", None),
    }


def _ts(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
