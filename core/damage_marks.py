"""손상 구간을 시간축에 올리고(표시용 빈 행), 선택하면 임의 복구(앞뒤 좌표를 잇는 추정 좌표)를 넣는다.

사용자 결정(2026-10-11)
  - 기본은 '복구하지 않음'(추천): 손상 구간은 빈 행으로 두고 지도·그래프·표에 손상으로 표시한다.
  - '임의 복구': 손상 직전 좌표와 손상 뒤 첫 좌표를 1초 간격으로 직선 보간해 넣는다. 지도에는 다른 색으로,
    표에는 '임의 복구'로 표시하고 속도는 만들지 않는다(그래프는 그 구간을 비운다).
  - 너무 멀리 갔으면 복구 불가: 손상 직전 속도 × 잘린 시간 = 예상 이동 거리, 실제 두 좌표 거리가
    예상 × 1.5 + 50 m를 넘으면 임의 복구를 하지 않고 안내한다. 직전에 정지 중(3 km/h 미만)이면 그 앞
    60초 안의 마지막 움직이던 속도를 기준으로 한다.
  - 영상은 원본을 그대로 재생한다(시간축 유지).

recovery_state 계약(리뷰 2026-10-11 #4·#14·#15·#20)
  - "gap_carved"는 손상 구간에서 회수한 **실측**이다. 시간축 앵커·판정·집계에 모두 넣는다.
  - "damaged"(빈 행)·"interpolated"(추정 행)는 합성 행이다(TrackPoint.is_synthetic). 어디서도 실측처럼
    보이면 안 되고, 실측(회수 포함)이 있는 초에는 만들지 않는다.
"""
from __future__ import annotations

import bisect
import math
import os
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

from core.damage import DamageReport
from core.outliers import haversine_m
from engine.engine_adapter import CancelledError, TrackPoint

MODE_NONE = "none"
MODE_INTERPOLATE = "interpolate"
MODE_LABELS = {MODE_NONE: "복구하지 않음", MODE_INTERPOLATE: "임의 복구"}

PLAUSIBLE_FACTOR = 1.5
PLAUSIBLE_SLACK_M = 50.0
MOVING_KMH = 3.0
LOOKBACK_SEC = 60.0
MIN_INTERVAL_SEC = 1.0
MAX_PLACEHOLDERS = 20_000
# 손상 구간 판정: 앞뒤 기록의 시간 간격이 기록 주기(중앙값)의 이 배수를 넘어야 '기록이 실제로 빠졌다'(리뷰 #15).
RECORD_GAP_FACTOR = 1.5
# 임의 복구 앵커(좌표)가 손상 구간 가장자리에서 이보다 멀면(터널 등 GPS 끊김) 잇지 않는다(리뷰 #40).
MAX_ANCHOR_DISTANCE_SEC = 5.0
# 시간축 추정 연속성: 기준 행보다 이만큼 넘게 뒤로 가거나(중복·순서 뒤바뀜 허용치) 앞으로 뛰면 다른 녹화(잔재)로
# 보고 그 뒤를 본 궤적에서 뺀다(리뷰 #3).
UTC_BACKSTEP_TOLERANCE_SEC = 5.0
UTC_MAX_FORWARD_GAP_SEC = 600.0

TIME_SOURCE_UTC_ESTIMATED = "gps_utc_estimated"
TIME_SOURCE_ELAPSED_ESTIMATED = "gps_elapsed_estimated"
TIME_SOURCE_ABS_TIME_ESTIMATED = "gps_abs_time_estimated"
ESTIMATED_TIME_SOURCES = (TIME_SOURCE_UTC_ESTIMATED, TIME_SOURCE_ELAPSED_ESTIMATED,
                          TIME_SOURCE_ABS_TIME_ESTIMATED)
# 추정 시각 안내 문구(상태 줄·리포트 공용, 리뷰 #74).
TIME_ESTIMATED_WARNING = ("재생 시각은 영상 시간축이 아니라 GPS 기록(UTC·경과 초)으로 추정한 값입니다 "
                          "- 영상 시각과 몇 초 어긋날 수 있습니다.")


