"""Acceptance dataset fixtures (DS-1 .. DS-15).

Each fixture is re-seedable and exposes:
- build(): reseed sentinel_mock deterministically
- truth(): expected record set via direct SQL against sentinel_mock
"""

from .catalog import DATASETS, DatasetFixture

__all__ = ["DATASETS", "DatasetFixture"]
