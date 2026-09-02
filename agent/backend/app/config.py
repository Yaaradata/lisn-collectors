"""Settings for the diagnostic agent. Every value comes from the environment."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_ROOT.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Repo root .env holds shared secrets (SIGNOZ_*, DSNs); agent/backend/.env
        # overrides when present. OS env always wins over files.
        env_file=(
            str(_REPO_ROOT / ".env"),
            str(_BACKEND_ROOT / ".env"),
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    agent_port: int = Field(default=8090, alias="AGENT_PORT")

    # Prefer a SELECT-only Postgres role. See README open item if one does not
    # yet exist — do not silently assume the application superuser is fine.
    collector_dsn_readonly: str = Field(alias="COLLECTOR_DSN_READONLY")
    sentinel_mock_dsn_readonly: str = Field(alias="SENTINEL_MOCK_DSN_READONLY")

    # Write-capable DSN for agent_session / agent_message only. Falls back to
    # COLLECTOR_DSN / COLLECTOR_DSN_READONLY when unset (pilot stopgap).
    agent_dsn: str | None = Field(default=None, alias="AGENT_DSN")

    gcp_project: str = Field(default="clariversev1", alias="GCP_PROJECT")
    gcp_region: str = Field(default="asia-south1", alias="GCP_REGION")

    bq_raw_dataset: str = Field(default="sentinel_raw", alias="BQ_RAW_DATASET")
    bq_core_dataset: str = Field(default="sentinel_core", alias="BQ_CORE_DATASET")
    bq_landing_table: str = Field(default="incidents_v2", alias="BQ_LANDING_TABLE")
    # sentinel_raw.incidents_v2 held ~3.2M rows before the last reset; BQ bills
    # by bytes scanned. Cap every query so a vague question cannot run away.
    bq_max_bytes_billed: int = Field(default=1_000_000_000, alias="BQ_MAX_BYTES_BILLED")

    signoz_base_url: str | None = Field(default=None, alias="SIGNOZ_BASE_URL")
    signoz_api_key: str | None = Field(default=None, alias="SIGNOZ_API_KEY")

    # -------------------------------------------------------------------------
    # MODEL PROVIDER — DATA GOVERNANCE decision, not a technical preference.
    # Vertex keeps prompts/results inside clariversev1. Anthropic sends them to
    # a third party. Collector payloads include Flipkart incident ids, order ids
    # and agent names. Confirm with the customer before anything but local
    # testing uses a non-Vertex provider.
    # Default "vertex" is the safer choice — a default, not a decision.
    # -------------------------------------------------------------------------
    model_provider: Literal["vertex", "anthropic"] = Field(
        default="vertex", alias="MODEL_PROVIDER"
    )
    vertex_model: str = Field(
        default="gemini-2.5-flash", alias="VERTEX_MODEL"
    )
    # Gemini publisher models are often unavailable in asia-south1; keep BQ /
    # Cloud Run on GCP_REGION and point the chat model at a Vertex region that
    # actually hosts the chosen model (commonly us-central1).
    vertex_location: str = Field(default="us-central1", alias="VERTEX_LOCATION")
    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514", alias="ANTHROPIC_MODEL"
    )
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # Cloud Run jobs whose executions this agent may inspect (read-only).
    cloud_run_jobs: tuple[str, ...] = (
        "col-sentinel",
        "col-sentinel-discovery",
        "col-maintenance",
    )

    def resolve_agent_dsn(self) -> str:
        if self.agent_dsn:
            return self.agent_dsn
        # Pilot stopgap: reuse the collector DSN for agent_* writes.
        return os.environ.get("COLLECTOR_DSN") or self.collector_dsn_readonly


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