@dataclass
class DamageInterval:
    start_sec: float
    end_sec: float
    kind: str                 # "gap" / "zero" / "truncated" / "head"
    outcome: str = ""         # "interpolated" / "implausible" / "not_recovered" / "no_anchor"
    note: str = ""
    expected_m: Optional[float] = None
    actual_m: Optional[float] = None
    # 표시용 행 한도(MAX_PLACEHOLDERS)에 걸려 이 구간의 행을 다 만들지 못함(리뷰 #43).
    placeholder_limited: bool = False

    @property
    def label(self) -> str:
        names = {"interpolated": "손상으로 인한 임의복구", "implausible": "손상 - 복구 불가(이동 거리 비정상)",
                 "not_recovered": "손상으로 복구하지 않음", "no_anchor": "손상 - 복구 불가(앞뒤 좌표 없음)"}
        return names.get(self.outcome, "손상")

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 시간축 추정 (헤더 소실 AVI, moov 소실 MP4 카빙, FineVu 72바이트 레코드)
# ---------------------------------------------------------------------------

def estimate_times_from_utc(points: List[TrackPoint], excluded: Optional[List[TrackPoint]] = None) -> bool:
    """재생 시각이 하나도 없을 때 GPS 기록으로 시각을 추정한다. 추정했으면 True.

    기준은 체크섬이 맞고 측위가 유효한(A) 행의 날짜+UTC 절대 시각이다(리뷰 #3). 첫 기준 행을 0초로
    두고, 연속성이 깨지는(뒤로 가거나 10분 넘게 뛰는) 지점부터는 다른 녹화의 잔재로 보고 본 궤적에서
    뺀다 - 뺀 행은 `points`에서 제거하고 `excluded`에 넣는다(슬랙으로 표시). 음수 시각은 버린다.
    UTC가 없는 기기(FineVu)는 경과 초(gps_elapsed), 그것도 없으면 abs_time으로 추정한다(리뷰 #77).
    체크섬이 틀리거나 UTC가 없는 행은 바로 앞 행의 시각을 따른다(같은 측정의 반복 행).
    """
    if any(p.start_time_sec is not None for p in points):
        return False
    dropped: List[TrackPoint] = []
    for keys, source in ((_utc_keys(points), TIME_SOURCE_UTC_ESTIMATED),
                         (_elapsed_keys(points), TIME_SOURCE_ELAPSED_ESTIMATED),
                         (_abs_time_keys(points), TIME_SOURCE_ABS_TIME_ESTIMATED)):
        if keys and _assign_estimated_times(points, keys, source, dropped):
            break
    else:
        return False
    if dropped:
        gone = {id(p) for p in dropped}
        points[:] = [p for p in points if id(p) not in gone]
        if excluded is not None:
            excluded.extend(dropped)
    points.sort(key=lambda p: p.start_time_sec)
    return True


def _is_time_anchor(p: TrackPoint) -> bool:
    """시간축 기준으로 믿을 수 있는 행: 체크섬 OK, 측위 유효(A)."""
    return (p.gps_checksum_ok is not False and p.gps_trusted is not False
            and (p.gps_status or "").strip().upper() in ("", "A"))


def _utc_keys(points: List[TrackPoint]) -> Dict[int, float]:
    """행 번호 → 절대 초(날짜+UTC). 날짜가 있는 기준 행이 하나라도 있으면 날짜를 쓰고, 없으면 UTC만
    쓰되 자정 넘김은 하루를 더한다. 날짜 모드에서 날짜 없는 행은 '시각 없음'으로 둔다."""
    use_dates = any(_date_days(p.gps_date) is not None and _utc_seconds(p.gps_utc_time) is not None
                    and _is_time_anchor(p) for p in points)
    keys: Dict[int, float] = {}
    carry, prev = 0.0, None
    for i, p in enumerate(points):
        u = _utc_seconds(p.gps_utc_time)
        if u is None:
            continue
        if use_dates:
            d = _date_days(p.gps_date)
            if d is None:
                continue
            keys[i] = d * 86400.0 + u
            continue
        if prev is not None and u + carry < prev - 43200:
            carry += 86400.0
        keys[i] = u + carry
        if _is_time_anchor(p):
            prev = keys[i] if prev is None else max(prev, keys[i])
    return keys


def _elapsed_keys(points: List[TrackPoint]) -> Dict[int, float]:
    return {i: float(p.gps_elapsed) for i, p in enumerate(points)
            if p.gps_elapsed is not None and math.isfinite(p.gps_elapsed)}


def _abs_time_keys(points: List[TrackPoint]) -> Dict[int, float]:
    keys = {}
    for i, p in enumerate(points):
        t = _abs_time_seconds(p.gps_abs_time)
        if t is not None:
            keys[i] = t
    return keys


