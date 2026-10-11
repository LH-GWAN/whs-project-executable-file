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

from core.acceleration import RATE_WINDOW_SEC, RATE_WINDOW_TOLERANCE_SEC, is_synthetic
from engine.engine_adapter import TrackPoint

MAX_SPEED_KMH = 300.0        # 이 위는 차량 속도로 볼 수 없다
MAX_JUMP_MPS = 100.0         # 두 측정 사이 이동 속도가 360 km/h를 넘으면 위치가 튄 것
MIN_JUMP_M = 150.0           # 아주 짧은 dt로 나눠 커진 값이 아니라 실제로 멀리 튄 경우만
MAX_SPEED_JUMP_KMH_S = 60.0  # 1초에 이만큼 넘게 속도가 튀었다가 되돌아오면 측정 오류(리뷰 #48)
# 차량이 낼 수 없는 속도 변화율(km/h/s). 1 g = 35.3 km/h/s - 마른 노면에서 ABS 제동이 약 1.0 g, 양산차 가속은
# 그 아래이고, 판정 기준표의 최대값은 초당 15 km/h다(core/driving_events.CRITERIA). 40은 1.13 g(11.1 m/s²)로
# 그 위는 측정 오류거나 충돌 같은 외력이다. 되돌아오는 튐은 이상치로, 가속 쪽 상한 초과는 '검증 필요'로
# 분리해 가짜 급가속·급출발이 되지 않게 한다. 감속 쪽은 되돌아오지 않으면(충돌 뒤 정지) 그대로 둔다 -
# 측정 오류라는 근거가 없고 증거로 봐야 한다(리뷰 2026-10-11 #76).
MAX_PHYSICAL_RATE_KMH_S = 40.0
# 상한을 넘은 뒤 이 시간 안에 앞 속도와 맞는 값으로 돌아오면 그 사이는 튄 것이다(측정 공백 상한과 같다).
RETURN_WINDOW_SEC = 5.0
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
    if abs(p.latitude) < 1e-6 and abs(p.longitude) < 1e-6:
        return False  # (0, 0) - 수신 실패를 0으로 채운 기기. PR #2에서 빠졌던 것을 복원
    return True


def _fix_key(p: TrackPoint):
    """같은 측정이 반복 기록된 행을 묶는 열쇠. TrackPoint.record_key()(UTC > 기기 절대 시각·경과 초 > 값)에
    좌표를 붙인다. 값으로만 묶으면 UTC 없는 기기(FineVu)의 정차 구간 전체가 한 묶음이 되어 dt가 정차
    길이만큼 부풀고 정차 중 튄 점을 못 잡는다(리뷰 #7, 2026-10-11 #22)."""
    return p.record_key() + (p.latitude, p.longitude)


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


# 속도 변화율은 1초 창으로 잰다(core/acceleration과 같은 창). 기기 시각이 1초 단위라 같은 초 안에서 값이 바뀐 행
# (FineVu는 시각이 먼저 바뀌고 다음 행에서 값이 바뀐다)을 0.06초 간격으로 나누면 4 km/h 차이도 초당 70이 된다.
_MIN_WINDOW_SEC = RATE_WINDOW_SEC - RATE_WINDOW_TOLERANCE_SEC


def _ref_before(groups: List[_Group], gi: int) -> Optional[int]:
    """gi보다 1초 창 이상 앞선 가장 가까운 묶음 번호."""
    j = gi - 1
    while j >= 0 and groups[gi].t - groups[j].t < _MIN_WINDOW_SEC:
        j -= 1
    return j if j >= 0 else None


def _ref_after(groups: List[_Group], gi: int) -> Optional[int]:
    j = gi + 1
    while j < len(groups) and groups[j].t - groups[gi].t < _MIN_WINDOW_SEC:
        j += 1
    return j if j < len(groups) else None


def _rate_ok(v_from: float, t_from: float, v_to: float, t_to: float) -> bool:
    dt = t_to - t_from
    return dt > 0 and abs(v_to - v_from) / dt <= MAX_PHYSICAL_RATE_KMH_S


