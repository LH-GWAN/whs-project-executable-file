"""유효 GPS 측정 고르기와 초당 속도 변화.

위험운전 판정(core/driving_events.py), 속도 그래프 말풍선, 속도 통계가 모두 여기서 고른
같은 측정을 쓴다. 판정 기준(국토부 DTG 기준)이 "초당 몇 km/h"라서 속도 변화는 **1초 창**으로
잰다 - 이 행에서 1초 이상 앞선 가장 가까운 실측과 비교한다. 1Hz 기기는 바로 앞 측정이고,
초당 2회 기록하는 기기(FineVu 등)도 0.5초 차이로 두 배 부풀려지지 않는다.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from engine.engine_adapter import TrackPoint

MAX_GAP_SEC = 5.0
RATE_WINDOW_SEC = 1.0
# 기기 시계가 1초보다 조금 짧게 도는 경우(0.986초 간격 등)도 1초 창으로 본다.
RATE_WINDOW_TOLERANCE_SEC = 0.15


def _time_of(point: TrackPoint) -> Optional[float]:
    return point.start_time_sec


def _fix_key(point: TrackPoint):
    utc = (point.gps_utc_time or "").strip()
    if utc:
        return ("utc", point.gps_date or "", utc)
    return ("val", point.latitude, point.longitude, point.speed_kmh)


def _distinct_fix_indices(points: List[TrackPoint]) -> List[int]:
    out: List[int] = []
    prev_key = None
    for i, p in enumerate(points):
        if (not p.has_fix or p.speed_kmh is None or not math.isfinite(p.speed_kmh)
                or p.speed_kmh < 0 or _time_of(p) is None
                or not math.isfinite(_time_of(p)) or _time_of(p) < 0):
            continue
        key = _fix_key(p)
        if prev_key is None or key != prev_key:
            out.append(i)
            prev_key = key
    return out


def speed_rate_pairs(points: List[TrackPoint],
                     max_gap_sec: float = MAX_GAP_SEC) -> List[Tuple[int, int, float, float]]:
    """(앞 행, 이 행, 간격 초, 초당 속도 변화 km/h - 부호 유지) 목록. 앞 행은 이 행보다 1초
    이상 앞선 가장 가까운 유효 측정이다. 첫 측정, 끊김(max_gap 초과) 뒤의 첫 측정은 빠진다."""
    usable = _distinct_fix_indices(points)
    times = [_time_of(points[i]) for i in usable]
    min_window = RATE_WINDOW_SEC - RATE_WINDOW_TOLERANCE_SEC
    out: List[Tuple[int, int, float, float]] = []
    for k in range(1, len(usable)):
        j = k - 1
        while j >= 0 and 0 <= times[k] - times[j] < min_window:
            j -= 1
        if j < 0:
            continue
        dt = times[k] - times[j]
        if dt < min_window or dt > max_gap_sec:
            continue  # 시간이 거꾸로 가거나 너무 멀다
        prev_i, cur_i = usable[j], usable[k]
        out.append((prev_i, cur_i, dt, (points[cur_i].speed_kmh - points[prev_i].speed_kmh) / dt))
    return out


def compute_point_accelerations(points: List[TrackPoint],
                                max_gap_sec: float = MAX_GAP_SEC) -> List[Optional[float]]:
    """행마다 '1초 앞 실측 → 이 행'의 가속도(m/s², 부호 유지). 비교할 앞 측정이 없거나
    미기록·이상치·반복 기록 행은 None. 위험운전 판정과 그래프 말풍선이 같은 값을 쓴다."""
    out: List[Optional[float]] = [None] * len(points)
    for _prev_i, cur_i, _dt, rate_kmh in speed_rate_pairs(points, max_gap_sec):
        out[cur_i] = rate_kmh / 3.6
    return out
