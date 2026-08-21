"""Shared HTTP client for collector → source calls.

PRODUCTION path (known future task, not a gap for this sprint): real Flipkart
systems live on RFC1918 addresses (Sentinel at 10.24.1.91, Multi Track at
10.24.2.16). Those are unreachable from default Cloud Run egress regardless of
auth. Pointing at real systems will require Direct VPC egress or a connector.

This sprint's mock path is different: mock-sentinel is a Cloud Run service with
ingress=all and authentication required. The worker presents a Google ID token
(audience = root service URL) and holds roles/run.invoker.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse, urlunparse

import httpx

# audience -> (token, exp_unix)
_token_cache: dict[str, tuple[str, float]] = {}

# Refresh a bit before exp so in-flight requests never see an expired token.
_REFRESH_SKEW_S = 300.0


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _on_cloud_run() -> bool:
    return "CLOUD_RUN_TASK_INDEX" in os.environ or "K_SERVICE" in os.environ


def _should_use_id_token() -> bool:
    return _truthy(os.environ.get("USE_ID_TOKEN")) and _on_cloud_run()


def _root_audience(target_url: str) -> str:
    # The audience must be the ROOT service URL, not the full path.
    parsed = urlparse(target_url)
    if not parsed.scheme or not parsed.netloc:
        return target_url.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def _jwt_exp(token: str) -> float:
    """Read exp from an unsigned JWT payload (metadata tokens are opaque to us)."""
    try:
        payload_b64 = token.split(".")[1]
        pad = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
        return float(payload["exp"])
    except (IndexError, KeyError, ValueError, json.JSONDecodeError):
        # If we cannot parse exp, treat as short-lived so the next call refreshes.
        return time.time() + 60.0


def _fetch_id_token(audience: str) -> str:
    meta = (
        "http://metadata.google.internal/computeMetadata/v1/"
        "instance/service-accounts/default/identity"
        f"?audience={urllib.parse.quote(audience, safe='')}"
    )
    req = urllib.request.Request(meta, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode("utf-8").strip()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"failed to fetch ID token for audience={audience!r} from metadata server"
        ) from exc


def _get_id_token(audience: str) -> str:
    now = time.time()
    cached = _token_cache.get(audience)
    if cached is not None:
        token, exp = cached
        if exp - _REFRESH_SKEW_S > now:
            return token
    token = _fetch_id_token(audience)
    _token_cache[audience] = (token, _jwt_exp(token))
    return token


def get_client(target_url: str) -> httpx.Client:
    """Return an httpx client, with a Bearer ID token when running on Cloud Run.

    Shared rather than per-source: every source that talks to a Cloud Run
    service needs the same behaviour.
    """
    if _should_use_id_token():
        audience = _root_audience(target_url)
        token = _get_id_token(audience)
        return httpx.Client(headers={"Authorization": f"Bearer {token}"})
    return httpx.Client()
