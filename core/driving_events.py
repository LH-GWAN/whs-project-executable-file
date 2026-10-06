"""위험운전 행동 판정 - 국토교통부 디지털운행기록장치(DTG) 위험운전행동 판별 기준(2022).

출처: 국토교통부 보도자료 「급가속 등 위험운전 행동이 교통사고 가능성 높인다」(2022.5.19,
교통안전정책과·한국교통안전공단) 참고2 '위험운전행동 판별 기준(22년)'. 11개 유형 중 속도 변화로
정해지는 넷 - ③급가속 ④급출발 ⑤급감속 ⑥급정지 - 만 판정한다. 방향 계열(급진로변경·급앞지르기·
급좌우회전·급U턴)과 과속·장기과속은 쓰지 않기로 했다(사용자 결정: 넷 외에는 폐기).

기준표는 택시·버스·화물차 세 가지다. 승용차는 택시 기준을 쓴다(둘 다 승용 차체).

기준표 해석(표에 적히지 않은 부분):
  - "초당 X km/h"는 1초 창의 속도 변화다(core/acceleration.speed_rate_pairs).
  - 급가속·급감속의 속도 구간(6~10, 30 이하 …)은 **변화 전** 속도로 가른다. 급출발은 변화 전
    5 km/h 이하, 급정지는 변화 후 5 km/h 이하, 급감속은 변화 후 6 km/h 이상.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.acceleration import speed_rate_pairs
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

# 표시 순서 겸 지도 선 색 우선순위(앞쪽이 이긴다).
EVENT_KINDS = (EV_STOP, EV_START, EV_ACCEL, EV_DECEL)
DISPLAY_ORDER = (EV_ACCEL, EV_START, EV_DECEL, EV_STOP)
SPEED_EVENT_KINDS = DISPLAY_ORDER

EVENT_LABELS = {EV_ACCEL: "급가속", EV_START: "급출발", EV_DECEL: "급감속", EV_STOP: "급정지"}
# 지도 선·표지, 그래프 선, 표 배경이 같은 색을 쓴다. 주행 경로(초록)·GPS 끊김(회색)·
# 현재 위치(파랑)와 겹치지 않게 골랐다.
EVENT_COLORS = {EV_ACCEL: "#e03131", EV_START: "#d6336c", EV_DECEL: "#f08c00", EV_STOP: "#7048e8"}

ACCEL_MIN_SPEED = 6.0         # 급가속: 이 속도 이상에서
START_MAX_SPEED = 5.0         # 급출발: 이 속도 이하에서 출발
DECEL_MIN_END_SPEED = 6.0     # 급감속: 감속 후 속도가 이 이상
STOP_MAX_END_SPEED = 5.0      # 급정지: 감속 후 속도가 이 이하


@dataclass(frozen=True)
class Criteria:
    accel: Tuple[float, float, float]   # 변화 전 속도 6~10 / 10~20 / 20 초과일 때 초당 가속 km/h
    start: float                        # 급출발 초당 가속 km/h
    decel: Tuple[float, float, float]   # 변화 전 속도 30 이하 / 50 이하 / 50 초과일 때 초당 감속 km/h
    stop: float                         # 급정지 초당 감속 km/h


CRITERIA: Dict[str, Criteria] = {
    VEHICLE_CAR: Criteria((12, 10, 8), 10, (14, 15, 15), 14),   # 택시 기준
    VEHICLE_BUS: Criteria((8, 7, 6), 8, (9, 10, 12), 9),
    VEHICLE_TRUCK: Criteria((7, 6, 5), 6, (8, 8, 8), 8),
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
    peak: float          # 초당 속도 변화(km/h, 부호 유지)
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
    events = _speed_events(points, c)
    order = {k: i for i, k in enumerate(DISPLAY_ORDER)}
    events.sort(key=lambda e: (e.start_index, order.get(e.kind, 99)))
    return events
