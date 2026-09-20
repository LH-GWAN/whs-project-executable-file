from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from engine.engine_adapter import TrackPoint

DEFAULT_THRESHOLD_MPS2 = 3.0

MAX_GAP_SEC = 5.0


KIND_ACCEL = "accel"
KIND_DECEL = "decel"


@dataclass
class FlaggedSegment:
    start_index: int
    end_index: int
    start_time_sec: Optional[float]
    end_time_sec: Optional[float]
    # 구간에서 크기가 가장 큰 가속도. **부호를 유지한다** - 음수면 급감속이다.
    # 예전엔 abs()로 저장해 감속도 전부 "급가속"으로 표시됐다(검토 제보: REC_20240312 샘플
    # 임계값 2.0에서 -2.418/-2.212/-2.109 m/s² 감속이 급가속으로 나옴).
    max_acceleration_mps2: float
    kind: str = KIND_ACCEL

    @property
    def is_decel(self) -> bool:
        return self.kind == KIND_DECEL

    @property
    def label(self) -> str:
        return "급감속" if self.is_decel else "급가속"


def count_by_kind(segments: List["FlaggedSegment"]) -> Tuple[int, int]:
    """(급가속 개수, 급감속 개수)"""
    decel = sum(1 for s in segments if s.is_decel)
    return len(segments) - decel, decel


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
        if p.speed_kmh is None or _time_of(p) is None or p.is_outlier:
            continue
        key = _fix_key(p)
        if prev_key is None or key != prev_key:
            out.append(i)
            prev_key = key
    return out


def compute_point_accelerations(points: List[TrackPoint],
                                max_gap_sec: float = MAX_GAP_SEC) -> List[Optional[float]]:
    """행마다 '직전 실측 → 이 행' 구간의 가속도(m/s², 부호 유지). 첫 실측, 끊김(max_gap 초과)
    뒤의 첫 실측, 미기록·이상치·반복 기록 행은 None. 급가감속 판정과 그래프 툴팁이 같은
    값을 쓰도록 한 곳에 둔다."""
    out: List[Optional[float]] = [None] * len(points)
    usable = _distinct_fix_indices(points)
    for prev_i, cur_i in zip(usable, usable[1:]):
        prev_p, cur_p = points[prev_i], points[cur_i]
        dt = _time_of(cur_p) - _time_of(prev_p)
        if dt <= 0 or dt > max_gap_sec:
            continue
        out[cur_i] = ((cur_p.speed_kmh - prev_p.speed_kmh) / 3.6) / dt
    return out


def compute_flagged_segments(points: List[TrackPoint],
                              threshold_mps2: float = DEFAULT_THRESHOLD_MPS2,
                              max_gap_sec: float = MAX_GAP_SEC) -> List[FlaggedSegment]:
    n = len(points)
    flags = [False] * n
    accel_at: List[Optional[float]] = [None] * n

    usable = _distinct_fix_indices(points)
    per_point = compute_point_accelerations(points, max_gap_sec)

    for prev_i, cur_i in zip(usable, usable[1:]):
        accel = per_point[cur_i]
        if accel is None or abs(accel) < threshold_mps2:
            continue
        for i in range(prev_i, cur_i + 1):
            flags[i] = True
            if accel_at[i] is None or abs(accel) > abs(accel_at[i]):
                accel_at[i] = accel

    # 연속된 표시 행을 한 구간으로 묶되, 가속과 감속이 맞붙어 있으면(급가속 직후 급제동)
    # 부호가 바뀌는 곳에서 구간을 나눠 각각 이름을 붙인다.
    def sign_of(i: int) -> int:
        a = accel_at[i]
        if a is None:
            return 0
        return -1 if a < 0 else 1

    segments: List[FlaggedSegment] = []
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        start = i
        seg_sign = sign_of(i)
        while i < n and flags[i]:
            s_i = sign_of(i)
            if seg_sign == 0:
                seg_sign = s_i
            elif s_i != 0 and s_i != seg_sign:
                break
            i += 1
        end = i - 1
        signed = [a for a in accel_at[start:end + 1] if a is not None]
        if not signed:
            continue
        peak = max(signed, key=abs)
        segments.append(FlaggedSegment(
            start_index=start,
            end_index=end,
            start_time_sec=_time_of(points[start]),
            end_time_sec=points[end].end_time_sec or _time_of(points[end]),
            max_acceleration_mps2=peak,
            kind=KIND_DECEL if peak < 0 else KIND_ACCEL,
        ))
    return segments