def _assign_estimated_times(points: List[TrackPoint], keys: Dict[int, float], source: str,
                            dropped: List[TrackPoint]) -> bool:
    anchors = [i for i in sorted(keys) if _is_time_anchor(points[i])]
    if not anchors:
        return False
    base = keys[anchors[0]]
    # 첫 연속 블록: 기준 행 순서대로 보며 연속성이 깨지는 첫 기준 행에서 자른다(그 뒤는 전부 잔재).
    block_end = len(points)
    last = base
    for i in anchors[1:]:
        t = keys[i]
        if t < last - UTC_BACKSTEP_TOLERANCE_SEC or t > last + UTC_MAX_FORWARD_GAP_SEC:
            block_end = i
            break
        last = max(last, t)
    current = 0.0
    seen_anchor = False
    for i, p in enumerate(points):
        if i >= block_end:
            dropped.append(p)
            continue
        if i in keys:
            rel = keys[i] - base
            is_anchor = _is_time_anchor(p)
            if is_anchor or (seen_anchor and -UTC_BACKSTEP_TOLERANCE_SEC <= rel - current <= UTC_MAX_FORWARD_GAP_SEC):
                if rel < 0:
                    dropped.append(p)      # 음수 시각(기준보다 앞서는 잔재·순서 뒤바뀜)은 버린다
                    continue
                current = rel
                seen_anchor = seen_anchor or is_anchor
            elif not seen_anchor:
                dropped.append(p)          # 기준 행 앞의 체크섬 실패·RTC 기본값(1980년) 행
                continue
        elif not seen_anchor and p.has_gps_record and not _is_time_anchor(p):
            dropped.append(p)
            continue
        p.start_time_sec = current
        p.end_time_sec = current + 1.0
        p.time_source = source
        p.time_estimated = True
    return any(p.start_time_sec is not None for p in points)


def _utc_seconds(text: str) -> Optional[float]:
    if not text:
        return None
    digits = text.strip().replace(":", "")
    try:
        value = int(digits[0:2]) * 3600 + int(digits[2:4]) * 60 + float(digits[4:] or 0)
    except ValueError:
        return None
    return value if 0 <= value < 86400 + 60 else None


def _date_days(text: str) -> Optional[int]:
    """gps_date('2024-09-16' 또는 NMEA 'ddmmyy')를 1970-01-01 기준 일수로. 못 읽으면 None."""
    text = (text or "").strip()
    if not text:
        return None
    from datetime import date
    try:
        if len(text) == 6 and text.isdigit():
            yy = int(text[4:6])
            d = date(1900 + yy if yy >= 80 else 2000 + yy, int(text[2:4]), int(text[0:2]))
        else:
            d = date.fromisoformat(text[:10])
    except ValueError:
        return None
    return (d - date(1970, 1, 1)).days


def _abs_time_seconds(text: str) -> Optional[float]:
    text = (text or "").strip()
    if not text:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 손상 구간 → 재생 시각 구간
# ---------------------------------------------------------------------------

