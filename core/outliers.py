"""GPS 이상치 판정.

블랙박스 GPS는 가끔 터널 출구·고층 건물 사이에서 좌표가 수 km 튀거나 속도가 말이 안 되는
값으로 찍힌다. 그대로 두면 지도 궤적이 엉뚱한 곳으로 선을 긋고, 그 지점 앞뒤로 가짜
급가속이 잡힌다. 여기서 골라낸 지점은 `TrackPoint.is_outlier`로 표시되어 화면·지도·그래프·
급가감속 판정에서 빠지고 표에는 "(이상치)"로 남는다. 원본 값은 지우지 않는다(툴팁·CSV에
그대로 있다) - 판정은 앱의 해석이지 증거를 고치는 것이 아니다.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from engine.engine_adapter import TrackPoint

MAX_SPEED_KMH = 300.0        # 이 위는 차량 속도로 볼 수 없다
MAX_JUMP_MPS = 100.0         # 두 측정 사이 이동 속도가 360 km/h를 넘으면 위치가 튄 것
MIN_JUMP_M = 150.0           # 아주 짧은 dt로 나눠 커진 값이 아니라 실제로 멀리 튄 경우만
_EARTH_R = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def _coords_valid(p: TrackPoint) -> bool:
    if p.latitude is None or p.longitude is None:
        return False
    if not math.isfinite(p.latitude) or not math.isfinite(p.longitude):
        return False
    if abs(p.latitude) > 90 or abs(p.longitude) > 180:
        return False
    return True


def _fix_key(p: TrackPoint):
    utc = (p.gps_utc_time or "").strip()
    if utc:
        return ("utc", p.gps_date or "", utc, p.latitude, p.longitude)
    return ("val", p.latitude, p.longitude, p.speed_kmh)


class _Group:
    """같은 GPS 측정값이 반복 기록된 행 묶음(초당 수십 행 쓰는 기기 대응)."""
    __slots__ = ("indices", "t", "lat", "lon")

    def __init__(self, index: int, t: float, lat: float, lon: float):
        self.indices = [index]
        self.t = t
        self.lat = lat
        self.lon = lon


def _groups(points: List[TrackPoint]) -> List[_Group]:
    groups: List[_Group] = []
    prev_key = None
    for i, p in enumerate(points):
        if not p.has_fix or p.start_time_sec is None or not math.isfinite(p.start_time_sec):
            continue
        key = _fix_key(p)
        if groups and key == prev_key:
            groups[-1].indices.append(i)
            continue
        groups.append(_Group(i, p.start_time_sec, p.latitude, p.longitude))
        prev_key = key
    return groups


def _speed_between(a: _Group, b: _Group) -> Optional[Tuple[float, float]]:
    """(이동 속도 m/s, 거리 m). 시간이 같거나 역행이면 None."""
    dt = b.t - a.t
    if dt <= 0:
        return None
    dist = haversine_m(a.lat, a.lon, b.lat, b.lon)
    return dist / dt, dist


def _mark(points: List[TrackPoint], indices: List[int], reason: str) -> None:
    for i in indices:
        points[i].is_outlier = True
        points[i].outlier_reason = reason


def mark_outliers(points: List[TrackPoint]) -> int:
    """이상치를 표시하고 개수를 돌려준다. 다시 부르면 이전 표시를 지우고 새로 판정한다."""
    for p in points:
        p.is_outlier = False
        p.outlier_reason = ""

    # 1) 값 자체가 불가능한 행
    for i, p in enumerate(points):
        if p.latitude is not None and p.longitude is not None and not _coords_valid(p):
            _mark(points, [i], f"좌표가 유효 범위 밖 ({p.latitude}, {p.longitude})")
        elif p.speed_kmh is not None and (not math.isfinite(p.speed_kmh) or p.speed_kmh > MAX_SPEED_KMH or p.speed_kmh < 0):
            _mark(points, [i], f"속도 비정상 ({p.speed_kmh:.0f} km/h)")

    # 2) 위치가 튄 측정: 직전 정상 측정에서 불가능한 속도로 멀어졌다가(그리고 다음 측정이
    #    다시 원래 자리 근처면) 그 한 점만 이상치로 본다. 다음 측정도 멀리 있으면 실제로
    #    이동한 것일 수 있으므로 건드리지 않는다 - 단, 마지막 점은 되돌아올 다음 점이 없어
    #    튄 것으로 본다.
    groups = _groups(points)
    last_ok: Optional[_Group] = None
    for gi, g in enumerate(groups):
        if last_ok is None:
            # 첫 점이 튄 경우: 두 번째로 가는 속도는 불가능한데 두 번째→세 번째는 정상이면
            if gi + 2 < len(groups):
                s01 = _speed_between(g, groups[gi + 1])
                s12 = _speed_between(groups[gi + 1], groups[gi + 2])
                if (s01 and s01[0] > MAX_JUMP_MPS and s01[1] > MIN_JUMP_M
                        and s12 and s12[1] < 0.5 * s01[1]):
                    _mark(points, g.indices, f"위치 급변 (다음 측정까지 {s01[1] / 1000:.1f} km, {s01[0] * 3.6:.0f} km/h 상당)")
                    continue
            last_ok = g
            continue

        s_in = _speed_between(last_ok, g)
        if s_in is None or s_in[0] <= MAX_JUMP_MPS or s_in[1] <= MIN_JUMP_M:
            last_ok = g
            continue
        # 여기까지 왔으면 last_ok → g 가 불가능한 이동이다.
        nxt = groups[gi + 1] if gi + 1 < len(groups) else None
        if nxt is None:
            _mark(points, g.indices, f"위치 급변 ({s_in[1] / 1000:.1f} km를 {g.t - last_ok.t:.0f}초 만에 이동)")
            continue
        # 다음 측정이 튄 지점보다 원래 자리(last_ok)에 훨씬 가까우면 한 점만 튄 것이다.
        # 다음 측정도 튄 지점 근처에 있으면 실제 이동(긴 끊김 뒤 재수신 등)으로 본다.
        dist_back = haversine_m(last_ok.lat, last_ok.lon, nxt.lat, nxt.lon)
        if dist_back < 0.5 * s_in[1]:
            _mark(points, g.indices, f"위치 급변 ({s_in[1] / 1000:.1f} km를 {g.t - last_ok.t:.0f}초 만에 이동 후 복귀)")
            continue
        last_ok = g

    return sum(1 for p in points if p.is_outlier)
