"""유효 GPS 측정 고르기와 초당 속도 변화.

위험운전 판정(core/driving_events.py), 속도 그래프 말풍선, 속도 통계가 모두 여기서 고른
같은 측정을 쓴다. 판정 기준(국토부 DTG 기준)이 "초당 몇 km/h"라서 속도 변화는 **1초 창**으로
잰다 - 이 행에서 1초 이상 앞선 가장 가까운 실측과 비교한다. 1Hz 기기는 바로 앞 측정이고,
초당 2회 기록하는 기기(FineVu 등)도 0.5초 차이로 두 배 부풀려지지 않는다.

recovery_state 계약(리뷰 2026-10-11 #4): `gap_carved`는 손상 구간에서 회수한 **실측**이라 판정에 넣는다.
`damaged`(빈 행)·`interpolated`(추정 좌표)만 합성 행이라 어디서도 측정처럼 쓰지 않는다 - is_synthetic().
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from engine.engine_adapter import TrackPoint

MAX_GAP_SEC = 5.0
RATE_WINDOW_SEC = 1.0
# 기기 시계가 1초보다 조금 짧게 도는 경우(0.986초 간격 등)도 1초 창으로 본다.
RATE_WINDOW_TOLERANCE_SEC = 0.15
# 판정에 쓰는 창의 상한. 기준은 '초당' 변화라 1초 안팎의 창만 판정한다 - 5초 창의 평균(47→5)을 초당
# 값으로 보면 1초 자료에는 없는 급정지가 생겼다(리뷰 #44). 더 긴 간격은 말풍선에도 '초당' 값으로 보이지
# 않는다(리뷰 #52) - compute_point_rate_windows()가 간격을 같이 주니 화면은 '간격 평균, 판정 제외'로 적는다.
EVENT_MAX_WINDOW_SEC = 1.5

# 손상 처리로 만든 합성 행의 recovery_state. gap_carved는 여기 없다(회수한 실측).
SYNTHETIC_STATES = ("damaged", "interpolated")


def is_synthetic(point: TrackPoint) -> bool:
    """손상 처리로 만든 행(빈 행·임의 복구 추정 좌표)인가. 측정이 아니므로 속도·판정·이상치·집계에서 뺀다."""
    flag = getattr(point, "is_synthetic", None)   # engine_adapter.TrackPoint.is_synthetic(같은 계약)이 있으면 그것
    return bool(flag) if isinstance(flag, bool) else point.recovery_state in SYNTHETIC_STATES


# 다른 모듈이 같은 이름으로 쓰던 헬퍼 - 조정자가 TrackPoint.is_synthetic으로 통일할 때 같이 치운다.
_is_synthetic = is_synthetic


def _time_of(point: TrackPoint) -> Optional[float]:
    return point.start_time_sec


def _fix_key(point: TrackPoint):
    return point.record_key()


def _distinct_fix_indices(points: List[TrackPoint]) -> List[int]:
    out: List[int] = []
    prev_key = None
    for i, p in enumerate(points):
        if (is_synthetic(p) or not p.has_fix or p.speed_kmh is None or not math.isfinite(p.speed_kmh)
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
        if points[prev_i].segment_index != points[cur_i].segment_index:
            continue  # 이어보기 영상 경계는 잇지 않는다(가상의 간격, 리뷰 #45)
        if any(is_synthetic(points[m]) for m in range(prev_i + 1, cur_i)):
            continue  # 손상 구간(빈 행·임의 복구 행)을 가로지르는 변화는 판정하지 않는다
        out.append((prev_i, cur_i, dt, (points[cur_i].speed_kmh - points[prev_i].speed_kmh) / dt))
    return out


def judged_pair_count(points: List[TrackPoint]) -> int:
    """위험운전 판정에 실제로 쓰인 속도 변화 표본 수(1.5초 이하 창). 0이면 '위험운전 없음'이 아니라
    '판정 불가'다(리뷰 #4)."""
    return sum(1 for _p, _c, dt, _r in speed_rate_pairs(points) if dt <= EVENT_MAX_WINDOW_SEC)


def compute_point_rate_windows(points: List[TrackPoint],
                               max_gap_sec: float = MAX_GAP_SEC) -> List[Optional[Tuple[float, float]]]:
    """행마다 (가속도 m/s², 비교 창 초). 비교할 앞 측정이 없거나 미기록·이상치·반복 기록·합성 행은 None.
    창이 EVENT_MAX_WINDOW_SEC를 넘으면 판정에 쓰이지 않은 '간격 평균'이다 - 화면은 그렇게 적어야 한다."""
    out: List[Optional[Tuple[float, float]]] = [None] * len(points)
    for _prev_i, cur_i, dt, rate_kmh in speed_rate_pairs(points, max_gap_sec):
        out[cur_i] = (rate_kmh / 3.6, dt)
    return out


def compute_point_accelerations(points: List[TrackPoint],
                                max_gap_sec: float = MAX_GAP_SEC,
                                max_window_sec: float = EVENT_MAX_WINDOW_SEC) -> List[Optional[float]]:
    """행마다 '1초 앞 실측 → 이 행'의 가속도(m/s², 부호 유지). 비교할 앞 측정이 없거나
    미기록·이상치·반복 기록 행, 그리고 창이 max_window_sec보다 긴 행(판정에 쓰지 않는 간격 평균)은
    None. 위험운전 판정과 그래프 말풍선이 같은 값을 쓴다(리뷰 #44·#52)."""
    return [None if w is None or w[1] > max_window_sec else w[0]
            for w in compute_point_rate_windows(points, max_gap_sec)]
