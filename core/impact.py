"""Report-only comparison of adjacent G-sensor magnitudes (not impulse)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from engine.engine_adapter import TrackPoint

IMPACT_RATIO = 2.0
MAX_REPORT_EVENTS = 1


@dataclass(frozen=True)
class ImpactEvent:
    time_sec: float
    magnitude_g: float
    rise_g: float

    @property
    def previous_g(self) -> float:
        return self.magnitude_g - self.rise_g


def detect_impacts(points: Iterable[TrackPoint]) -> list[ImpactEvent]:
    """Compare each sample to its immediate predecessor, with no 1-second rule.

    Missing/non-finite values break comparison. Zero cannot be a ratio baseline.
    GPS outliers do not invalidate independent G-sensor measurements.
    """
    events = []
    previous = None
    for point in points:
        t, g = point.start_time_sec, point.g_magnitude
        if t is None or g is None or not math.isfinite(t) or not math.isfinite(g) or t < 0:
            previous = None
            continue
        if previous is not None:
            previous_t, previous_g = previous
            if t > previous_t and previous_g > 0 and g >= IMPACT_RATIO * previous_g:
                events.append(ImpactEvent(t, g, g - previous_g))
        previous = (t, g)
    return events


def select_report_impacts(events: list[ImpactEvent]) -> list[ImpactEvent]:
    """Capture only the first qualifying time in the entire report."""
    return sorted(events, key=lambda e: e.time_sec)[:MAX_REPORT_EVENTS]
