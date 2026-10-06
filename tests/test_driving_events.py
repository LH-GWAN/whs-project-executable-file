"""국토부 DTG 위험운전행동 판별 기준(2022) 속도 계열 판정 - 합성 궤적, 영상·키 불필요."""
import math
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest

from core.acceleration import compute_point_accelerations, speed_rate_pairs
from core.driving_events import (EV_ACCEL, EV_DECEL, EV_START, EV_STOP, VEHICLE_BUS, VEHICLE_CAR,
                                 VEHICLE_TRUCK, detect_driving_events, events_by_row, normalize_vehicle)
from engine.engine_adapter import TrackPoint

M_PER_DEG_LAT = 111_320.0


def track(speeds, headings=None, dt=1.0, start=(37.5, 127.0)):
    """속도(km/h)·진행각(도) 목록으로 1초 간격(기본) 궤적을 만든다. 좌표는 속도·방향대로 적분."""
    headings = headings or [0.0] * len(speeds)
    lat, lon = start
    out = []
    for i, (v, h) in enumerate(zip(speeds, headings)):
        if i:
            step = v / 3.6 * dt
            lat += step * math.cos(math.radians(h)) / M_PER_DEG_LAT
            lon += step * math.sin(math.radians(h)) / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
        t = i * dt
        out.append(TrackPoint(start_time_sec=t, latitude=lat, longitude=lon, speed_kmh=v,
                              track_deg=h, gps_date='2026-09-29',
                              gps_utc_time=f'00:{int(t) // 60:02}:{t % 60:06.3f}'))
    return out


def kinds(points, vehicle=VEHICLE_CAR):
    return [e.kind for e in detect_driving_events(points, vehicle)]


@pytest.mark.parametrize('vehicle, rate, expected', [
    (VEHICLE_CAR, 8, EV_ACCEL), (VEHICLE_CAR, 7.9, None),
    (VEHICLE_BUS, 6, EV_ACCEL), (VEHICLE_TRUCK, 5, EV_ACCEL), (VEHICLE_TRUCK, 4.9, None)])
def test_accel_band_above_20(vehicle, rate, expected):
    got = kinds(track([30, 30, 30 + rate, 30 + rate]), vehicle)
    assert got == ([expected] if expected else [])


def test_accel_band_uses_speed_before_change():
    # 8 km/h(6~10 구간)에서 초당 11 km/h: 승용차 기준 12 미만이라 아님, 버스 8 이상이라 급가속.
    pts = track([8, 8, 19, 19])
    assert kinds(pts, VEHICLE_CAR) == []
    assert kinds(pts, VEHICLE_BUS) == [EV_ACCEL]


def test_start_and_stop():
    assert kinds(track([0, 0, 10, 10])) == [EV_START]
    assert kinds(track([0, 0, 9, 9])) == []
    assert kinds(track([20, 20, 5, 5])) == [EV_STOP]        # 초당 15 감속해 5 km/h 이하
    assert kinds(track([18, 18, 5, 5])) == []               # 초당 13 < 승용차 14
    assert kinds(track([20, 20, 7, 7])) == []               # 7 km/h로 끝나 급정지 아님, 급감속은 14 필요
    assert kinds(track([12, 12, 4, 4]), VEHICLE_BUS) == []  # 초당 8 < 버스 9
    assert kinds(track([12, 12, 3, 3]), VEHICLE_BUS) == [EV_STOP]


def test_decel_requires_speed_kept_and_band():
    assert kinds(track([60, 60, 45, 45])) == [EV_DECEL]    # 50 초과 구간 15
    assert kinds(track([60, 60, 46, 46])) == []
    assert kinds(track([60, 60, 48, 48]), VEHICLE_BUS) == [EV_DECEL]  # 버스 50 초과 12
    assert kinds(track([30, 30, 21, 21]), VEHICLE_BUS) == [EV_DECEL]  # 버스 30 이하 9
    assert kinds(track([10, 10, 5.5, 5.5]), VEHICLE_TRUCK) == []      # 5~6 km/h는 급감속도 급정지도 아님


def test_consecutive_seconds_merge_into_one_event():
    events = detect_driving_events(track([30, 30, 40, 50, 60, 60]))
    assert [e.kind for e in events] == [EV_ACCEL]
    assert events[0].start_time_sec == 1 and events[0].end_time_sec == 4


def test_half_second_gps_uses_one_second_window():
    # 0.5초마다 기록(초당 2회)하는 기기: 0.5초 차이로 나누면 두 배(초당 12)로 부풀어 급가속이 된다.
    pts = track([30, 30, 33, 36, 39, 42, 42], dt=0.5)
    assert all(abs(r - 6) < 1e-6 for _p, _c, _dt, r in speed_rate_pairs(pts)[1:-1])
    assert kinds(pts) == []
    assert abs(compute_point_accelerations(pts)[4] - 6 / 3.6) < 1e-6







def test_events_by_row_and_vehicle_fallback():
    pts = track([30, 30, 40, 40])
    events = detect_driving_events(pts)
    rows = events_by_row(events, len(pts))
    assert [bool(r) for r in rows] == [False, True, True, False]
    assert normalize_vehicle(None) == normalize_vehicle('taxi') == VEHICLE_CAR
