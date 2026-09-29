"""G-sensor 합력의 직전 유효 샘플 대비 증가를 탐지한다.

이 값은 충돌 확정이나 물리적 충격량(impulse, N·s)이 아니다.
일반 보고서 임계값은 2.0배이다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

DEFAULT_IMPACT_THRESHOLD = 2.0


@dataclass(frozen=True)
class ImpactEvent:
    index: int
    time_sec: float
    previous_g: float
    current_g: float
    ratio: float
    threshold: float


def find_first_impact(points: Iterable, threshold: float = DEFAULT_IMPACT_THRESHOLD
                      ) -> Optional[ImpactEvent]:
    """연속된 두 유효·시각 증가 센서행의 합력 비율에서 최초 기준 충족 행을 찾는다.

    0/음수/NaN/Inf/시각 누락, 비단조 시각은 비교 사슬을 끊어 손상 행을
    가로질러 허위 급증을 만들지 않는다. GPS 유무와 독립적이다.
    """
    if not math.isfinite(threshold) or threshold <= 1.0:
        raise ValueError("threshold must be finite and greater than 1.0")
    previous = None
    for index, point in enumerate(points):
        timestamp = getattr(point, "start_time_sec", None)
        magnitude = getattr(point, "g_magnitude", None)
        if (not isinstance(timestamp, (float, int))
                or not isinstance(magnitude, (float, int))
                or not math.isfinite(timestamp) or timestamp < 0
                or not math.isfinite(magnitude) or magnitude <= 0):
            previous = None
            continue
        t, g = float(timestamp), float(magnitude)
        if previous is not None:
            prev_time, prev_g = previous
            if t <= prev_time:
                previous = None
                continue
            ratio = g / prev_g
            if ratio >= threshold:
                return ImpactEvent(index, t, prev_g, g, ratio, threshold)
        previous = (t, g)
    return None
