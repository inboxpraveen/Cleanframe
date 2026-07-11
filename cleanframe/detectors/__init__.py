"""Detector registry and the built-in detector suite.

Importing this package registers every built-in detector (importing the modules
runs their ``@detector`` decorators). Third-party detectors register themselves the
same way — by being imported — so ``import cleanframe`` wires up the built-ins and
users add their own with ``@cf.detector(...)``.
"""

from __future__ import annotations

# Importing each module triggers its @detector registrations. Ordered by the
# priority they run at, for readability only.
from . import (
    categories,  # noqa: E402,F401       priority 50
    contacts,  # noqa: E402,F401         priority 45
    currency,  # noqa: E402,F401         priority 45
    dates,  # noqa: E402,F401            priority 40
    dedup,  # noqa: E402,F401            priority 80
    nulls,  # noqa: E402,F401            priority 20
    outliers,  # noqa: E402,F401         priority 70
    schema_mapping,  # noqa: E402,F401  priority 5
    text,  # noqa: E402,F401             priority 10, 60
    units,  # noqa: E402,F401            priority 46
)
from .base import (
    DETECTOR_REGISTRY,
    DetectorContext,
    DetectorSpec,
    detector,
    list_detectors,
    run_detectors,
    unregister_detector,
)

__all__ = [
    "DETECTOR_REGISTRY",
    "DetectorContext",
    "DetectorSpec",
    "detector",
    "list_detectors",
    "run_detectors",
    "unregister_detector",
]
