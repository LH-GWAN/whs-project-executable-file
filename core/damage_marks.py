"""손상 구간을 시간축에 올리고(표시용 빈 행), 선택하면 임의 복구(앞뒤 좌표를 잇는 추정 좌표)를 넣는다.

사용자 결정(2026-10-11)
  - 기본은 '복구하지 않음'(추천): 손상 구간은 빈 행으로 두고 지도·그래프·표에 손상으로 표시한다.
  - '임의 복구': 손상 직전 좌표와 손상 뒤 첫 좌표를 1초 간격으로 직선 보간해 넣는다. 지도에는 다른 색으로,
    표에는 '임의 복구'로 표시하고 속도는 만들지 않는다(그래프는 그 구간을 비운다).
  - 너무 멀리 갔으면 복구 불가: 손상 직전 속도 × 잘린 시간 = 예상 이동 거리, 실제 두 좌표 거리가
    예상 × 1.5 + 50 m를 넘으면 임의 복구를 하지 않고 안내한다. 직전에 정지 중(3 km/h 미만)이면 그 앞
    60초 안의 마지막 움직이던 속도를 기준으로 한다.
  - 영상은 원본을 그대로 재생한다(시간축 유지).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

from core.damage import DamageReport
from core.outliers import haversine_m
from engine.engine_adapter import TrackPoint

MODE_NONE = "none"
MODE_INTERPOLATE = "interpolate"
MODE_LABELS = {MODE_NONE: "복구하지 않음", MODE_INTERPOLATE: "임의 복구"}

PLAUSIBLE_FACTOR = 1.5
PLAUSIBLE_SLACK_M = 50.0
MOVING_KMH = 3.0
LOOKBACK_SEC = 60.0
MIN_INTERVAL_SEC = 1.0
MAX_PLACEHOLDERS = 20_000


@dataclass
class DamageInterval:
    start_sec: float
    end_sec: float
    kind: str                 # "gap" / "zero" / "truncated" / "head"
    outcome: str = ""         # "interpolated" / "implausible" / "not_recovered" / "no_anchor"
    note: str = ""
    expected_m: Optional[float] = None
    actual_m: Optional[float] = None

    @property
    def label(self) -> str:
        names = {"interpolated": "손상으로 인한 임의복구", "implausible": "손상 - 복구 불가(이동 거리 비정상)",
                 "not_recovered": "손상으로 복구하지 않음", "no_anchor": "손상 - 복구 불가(앞뒤 좌표 없음)"}
        return names.get(self.outcome, "손상")

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_times_from_utc(points: List[TrackPoint]) -> bool:
    """재생 시각이 하나도 없고 GPS UTC는 있는 경우(헤더 소실 AVI): 첫 UTC를 0초로 두고 시각을 추정한다.
    자정을 넘기면 하루를 더한다. 추정했으면 True."""
    if any(p.start_time_sec is not None for p in points):
        return False
    stamped = [(i, _utc_seconds(p.gps_utc_time)) for i, p in enumerate(points)]
    stamped = [(i, u) for i, u in stamped if u is not None]
    if len(stamped) < 2:
        return False
    first = stamped[0][1]
    last_u = first
    base_offset = 0.0
    assigned = {}
    for i, u in stamped:
        if u + base_offset < last_u - 43200:
            base_offset += 86400.0
        t = u + base_offset - first
        last_u = u + base_offset
        assigned[i] = t
    # UTC가 없는 행(같은 측정의 반복 행)은 바로 앞 시각을 따른다.
    current = 0.0
    for i, p in enumerate(points):
        if i in assigned:
            current = assigned[i]
        p.start_time_sec = current
        p.end_time_sec = current + 1.0
        p.time_source = "gps_utc_estimated"
        p.time_estimated = True
    points.sort(key=lambda p: p.start_time_sec)
    return True


def _utc_seconds(text: str) -> Optional[float]:
    if not text:
        return None
    digits = text.replace(":", "")
    try:
        return int(digits[0:2]) * 3600 + int(digits[2:4]) * 60 + float(digits[4:] or 0)
    except ValueError:
        return None


def compute_intervals(points: List[TrackPoint], report: DamageReport,
                      duration_sec: Optional[float]) -> List[DamageInterval]:
    """바이트 구간을 재생 시각 구간으로 옮긴다: 구간 앞의 마지막 기록 시각 ~ 구간 뒤의 첫 기록 시각."""
    timed = [p for p in points if p.start_time_sec is not None and p.source_offset is not None
             and not p.recovery_state]
    timed.sort(key=lambda p: p.source_offset)
    offsets = [p.source_offset for p in timed]
    out: List[DamageInterval] = []
    for a, b in report.regions():
        kind = "gap" if [a, b] in report.gaps or any(ga <= a < gb for ga, gb in report.gaps) else "zero"
        before = _last_before(timed, offsets, a)
        after = _first_at_or_after(timed, offsets, b)
        start = before.start_time_sec if before else 0.0
        if after is not None:
            end = after.start_time_sec
        elif duration_sec:
            end = duration_sec
        else:
            end = (max(p.start_time_sec for p in timed) if timed else start)
        if end - start >= MIN_INTERVAL_SEC:
            out.append(DamageInterval(round(start, 3), round(end, 3), kind if before else "head"))
    if report.truncated and timed and duration_sec:
        last = max(p.start_time_sec for p in timed)
        if duration_sec - last >= 2.0 and not any(abs(iv.end_sec - duration_sec) < 1e-6 for iv in out):
            out.append(DamageInterval(round(last, 3), round(duration_sec, 3), "truncated"))
    out.sort(key=lambda iv: iv.start_sec)
    return _merge(out)


def _merge(items: List[DamageInterval]) -> List[DamageInterval]:
    merged: List[DamageInterval] = []
    for iv in items:
        if merged and iv.start_sec <= merged[-1].end_sec + 0.5:
            merged[-1].end_sec = max(merged[-1].end_sec, iv.end_sec)
        else:
            merged.append(iv)
    return merged


def _last_before(timed, offsets, offset):
    import bisect
    i = bisect.bisect_left(offsets, offset) - 1
    return timed[i] if i >= 0 else None


def _first_at_or_after(timed, offsets, offset):
    import bisect
    i = bisect.bisect_left(offsets, offset)
    return timed[i] if i < len(timed) else None


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


def apply_damage_marks(points: List[TrackPoint], intervals: List[DamageInterval], mode: str) -> List[TrackPoint]:
    """구간마다 빈 행(복구 안 함) 또는 추정 좌표 행(임의 복구)을 끼워 넣는다. 원본 행은 바꾸지 않는다.
    돌려주는 목록은 시각순이다. intervals의 outcome/note를 채운다."""
    base = [p for p in points if not p.recovery_state or p.recovery_state == "gap_carved"]
    extra: List[TrackPoint] = []
    budget = MAX_PLACEHOLDERS
    for iv in intervals:
        before = _last_fix_before(base, iv.start_sec)
        after = _first_fix_after(base, iv.end_sec)
        segment = before.segment_index if before else (after.segment_index if after else 0)
        if mode == MODE_INTERPOLATE and before is not None and after is not None:
            dt = max(after.start_time_sec - before.start_time_sec, 0.001)
            v_ref = _reference_speed_kmh(base, before)
            expected = v_ref / 3.6 * dt
            actual = haversine_m(before.latitude, before.longitude, after.latitude, after.longitude)
            iv.expected_m, iv.actual_m = round(expected, 1), round(actual, 1)
            if actual > PLAUSIBLE_FACTOR * expected + PLAUSIBLE_SLACK_M:
                iv.outcome = "implausible"
                iv.note = (f"앞뒤 좌표 거리 {actual:.0f} m가 예상 이동 거리 {expected:.0f} m(직전 속도 "
                           f"{v_ref:.0f} km/h × {dt:.0f}초)의 1.5배+50 m를 넘어 임의 복구를 하지 않았습니다.")
                extra.extend(_placeholders(iv, segment, budget))
            else:
                iv.outcome = "interpolated"
                iv.note = (f"직전 좌표와 다음 좌표를 직선으로 이었습니다 (거리 {actual:.0f} m, 예상 {expected:.0f} m). "
                           "속도는 만들지 않았습니다.")
                extra.extend(_interpolated(before, after, segment, budget))
        elif mode == MODE_INTERPOLATE:
            iv.outcome = "no_anchor"
            iv.note = "손상 앞이나 뒤에 좌표가 없어 이을 수 없습니다."
            extra.extend(_placeholders(iv, segment, budget))
        else:
            iv.outcome = "not_recovered"
            iv.note = "손상 구간의 기록은 비워 두었습니다."
            extra.extend(_placeholders(iv, segment, budget))
        budget -= len(extra)
        if budget <= 0:
            break
    merged = base + extra
    merged.sort(key=lambda p: (p.start_time_sec is None, p.start_time_sec or 0.0))
    return merged


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


def _placeholders(iv: DamageInterval, segment: int, budget: int) -> List[TrackPoint]:
    out = []
    t = math.floor(iv.start_sec) + 1.0
    while t < iv.end_sec and len(out) < budget:
        out.append(TrackPoint(start_time_sec=t, end_time_sec=t + 1.0, time_source="damage",
                              recovery_state="damaged", segment_index=segment))
        t += 1.0
    return out


def _interpolated(before: TrackPoint, after: TrackPoint, segment: int, budget: int) -> List[TrackPoint]:
    out = []
    t0, t1 = before.start_time_sec, after.start_time_sec
    t = math.floor(t0) + 1.0
    while t < t1 and len(out) < budget:
        f = (t - t0) / (t1 - t0)
        out.append(TrackPoint(
            start_time_sec=t, end_time_sec=t + 1.0, time_source="interpolated",
            latitude=before.latitude + (after.latitude - before.latitude) * f,
            longitude=before.longitude + (after.longitude - before.longitude) * f,
            speed_kmh=None, recovery_state="interpolated", segment_index=segment))
        t += 1.0
    return out


def carve_whole_file_gps(path: str, start: int = 0, end: Optional[int] = None,
                         cancel_event=None) -> List[TrackPoint]:
    """moov(샘플 표)가 사라진 일반 MP4: 미디어 영역 전체에서 NMEA RMC 문장을 찾아 좌표 행을 만든다.
    시각은 없고(뒤에서 UTC로 추정) 파일 위치만 남긴다. 같은 (날짜, UTC)는 한 번만."""
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
                raise InterruptedError("cancelled")
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
                    gps_checksum_ok=True, gps_trusted=bool(parsed.get("trusted", True)),
                    source_offset=pos + m.start(), recovery_state="gap_carved", time_source="gps_utc_estimated"))
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
            for iv in intervals]