def compute_intervals(points: List[TrackPoint], report: DamageReport,
                      duration_sec: Optional[float]) -> List[DamageInterval]:
    """바이트 구간을 재생 시각 구간으로 옮긴다.

    파일 위치(source_offset)가 있는 실측 행(회수 행 gap_carved 포함, 리뷰 #14)을 앵커로 쓴다. 이웃한
    두 앵커 사이의 바이트 범위가 손상 구간과 겹치고, 두 앵커의 시간 간격이 기록 주기의 1.5배를 넘을 때만
    (기록이 실제로 빠졌을 때만, 리뷰 #15) 구간이 된다. 앵커가 하나도 없으면(udta/mamt 영상처럼 GPS가
    미디어 밖에 있는 경우) 바이트 구간은 시각으로 옮기지 않는다(리뷰 #29)."""
    real = [p for p in points if p.start_time_sec is not None and not p.is_synthetic]
    anchored = sorted((p for p in real if p.source_offset is not None), key=lambda p: p.source_offset)
    # 기록 주기는 앵커(GPS 기록) 기준으로 잰다 - 센서 행까지 섞으면 주기가 짧아져 멀쩡한 1초 간격도 '빠짐'이 된다.
    nominal = _nominal_spacing(anchored or real)
    threshold = nominal * RECORD_GAP_FACTOR + 1e-6
    regions = report.regions()
    starts = [a for a, _ in regions]
    out: List[DamageInterval] = []
    if anchored and regions:
        first, last = anchored[0], anchored[-1]
        if regions[0][0] < first.source_offset and first.start_time_sec > threshold:
            out.append(DamageInterval(0.0, round(first.start_time_sec, 3), "head"))
        for p, q in zip(anchored, anchored[1:]):
            if q.start_time_sec <= p.start_time_sec + threshold:
                continue
            region = _overlapping_region(regions, starts, p.source_offset, q.source_offset)
            if region is None:
                continue
            out.append(DamageInterval(round(p.start_time_sec, 3), round(q.start_time_sec, 3),
                                      _region_kind(report, region)))
        if duration_sec and regions[-1][1] > last.source_offset and duration_sec - last.start_time_sec > threshold:
            region = _overlapping_region(regions, starts, last.source_offset, float("inf"))
            out.append(DamageInterval(round(last.start_time_sec, 3), round(duration_sec, 3),
                                      _region_kind(report, region) if region else "zero"))
    if report.truncated and real and duration_sec:
        last_t = max(p.start_time_sec for p in real)
        if duration_sec - last_t >= max(2.0, threshold) and not any(abs(iv.end_sec - duration_sec) < 1e-6 for iv in out):
            out.append(DamageInterval(round(last_t, 3), round(duration_sec, 3), "truncated"))
    out.sort(key=lambda iv: iv.start_sec)
    return _merge(out)


