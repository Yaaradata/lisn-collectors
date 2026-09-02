"""Read-only source clients for the diagnostic agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResult(BaseModel):
    """Uniform health payload returned by every client."""

    name: str
    status: Literal["ok", "error", "unavailable"]
    message: str


__all__ = ["HealthResult"]
