"""SigNoz Cloud query client — read-only.

API surface found (March 2026 docs — verified before writing this module):

  Endpoint (logs, traces, AND metrics share one path):
    POST {SIGNOZ_BASE_URL}/api/v5/query_range

  Auth header:
    SIGNOZ-API-KEY: <service-account API key>
    (Settings → Service Accounts → Keys in the SigNoz UI)

  Payload shape (common):
    {
      "start": <epoch_ms>,
      "end": <epoch_ms>,
      "requestType": "raw" | "scalar" | "time_series",
      "compositeQuery": {
        "queries": [{
          "type": "builder_query",
          "spec": {
            "name": "A",
            "signal": "logs" | "traces" | "metrics",
            ...
          }
        }]
      }
    }

Docs:
  https://signoz.io/docs/logs-management/logs-api/overview/
  https://signoz.io/docs/traces-management/trace-api/overview/
  https://signoz.io/docs/metrics-management/query-range-api/

This is NOT the OTLP ingest endpoint (ingest.us2.signoz.cloud). Querying
requires the product URL (e.g. https://<org>.us2.signoz.cloud) plus a
service-account API key — separate from SIGNOZ_INGESTION_KEY used by the
collector to push telemetry.

If SIGNOZ_BASE_URL / SIGNOZ_API_KEY are unset, health_check reports
unavailable and query helpers raise clearly — never invent alternate paths.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.clients import HealthResult

logger = logging.getLogger(__name__)

QUERY_PATH = "/api/v5/query_range"


class SignozClient:
    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        timeout_s: float = 30.0,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/") or None
        self._api_key = api_key or None
        self._client: httpx.Client | None = None
        if self._base_url and self._api_key:
            self._client = httpx.Client(
                base_url=self._base_url,
                headers={
                    "SIGNOZ-API-KEY": self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=timeout_s,
            )

    @property
    def configured(self) -> bool:
        return self._client is not None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def query_range(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v5/query_range — shared by logs, traces, and metrics."""
        if self._client is None:
            raise RuntimeError(
                "SigNoz client not configured: set SIGNOZ_BASE_URL and "
                "SIGNOZ_API_KEY (service-account key, not the ingestion key)"
            )
        response = self._client.post(QUERY_PATH, json=payload)
        response.raise_for_status()
        return response.json()

    def health_check(self) -> HealthResult:
        if not self._base_url or not self._api_key:
            return HealthResult(
                name="signoz",
                status="unavailable",
                message=(
                    "SIGNOZ_BASE_URL and/or SIGNOZ_API_KEY unset. "
                    "Query API is POST /api/v5/query_range with header "
                    "SIGNOZ-API-KEY (not the OTLP ingestion key)."
                ),
            )
        assert self._client is not None
        try:
            # Lightweight connectivity probe: OPTIONS/GET on the base often
            # 404s; a scalar logs query over a 1-minute window is the documented
            # surface and fails closed on bad auth.
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - 60_000
            payload = {
                "start": start_ms,
                "end": end_ms,
                "requestType": "scalar",
                "compositeQuery": {
                    "queries": [
                        {
                            "type": "builder_query",
                            "spec": {
                                "name": "A",
                                "signal": "logs",
                                "stepInterval": 60,
                                "aggregations": [
                                    {"expression": "count()", "alias": "c"}
                                ],
                                "disabled": False,
                            },
                        }
                    ]
                },
            }
            response = self._client.post(QUERY_PATH, json=payload)
            if response.status_code in (401, 403):
                msg = f"auth failed HTTP {response.status_code}: check SIGNOZ_API_KEY"
                try:
                    body = response.json()
                    err = body.get("error") or {}
                    if err.get("code") == "authz_forbidden":
                        detail = err.get("message") or ""
                        msg = (
                            "SigNoz service account lacks query permissions "
                            "(needs logs:read at minimum). In SigNoz UI: "
                            "Settings → Service Accounts → grant logs:read "
                            "(and traces:read / metrics:read for full agent tools)."
                        )
                        if detail:
                            msg = f"{msg} Detail: {detail}"
                except Exception:  # noqa: BLE001
                    pass
                return HealthResult(
                    name="signoz",
                    status="error",
                    message=msg,
                )
            if response.status_code >= 400:
                return HealthResult(
                    name="signoz",
                    status="error",
                    message=f"HTTP {response.status_code}: {response.text[:300]}",
                )
            return HealthResult(
                name="signoz",
                status="ok",
                message=f"POST {self._base_url}{QUERY_PATH} reachable",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("signoz health_check failed")
            return HealthResult(name="signoz", status="error", message=str(exc))