def _mark_impossible_rates(points: List[TrackPoint]) -> None:
    groups = [g for g in _groups(points)
              if points[g.indices[0]].speed_kmh is not None and not points[g.indices[0]].is_outlier]
    speed = lambda g: points[g.indices[0]].speed_kmh  # noqa: E731
    trusted: List[_Group] = []   # 신뢰한 측정(시간순). 비교 상대는 이 중 1초 창 이상 앞선 마지막 것.
    gi = 0
    while gi < len(groups):
        g = groups[gi]
        last_ok = next((r for r in reversed(trusted) if g.t - r.t >= _MIN_WINDOW_SEC), None)
        if last_ok is None or _rate_ok(speed(last_ok), last_ok.t, speed(g), g.t):
            trusted.append(g)
            gi += 1
            continue
        # 변화가 불가능하다. RETURN_WINDOW_SEC 안에서 직전 신뢰 측정 쪽으로 돌아오는 점(물리적으로 직전 측정과
        # 이어지고, 튄 값보다 직전 값에 가까운 점)을 찾는다. 60→0→0처럼 떨어진 채 머물면 돌아온 것이 아니다.
        v0 = speed(last_ok)
        back = None
        for hj in range(gi + 1, len(groups)):
            h = groups[hj]
            if h.t - last_ok.t > RETURN_WINDOW_SEC:
                break
            if (_rate_ok(v0, last_ok.t, speed(h), h.t)
                    and abs(speed(h) - v0) < abs(speed(h) - speed(g))):
                back = hj
                break
        if back is not None:
            for m in range(gi, back):
                _mark(points, groups[m].indices,
                      f"속도 급변 ({v0:.0f}→{speed(groups[m]):.0f}→{speed(groups[back]):.0f} km/h, "
                      f"초당 {MAX_PHYSICAL_RATE_KMH_S:.0f} km/h 상한 초과 - 검증 필요)")
            trusted.append(groups[back])
            gi = back + 1
            continue
        if speed(g) > v0:
            _mark(points, g.indices,
                  f"가속 상한 초과 ({v0:.0f}→{speed(g):.0f} km/h를 {g.t - last_ok.t:.0f}초 만에, "
                  f"초당 {MAX_PHYSICAL_RATE_KMH_S:.0f} km/h 이상 - 검증 필요)")
            gi += 1
            continue   # 직전 신뢰 측정은 그대로 둔다 - 뒤 점들은 그것과 다시 비교한다
        # 급히 떨어진 뒤 돌아오지 않는 감속(충돌 뒤 정지 등)은 측정 오류라는 근거가 없다. 그대로 둔다.
        trusted.append(g)
        gi += 1


def mark_outliers(points: List[TrackPoint]) -> int:
    """이상치를 표시하고 개수를 돌려준다. 다시 부르면 이전 표시를 지우고 새로 판정한다."""
    # 손상 처리로 만든 행(빈 행·임의 복구 좌표)은 측정이 아니다 - 이상치 판정에서 뺀다.
    # gap_carved(회수한 실측)는 판정에 넣는다(리뷰 2026-10-11 #4).
    if any(is_synthetic(p) for p in points):
        return mark_outliers([p for p in points if not is_synthetic(p)])
    for p in points:
        p.is_outlier = False
        p.outlier_reason = ""

    # 1) 값 자체가 불가능한 행. 좌표가 없는 행(측위 전 V 문장 등)은 속도만으로 이상치로 보지 않는다 -
    #    표가 위경도를 포맷하다 죽었다(리뷰 #46).
    for i, p in enumerate(points):
        if p.latitude is not None and p.longitude is not None and not _coords_valid(p):
            _mark(points, [i], f"좌표가 유효 범위 밖 ({p.latitude}, {p.longitude})")
        elif (p.has_coords and p.speed_kmh is not None
              and (not math.isfinite(p.speed_kmh) or p.speed_kmh > MAX_SPEED_KMH or p.speed_kmh < 0)):
            _mark(points, [i], f"속도 비정상 ({p.speed_kmh:.0f} km/h)")

    # 1b) 속도 한 점 튐: 앞뒤 측정과 맞지 않게 치솟았다가 돌아오면(30→150→30) 측정 오류다. 그대로 두면
    #     가짜 급가속·급감속이 두 건 확정된다(리뷰 #48).
    speed_groups = [g for g in _groups(points) if points[g.indices[0]].speed_kmh is not None]
    for gi in range(1, len(speed_groups) - 1):
        ai, ci = _ref_before(speed_groups, gi), _ref_after(speed_groups, gi)   # 1초 창 앞뒤 측정과 비교
        if ai is None or ci is None:
            continue
        a, b, c = speed_groups[ai], speed_groups[gi], speed_groups[ci]
        va, vb, vc = (points[g.indices[0]].speed_kmh for g in (a, b, c))
        dt_ab, dt_bc = b.t - a.t, c.t - b.t
        if dt_ab <= 0 or dt_bc <= 0:
            continue
        up, down = (vb - va) / dt_ab, (vb - vc) / dt_bc
        if abs(up) > MAX_SPEED_JUMP_KMH_S and abs(down) > MAX_SPEED_JUMP_KMH_S and up * down > 0:
            _mark(points, b.indices, f"속도 급변 ({va:.0f}→{vb:.0f}→{vc:.0f} km/h, 앞뒤와 맞지 않음)")

    # 1c) 물리 상한: 직전 신뢰 측정과 비교해 차량이 낼 수 없는 변화율이면 측정으로 보지 않는다. 되돌아오면
    #     (한 점이든 여러 점이든) 그 사이가 튄 것이고, 가속 쪽은 되돌아오지 않아도 '검증 필요'다(리뷰 #76).
    _mark_impossible_rates(points)

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
