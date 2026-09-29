"""위험운전 행동 판정 - 국토교통부 디지털운행기록장치(DTG) 위험운전행동 판별 기준(2022).

출처: 국토교통부 보도자료 「급가속 등 위험운전 행동이 교통사고 가능성 높인다」(2022.5.19,
교통안전정책과·한국교통안전공단) 참고2 '위험운전행동 판별 기준(22년)'. 11개 유형 중
①과속·②장기과속(도로 제한속도가 영상에 없음)과 ⑧급앞지르기는 판정하지 않는다.

기준표는 택시·버스·화물차 세 가지다. 승용차는 택시 기준을 쓴다(둘 다 승용 차체).

기준표 해석(표에 적히지 않은 부분):
  - "초당 X km/h"는 1초 창의 속도 변화다(core/acceleration.speed_rate_pairs).
  - 급가속·급감속의 속도 구간(6~10, 30 이하 …)은 **변화 전** 속도로 가른다. 급출발은 변화 전
    5 km/h 이하, 급정지는 변화 후 5 km/h 이하, 급감속은 변화 후 6 km/h 이상.
  - 급진로변경의 "5초 동안 누적각도 ±2°/sec 이하"는 5초 동안 초당 방향 변화를 더한 값,
    즉 5초 뒤 진행 방향이 처음과 2° 안쪽으로 돌아온 것으로 본다(차로만 바꾸고 방향은 그대로).
  - 급좌·우회전과 급U턴의 속도 조건은 창 안의 모든 측정에 적용한다(저속에서 GPS 방향이
    흔들려 생기는 오탐을 막는다). 160°를 넘는 회전은 급U턴으로 본다.
진행 방향은 GPS 진행각(track_deg)을 쓰고, 없는 행은 직전 좌표에서 잰 방위각으로 채운다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.acceleration import _distinct_fix_indices, speed_rate_pairs
from engine.engine_adapter import TrackPoint

VEHICLE_CAR = "car"
VEHICLE_BUS = "bus"
VEHICLE_TRUCK = "truck"
VEHICLE_TYPES = (VEHICLE_CAR, VEHICLE_BUS, VEHICLE_TRUCK)
VEHICLE_LABELS = {VEHICLE_CAR: "승용차", VEHICLE_BUS: "버스", VEHICLE_TRUCK: "화물차"}
DEFAULT_VEHICLE = VEHICLE_CAR

EV_ACCEL = "rapid_accel"
EV_START = "rapid_start"
EV_DECEL = "rapid_decel"
EV_STOP = "rapid_stop"
EV_LANE_CHANGE = "rapid_lane_change"
EV_LEFT_TURN = "rapid_left_turn"
EV_RIGHT_TURN = "rapid_right_turn"
EV_UTURN = "rapid_uturn"

# 표시 순서 겸 지도 선 색 우선순위(앞쪽이 이긴다).
EVENT_KINDS = (EV_STOP, EV_START, EV_ACCEL, EV_DECEL,
               EV_UTURN, EV_LEFT_TURN, EV_RIGHT_TURN, EV_LANE_CHANGE)
DISPLAY_ORDER = (EV_ACCEL, EV_START, EV_DECEL, EV_STOP,
                 EV_LANE_CHANGE, EV_LEFT_TURN, EV_RIGHT_TURN, EV_UTURN)
# Speed Analysis에는 속도 변화로 정해지는 넷만 그린다.
SPEED_EVENT_KINDS = (EV_ACCEL, EV_START, EV_DECEL, EV_STOP)

EVENT_LABELS = {
    EV_ACCEL: "급가속", EV_START: "급출발", EV_DECEL: "급감속", EV_STOP: "급정지",
    EV_LANE_CHANGE: "급진로변경", EV_LEFT_TURN: "급좌회전", EV_RIGHT_TURN: "급우회전",
    EV_UTURN: "급U턴",
}
# 지도 선·표지, 그래프 띠, 표 배경이 같은 색을 쓴다. 주행 경로(초록)·GPS 끊김(회색)·
# 현재 위치(파랑)와 겹치지 않게 골랐다.
EVENT_COLORS = {
    EV_ACCEL: "#e03131", EV_START: "#d6336c", EV_DECEL: "#f08c00", EV_STOP: "#7048e8",
    EV_LANE_CHANGE: "#0c8599", EV_LEFT_TURN: "#ae3ec9", EV_RIGHT_TURN: "#ae3ec9",
    EV_UTURN: "#795548",
}

ACCEL_MIN_SPEED = 6.0         # 급가속: 이 속도 이상에서
START_MAX_SPEED = 5.0         # 급출발: 이 속도 이하에서 출발
DECEL_MIN_END_SPEED = 6.0     # 급감속: 감속 후 속도가 이 이상
STOP_MAX_END_SPEED = 5.0      # 급정지: 감속 후 속도가 이 이하
LANE_MIN_SPEED = 30.0
LANE_WINDOW_SEC = 5.0
LANE_NET_MAX_DEG = 2.0
LANE_ACCEL_MAX = 2.0          # 초당 km/h
TURN_MIN_DEG = 60.0
UTURN_MIN_DEG = 160.0
HEADING_STEP_MAX_GAP_SEC = 2.5  # 진행 방향을 이어 더할 수 있는 측정 간격
TIME_TOLERANCE_SEC = 0.15
HEADING_MIN_MOVE_M = 1.0


@dataclass(frozen=True)
class Criteria:
    accel: Tuple[float, float, float]   # 변화 전 속도 6~10 / 10~20 / 20 초과일 때 초당 가속 km/h
    start: float                        # 급출발 초당 가속 km/h
    decel: Tuple[float, float, float]   # 변화 전 속도 30 이하 / 50 이하 / 50 초과일 때 초당 감속 km/h
    stop: float                         # 급정지 초당 감속 km/h
    lane_change_dps: float              # 급진로변경 초당 방향 변화(도)
    turn_speed: float                   # 급좌·우회전 최저 속도 km/h
    turn_window_sec: float
    uturn_speed: float
    uturn_window_sec: float


CRITERIA: Dict[str, Criteria] = {
    VEHICLE_CAR: Criteria((12, 10, 8), 10, (14, 15, 15), 14, 10, 30, 2, 25, 4),   # 택시 기준
    VEHICLE_BUS: Criteria((8, 7, 6), 8, (9, 10, 12), 9, 8, 25, 3, 20, 6),
    VEHICLE_TRUCK: Criteria((7, 6, 5), 6, (8, 8, 8), 8, 6, 20, 3, 15, 6),
}


def normalize_vehicle(vehicle_type: Optional[str]) -> str:
    return vehicle_type if vehicle_type in CRITERIA else DEFAULT_VEHICLE


def vehicle_label(vehicle_type: Optional[str]) -> str:
    return VEHICLE_LABELS[normalize_vehicle(vehicle_type)]


@dataclass
class DrivingEvent:
    kind: str
    start_index: int
    end_index: int
    start_time_sec: Optional[float]
    end_time_sec: Optional[float]
    # 속도 계열: 초당 속도 변화(km/h, 부호 유지). 회전 계열: 누적 회전각(도, +우 / -좌).
    # 급진로변경: 초당 방향 변화(도).
    peak: float
    detail: str = ""

    @property
    def label(self) -> str:
        return EVENT_LABELS.get(self.kind, self.kind)

    @property
    def color(self) -> str:
        return EVENT_COLORS.get(self.kind, "#e03131")

    @property
    def is_speed_event(self) -> bool:
        return self.kind in SPEED_EVENT_KINDS

    def covers(self, index: int) -> bool:
        return self.start_index <= index <= self.end_index


def count_events(events: List[DrivingEvent]) -> Dict[str, int]:
    counts = {kind: 0 for kind in DISPLAY_ORDER}
    for ev in events:
        counts[ev.kind] = counts.get(ev.kind, 0) + 1
    return counts


def summarize_counts(events: List[DrivingEvent], kinds=DISPLAY_ORDER) -> str:
    """"급가속 2 · 급감속 1" - 0건은 빼고, 하나도 없으면 "없음"."""
    counts = count_events(events)
    parts = [f"{EVENT_LABELS[k]} {counts[k]}" for k in kinds if counts.get(k)]
    return " · ".join(parts) if parts else "없음"


def events_by_row(events: List[DrivingEvent], row_count: int) -> List[List[DrivingEvent]]:
    """행마다 걸친 위험운전 목록(표시 순서대로)."""
    rows: List[List[DrivingEvent]] = [[] for _ in range(row_count)]
    order = {k: i for i, k in enumerate(DISPLAY_ORDER)}
    for ev in sorted(events, key=lambda e: order.get(e.kind, 99)):
        for i in range(max(0, ev.start_index), min(row_count, ev.end_index + 1)):
            if ev not in rows[i]:
                rows[i].append(ev)
    return rows


def criteria_lines(vehicle_type: Optional[str]) -> List[str]:
    """선택한 차종의 판별 기준 요약(툴팁·리포트용)."""
    v = normalize_vehicle(vehicle_type)
    c = CRITERIA[v]
    base = "택시 기준" if v == VEHICLE_CAR else f"{VEHICLE_LABELS[v]} 기준"
    return [
        f"국토교통부 DTG 위험운전행동 판별 기준(2022) · {VEHICLE_LABELS[v]} ({base})",
        f"급가속: 6~10 km/h에서 초당 {c.accel[0]:g}, 10~20 km/h에서 {c.accel[1]:g}, "
        f"20 km/h 초과에서 {c.accel[2]:g} km/h 이상 가속",
        f"급출발: 5 km/h 이하에서 출발해 초당 {c.start:g} km/h 이상 가속",
        f"급감속: 30 km/h 이하에서 초당 {c.decel[0]:g}, 50 km/h 이하 {c.decel[1]:g}, "
        f"50 km/h 초과 {c.decel[2]:g} km/h 이상 감속하고 6 km/h 이상 유지",
        f"급정지: 초당 {c.stop:g} km/h 이상 감속해 5 km/h 이하가 됨",
        f"급진로변경: 30 km/h 이상에서 초당 {c.lane_change_dps:g}° 이상 방향 전환, "
        f"5초간 누적 ±2° 이하·가감속 초당 ±2 km/h 이하",
        f"급좌·우회전: {c.turn_speed:g} km/h 이상에서 {c.turn_window_sec:g}초 안에 60~160° 회전",
        f"급U턴: {c.uturn_speed:g} km/h 이상에서 {c.uturn_window_sec:g}초 안에 160~180° 회전",
    ]


# ---------------------------------------------------------------------------
# 속도 계열: 급가속 · 급출발 · 급감속 · 급정지
# ---------------------------------------------------------------------------

def classify_speed_change(v0: float, v1: float, rate: float, c: Criteria) -> Optional[str]:
    """변화 전 속도 v0, 변화 후 v1, 초당 속도 변화 rate(km/h)로 종류를 정한다."""
    if rate > 0:
        if v0 <= START_MAX_SPEED:
            return EV_START if rate >= c.start else None
        if v0 >= ACCEL_MIN_SPEED:
            limit = c.accel[0] if v0 <= 10 else c.accel[1] if v0 <= 20 else c.accel[2]
            return EV_ACCEL if rate >= limit else None
        return None
    if rate < 0:
        drop = -rate
        if v1 <= STOP_MAX_END_SPEED:
            return EV_STOP if drop >= c.stop else None
        if v1 >= DECEL_MIN_END_SPEED:
            limit = c.decel[0] if v0 <= 30 else c.decel[1] if v0 <= 50 else c.decel[2]
            return EV_DECEL if drop >= limit else None
    return None


def _speed_events(points: List[TrackPoint], c: Criteria) -> List[DrivingEvent]:
    hits = []
    for prev_i, cur_i, _dt, rate in speed_rate_pairs(points):
        kind = classify_speed_change(points[prev_i].speed_kmh, points[cur_i].speed_kmh, rate, c)
        if kind:
            hits.append((kind, prev_i, cur_i, rate))
    events = _merge_hits(points, hits)
    for ev in events:
        v0 = points[ev.start_index].speed_kmh
        v1 = points[ev.end_index].speed_kmh
        ev.detail = f"초당 {ev.peak:+.1f} km/h ({v0:.0f}→{v1:.0f} km/h)"
    return events


# ---------------------------------------------------------------------------
# 방향 계열: 급진로변경 · 급좌회전 · 급우회전 · 급U턴
# ---------------------------------------------------------------------------

def _wrap(deg: float) -> float:
    """-180~180으로 접는다."""
    return (deg + 180.0) % 360.0 - 180.0


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _headings(points: List[TrackPoint], usable: List[int]) -> Dict[int, float]:
    from core.outliers import haversine_m
    out: Dict[int, float] = {}
    prev = None
    for i in usable:
        p = points[i]
        if p.track_deg is not None and math.isfinite(p.track_deg):
            out[i] = p.track_deg % 360.0
        elif prev is not None:
            q = points[prev]
            dt = p.start_time_sec - q.start_time_sec
            if (0 < dt <= HEADING_STEP_MAX_GAP_SEC
                    and haversine_m(q.latitude, q.longitude, p.latitude, p.longitude) >= HEADING_MIN_MOVE_M):
                out[i] = _bearing(q.latitude, q.longitude, p.latitude, p.longitude)
        prev = i
    return out


def _heading_chains(points: List[TrackPoint], usable: List[int],
                    headings: Dict[int, float]) -> List[List[int]]:
    """방향을 아는 측정을, 간격이 벌어지지 않고 이어진 묶음으로 나눈다."""
    chains: List[List[int]] = []
    current: List[int] = []
    for i in usable:
        if i not in headings:
            if current:
                chains.append(current)
            current = []
            continue
        if current:
            gap = points[i].start_time_sec - points[current[-1]].start_time_sec
            if not 0 < gap <= HEADING_STEP_MAX_GAP_SEC:
                chains.append(current)
                current = []
        current.append(i)
    if current:
        chains.append(current)
    return chains


def _turn_events(points: List[TrackPoint], chains: List[List[int]],
                 headings: Dict[int, float], c: Criteria) -> List[DrivingEvent]:
    turn_hits, uturn_hits = [], []
    for chain in chains:
        t = [points[i].start_time_sec for i in chain]
        v = [points[i].speed_kmh for i in chain]
        for a in range(len(chain)):
            for window, min_speed, want_uturn in ((c.turn_window_sec, c.turn_speed, False),
                                                  (c.uturn_window_sec, c.uturn_speed, True)):
                if v[a] < min_speed:
                    continue
                cum, best, best_b = 0.0, 0.0, None
                for b in range(a + 1, len(chain)):
                    if t[b] - t[a] > window + TIME_TOLERANCE_SEC or v[b] < min_speed:
                        break
                    cum += _wrap(headings[chain[b]] - headings[chain[b - 1]])
                    if abs(cum) > abs(best):
                        best, best_b = cum, b
                if best_b is None:
                    continue
                if want_uturn and abs(best) >= UTURN_MIN_DEG:
                    uturn_hits.append((EV_UTURN, chain[a], chain[best_b], best))
                elif not want_uturn and TURN_MIN_DEG <= abs(best) < UTURN_MIN_DEG:
                    kind = EV_RIGHT_TURN if best > 0 else EV_LEFT_TURN
                    turn_hits.append((kind, chain[a], chain[best_b], best))
    uturns = _merge_hits(points, uturn_hits)
    # U턴 도중의 앞부분(60~160°)이 급좌·우회전으로 또 잡히면 뺀다.
    turns = [ev for ev in _merge_hits(points, turn_hits)
             if not any(ev.start_index <= u.end_index and u.start_index <= ev.end_index for u in uturns)]
    for ev in turns + uturns:
        seconds = points[ev.end_index].start_time_sec - points[ev.start_index].start_time_sec
        side = "우" if ev.peak > 0 else "좌"
        ev.detail = f"{seconds:.1f}초간 {side}측 {abs(ev.peak):.0f}° 회전"
    return turns + uturns


def _lane_change_events(points: List[TrackPoint], chains: List[List[int]],
                        headings: Dict[int, float], c: Criteria) -> List[DrivingEvent]:
    rate_at = {cur: (prev, rate) for prev, cur, _dt, rate in speed_rate_pairs(points)}
    position = {i: (n, k) for n, chain in enumerate(chains) for k, i in enumerate(chain)}
    hits = []
    for cur, (prev, _rate) in rate_at.items():
        if prev not in position or cur not in position:
            continue
        (n0, k0), (n1, k1) = position[prev], position[cur]
        if n0 != n1:
            continue
        chain = chains[n0]
        if points[prev].speed_kmh < LANE_MIN_SPEED or points[cur].speed_kmh < LANE_MIN_SPEED:
            continue
        dt = points[cur].start_time_sec - points[prev].start_time_sec
        turn_rate = sum(_wrap(headings[chain[k]] - headings[chain[k - 1]])
                        for k in range(k0 + 1, k1 + 1)) / dt
        if abs(turn_rate) < c.lane_change_dps:
            continue
        # 앞 측정부터 5초 동안: 방향이 처음과 2° 안쪽으로 돌아오고, 가감속이 작아야 한다.
        t0 = points[prev].start_time_sec
        end_k = None
        for k in range(k0 + 1, len(chain)):
            if points[chain[k]].start_time_sec - t0 > LANE_WINDOW_SEC + TIME_TOLERANCE_SEC:
                break
            end_k = k
        if end_k is None or points[chain[end_k]].start_time_sec - t0 < LANE_WINDOW_SEC - TIME_TOLERANCE_SEC:
            continue  # 5초를 다 보지 못했다
        net = sum(_wrap(headings[chain[k]] - headings[chain[k - 1]]) for k in range(k0 + 1, end_k + 1))
        if abs(net) > LANE_NET_MAX_DEG:
            continue
        window = set(chain[k0:end_k + 1])
        if any(abs(r) > LANE_ACCEL_MAX for c_i, (p_i, r) in rate_at.items()
               if c_i in window and p_i in window):
            continue
        hits.append((EV_LANE_CHANGE, prev, chain[end_k], turn_rate))
    events = _merge_hits(points, hits)
    for ev in events:
        net = _wrap(headings[ev.end_index] - headings[ev.start_index])
        ev.detail = f"초당 {ev.peak:+.0f}° 방향 전환, 5초 뒤 방향 차 {net:+.1f}°"
    return events


# ---------------------------------------------------------------------------

def _merge_hits(points: List[TrackPoint], hits) -> List[DrivingEvent]:
    """같은 종류의 판정이 겹치거나 맞닿으면 한 건으로 묶는다(초당 판정이 몇 초 이어져도 1건).
    peak는 크기가 가장 큰 값."""
    events: List[DrivingEvent] = []
    for kind, start, end, value in sorted(hits, key=lambda h: (h[0], h[1], h[2])):
        last = events[-1] if events and events[-1].kind == kind else None
        if last is not None and start <= last.end_index:
            last.end_index = max(last.end_index, end)
            if abs(value) > abs(last.peak):
                last.peak = value
            continue
        events.append(DrivingEvent(kind=kind, start_index=start, end_index=end,
                                   start_time_sec=None, end_time_sec=None, peak=value))
    for ev in events:
        ev.start_time_sec = points[ev.start_index].start_time_sec
        ev.end_time_sec = points[ev.end_index].start_time_sec
    return events


def detect_driving_events(points: List[TrackPoint],
                          vehicle_type: Optional[str] = DEFAULT_VEHICLE) -> List[DrivingEvent]:
    """선택한 차종 기준으로 위험운전 행동을 찾는다. 시간 순으로 돌려준다."""
    c = CRITERIA[normalize_vehicle(vehicle_type)]
    usable = _distinct_fix_indices(points)
    headings = _headings(points, usable)
    chains = _heading_chains(points, usable, headings)
    events = (_speed_events(points, c)
              + _turn_events(points, chains, headings, c)
              + _lane_change_events(points, chains, headings, c))
    order = {k: i for i, k in enumerate(DISPLAY_ORDER)}
    events.sort(key=lambda e: (e.start_index, order.get(e.kind, 99)))
    return events
