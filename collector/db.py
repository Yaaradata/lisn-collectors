"""psycopg helpers for collector state tables."""

from __future__ import annotations

import os

import psycopg


def connect() -> psycopg.Connection:
    """Return a psycopg connection to COLLECTOR_DSN.

    Procrastinate manages its own connections separately; this helper is only
    for collector_request / collector_job / raw_manifest.
    """
    return psycopg.connect(os.environ["COLLECTOR_DSN"])