def _nominal_spacing(real: List[TrackPoint]) -> float:
    """기록 주기(초): 시각순 이웃 간격의 중앙값. 기록이 적으면 1초."""
    times = sorted(p.start_time_sec for p in real)
    diffs = sorted(b - a for a, b in zip(times, times[1:]) if b - a > 1e-6)
    if len(diffs) < 2:
        return 1.0
    return diffs[len(diffs) // 2]


def _overlapping_region(regions, starts, lo, hi):
    """[lo, hi)와 겹치는 손상 구간(정렬·병합된 목록이라 start < hi 인 마지막 것만 보면 된다)."""
    i = bisect.bisect_left(starts, hi) - 1
    if i >= 0 and regions[i][1] > lo:
        return regions[i]
    return None


def _region_kind(report: DamageReport, region) -> str:
    a, b = region
    return "gap" if any(ga < b and gb > a for ga, gb in report.gaps) else "zero"


def _merge(items: List[DamageInterval]) -> List[DamageInterval]:
    """겹치는 구간만 합친다 - 맞닿은 구간을 합치면 그 경계 초의 실측이 손상 구간 안에 들어간다(리뷰 #15)."""
    merged: List[DamageInterval] = []
    for iv in items:
        if merged and iv.start_sec < merged[-1].end_sec:
            merged[-1].end_sec = max(merged[-1].end_sec, iv.end_sec)
        else:
            merged.append(iv)
    return merged


def _reference_speed_kmh(points: List[TrackPoint], anchor: TrackPoint) -> float:
    """손상 직전 속도. 정지 중이면 그 앞 60초 안의 마지막 움직이던 속도, 그것도 없으면 0."""
    if anchor.speed_kmh is not None and anchor.speed_kmh >= MOVING_KMH:
        return anchor.speed_kmh
    t0 = anchor.start_time_sec or 0.0
    for p in reversed(points):
        if p.start_time_sec is None or p.start_time_sec > t0:
            continue
        if t0 - p.start_time_sec > LOOKBACK_SEC:
            break
        if p.has_fix and p.speed_kmh is not None and p.speed_kmh >= MOVING_KMH:
            return p.speed_kmh
    return 0.0


# ---------------------------------------------------------------------------
# 구간마다 빈 행 / 추정 행
# ---------------------------------------------------------------------------

def apply_damage_marks(points: List[TrackPoint], intervals: List[DamageInterval], mode: str) -> List[TrackPoint]:
    """구간마다 빈 행(복구 안 함) 또는 추정 좌표 행(임의 복구)을 끼워 넣는다. 원본 행은 바꾸지 않는다.
    돌려주는 목록은 시각순이다. intervals의 outcome/note를 채운다.
    실측(회수 행 포함)이 있는 초에는 행을 만들지 않고(리뷰 #14·#15), 추정 행은 손상 구간 안에서만
    만든다(리뷰 #40). 표시 행 한도에 걸리면 구간에 placeholder_limited를 남긴다(리뷰 #43)."""
    base = [p for p in points if not p.is_synthetic]
    base.sort(key=lambda p: (p.start_time_sec is None, p.start_time_sec or 0.0))
    occupied = _OccupiedSeconds(base)
    extra: List[TrackPoint] = []
    budget = MAX_PLACEHOLDERS
    for iv in intervals:
        before = _last_fix_before(base, iv.start_sec)
        after = _first_fix_after(base, iv.end_sec)
        segment = before.segment_index if before else (after.segment_index if after else 0)
        rows: List[TrackPoint] = []
        if mode == MODE_INTERPOLATE and before is not None and after is not None:
            gap_before = iv.start_sec - before.start_time_sec
            gap_after = after.start_time_sec - iv.end_sec
            if gap_before > MAX_ANCHOR_DISTANCE_SEC or gap_after > MAX_ANCHOR_DISTANCE_SEC:
                iv.outcome = "no_anchor"
                iv.note = (f"손상 구간 가까이에 좌표가 없어(앞 {gap_before:.0f}초, 뒤 {gap_after:.0f}초 떨어짐 - "
                           "GPS 끊김) 이을 수 없습니다.")
                rows = _placeholders(iv, segment, occupied)
            else:
                dt = max(after.start_time_sec - before.start_time_sec, 0.001)
                v_ref = _reference_speed_kmh(base, before)
                expected = v_ref / 3.6 * dt
                actual = haversine_m(before.latitude, before.longitude, after.latitude, after.longitude)
                iv.expected_m, iv.actual_m = round(expected, 1), round(actual, 1)
                if actual > PLAUSIBLE_FACTOR * expected + PLAUSIBLE_SLACK_M:
                    iv.outcome = "implausible"
                    iv.note = (f"앞뒤 좌표 거리 {actual:.0f} m가 예상 이동 거리 {expected:.0f} m(직전 속도 "
                               f"{v_ref:.0f} km/h × {dt:.0f}초)의 1.5배+50 m를 넘어 임의 복구를 하지 않았습니다.")
                    rows = _placeholders(iv, segment, occupied)
                else:
                    iv.outcome = "interpolated"
                    iv.note = (f"직전 좌표와 다음 좌표를 직선으로 이었습니다 (거리 {actual:.0f} m, 예상 {expected:.0f} m). "
                               "속도는 만들지 않았습니다.")
                    rows = _interpolated(before, after, iv, segment, occupied)
        elif mode == MODE_INTERPOLATE:
            iv.outcome = "no_anchor"
            iv.note = "손상 앞이나 뒤에 좌표가 없어 이을 수 없습니다."
            rows = _placeholders(iv, segment, occupied)
        else:
            iv.outcome = "not_recovered"
            iv.note = "손상 구간의 기록은 비워 두었습니다."
            rows = _placeholders(iv, segment, occupied)
        if len(rows) > budget:
            rows = rows[:max(budget, 0)]
            iv.placeholder_limited = True
        extra.extend(rows)
        budget -= len(rows)
    merged = base + extra
    merged.sort(key=lambda p: (p.start_time_sec is None, p.start_time_sec or 0.0))
    return merged


class _OccupiedSeconds:
    """실측(회수 행 포함)이 있는 시각. 합성 행 후보 t는 ±w 안에 실측이 있으면 만들지 않는다. w는 GPS 기록
    주기의 절반(최대 0.5초): 1 Hz 기록이면 그 초 전체, 프레임마다 행이 있는 AVI(30 Hz)면 그 행 자리만 본다."""

    def __init__(self, base: List[TrackPoint]):
        timed = [p for p in base if p.start_time_sec is not None]
        records = [p for p in timed if p.source_offset is not None] or [p for p in timed if p.has_gps_record] or timed
        self.window = min(0.5, _nominal_spacing(records) / 2.0)
        self.times = sorted(p.start_time_sec for p in timed)

    def __contains__(self, t) -> bool:
        i = bisect.bisect_left(self.times, t - self.window)
        return i < len(self.times) and self.times[i] < t + self.window


def _last_fix_before(points, t):
    best = None
    for p in points:
        if p.start_time_sec is None or p.start_time_sec > t:
            continue
        if p.has_fix:
            best = p
    return best


def _first_fix_after(points, t):
    for p in points:
        if p.start_time_sec is not None and p.start_time_sec >= t and p.has_fix:
            return p
    return None


def _placeholders(iv: DamageInterval, segment: int, occupied) -> List[TrackPoint]:
    out = []
    t = math.floor(iv.start_sec) + 1.0
    while t < iv.end_sec:
        if t not in occupied:
            out.append(TrackPoint(start_time_sec=t, end_time_sec=t + 1.0, time_source="damage",
                                  recovery_state="damaged", segment_index=segment))
        t += 1.0
    return out


def _interpolated(before: TrackPoint, after: TrackPoint, iv: DamageInterval, segment: int,
                  occupied) -> List[TrackPoint]:
    out = []
    t0, t1 = before.start_time_sec, after.start_time_sec
    t = math.floor(iv.start_sec) + 1.0
    while t < iv.end_sec:
        if t not in occupied:
            f = (t - t0) / (t1 - t0)
            out.append(TrackPoint(
                start_time_sec=t, end_time_sec=t + 1.0, time_source="interpolated",
                latitude=before.latitude + (after.latitude - before.latitude) * f,
                longitude=before.longitude + (after.longitude - before.longitude) * f,
                speed_kmh=None, recovery_state="interpolated", segment_index=segment))
        t += 1.0
    return out


# ---------------------------------------------------------------------------
# moov 소실 MP4: 파일 전체 GPS 카빙
# ---------------------------------------------------------------------------

def carve_whole_file_gps(path: str, start: int = 0, end: Optional[int] = None,
                         cancel_event=None) -> List[TrackPoint]:
    """moov(샘플 표)가 사라진 MP4(조각 MP4 포함): 미디어 영역 전체에서 NMEA RMC 문장을 찾아 좌표 행을 만든다.
    시각은 없고(뒤에서 estimate_times_from_utc가 날짜+UTC로 추정) 파일 위치만 남긴다. 같은 (날짜, UTC)는
    한 번만. 취소되면 CancelledError(리뷰 #100)."""
    from core.paths import ensure_vendor_importable
    ensure_vendor_importable()
    import integration_mp4 as _mp4
    size = os.path.getsize(path)
    end = size if end is None else min(end, size)
    out: List[TrackPoint] = []
    seen = set()
    with open(path, "rb") as f:
        pos = max(0, start)
        while pos < end:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("분석이 취소되었습니다.")
            f.seek(pos)
            chunk = f.read(min(4 * 1024 * 1024, end - pos))
            if not chunk:
                break
            for m in _mp4.GAP_NMEA_RE.finditer(chunk):
                raw = m.group().decode("ascii", "replace")
                parsed = _mp4.try_parse_nmea(raw if raw.startswith("$") else "$" + raw)
                if (not parsed or parsed.get("checksum_ok") is not True or not parsed.get("status_valid")
                        or parsed.get("lat") is None or parsed.get("lon") is None):
                    continue
                key = (parsed.get("date", ""), parsed.get("utc_time", ""))
                if key in seen:
                    continue
                seen.add(key)
                out.append(TrackPoint(
                    latitude=parsed["lat"], longitude=parsed["lon"], speed_kmh=parsed.get("speed_kmh"),
                    track_deg=_float_or_none(parsed.get("track_deg")),
                    gps_date=parsed.get("date", ""), gps_utc_time=parsed.get("utc_time", ""),
                    gps_checksum_ok=True, gps_trusted=bool(parsed.get("trusted", True)), gps_status="A",
                    source_offset=pos + m.start(), recovery_state="gap_carved",
                    time_source=TIME_SOURCE_UTC_ESTIMATED))
            if pos + len(chunk) >= end or len(chunk) < 4 * 1024 * 1024:
                break
            pos += len(chunk) - 256
    out.sort(key=lambda p: p.source_offset or 0)
    return out


def _float_or_none(value):
    try:
        return float(value) if value not in ("", None) else None
    except (TypeError, ValueError):
        return None


def describe_intervals(intervals: List[DamageInterval]) -> List[str]:
    return [f"{iv.start_sec:.1f}~{iv.end_sec:.1f}초: {iv.label}" + (f" - {iv.note}" if iv.note else "")
            + (f" (표시 행 한도 {MAX_PLACEHOLDERS:,}개를 넘어 이 구간의 행 일부를 만들지 않았습니다)"
               if iv.placeholder_limited else "")
            for iv in intervals]
