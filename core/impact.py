from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

DEFAULT_IMPACT_THRESHOLD_G = 3.0 #현재 합력과 직전 2초 평균의 절대 차이가 3.0g 이상일 때 감지, 추후 수정 가능성 있음
BASELINE_WINDOW_SEC = 2.0
MIN_BASELINE_SEC = 1.0
MAX_SAMPLE_GAP_SEC = 1.0


@dataclass(frozen=True)
class ImpactEvent:
    index: int
    time_sec: float
    baseline_g: float
    current_g: float
    deviation_g: float
    threshold_g: float
    baseline_samples: int
    baseline_span_sec: float
    segment_index: int = 0


def find_first_impact(points: Iterable, threshold_g: float = DEFAULT_IMPACT_THRESHOLD_G
                      ) -> Optional[ImpactEvent]:
    """[t-2초, t)의 유효 합력 산술평균과 현재 값의 절대 차이를 비교한다.

    현재 값은 평균에서 제외한다. 최소 1초의 관측 기간과 2개 이전 샘플이
    필요하다. 1초 초과 공백, 손상 행, 역행/중복 시각, 영상 경계에서 초기화한다.
    중력 제거 기기의 0g는 유효하다. GPS 유무와 독립적이다.
    """
    if (isinstance(threshold_g, bool) or not isinstance(threshold_g, (int, float))
            or not math.isfinite(threshold_g) or threshold_g <= 0):
        raise ValueError("threshold_g must be finite and positive")
    history = deque()
    previous_time = None
    previous_segment = None
    for index, point in enumerate(points):
        t = getattr(point, "start_time_sec", None)
        g = getattr(point, "g_magnitude", None)
        segment = getattr(point, "segment_index", 0)
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or v < 0 for v in (t, g)):
            history.clear()
            previous_time = None
            continue
        if previous_time is not None:
            if segment != previous_segment or t - previous_time > MAX_SAMPLE_GAP_SEC:
                history.clear()
            elif t <= previous_time:
                history.clear()
                previous_time = None
                continue
        previous_time, previous_segment = t, segment
        while history and history[0][0] < t - BASELINE_WINDOW_SEC:
            history.popleft()
        if len(history) >= 2 and t - history[0][0] >= MIN_BASELINE_SEC:
            # Divide before summing so finite large input cannot overflow the mean.
            baseline = math.fsum(value / len(history) for _, value in history)
            deviation = abs(g - baseline)
            if deviation >= threshold_g:
                return ImpactEvent(index, t, baseline, g, deviation, threshold_g,
                                   len(history), t - history[0][0], segment)
        history.append((t, g))
    return None
