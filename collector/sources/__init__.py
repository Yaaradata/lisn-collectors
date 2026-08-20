"""Source collector registry.

Adding eKart is one import and one list entry.
"""

from __future__ import annotations

from collector.contract import SourceCollector
from collector.sources.sentinel import SentinelCollector

# name -> instance. Adding eKart is one import and one list entry.
REGISTRY: dict[str, SourceCollector] = {
    "sentinel": SentinelCollector(),
}


def get(source: str) -> SourceCollector:
    """Return the collector for `source`, or raise ValueError if unknown."""
    try:
        return REGISTRY[source]
    except KeyError as exc:
        known = ", ".join(sorted(REGISTRY)) or "(none registered)"
        raise ValueError(
            f"unknown source {source!r}; known sources: {known}"
        ) from exc
