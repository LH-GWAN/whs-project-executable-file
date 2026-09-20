"""GPS 기록 시각(UTC) → 화면 표시용 한국 시간.

NMEA RMC는 시각을 UTC로 싣는다(`gps_date` "2024-03-12", `gps_utc_time` "08:22:17.00").
엔진이 ISO 형식으로 바꿔 주지만, 변환에 실패한 원본(ddmmyy / hhmmss.ss)도 그대로 남길 수
있어 두 형식을 다 받는다. 표시는 한국 시간(UTC+9) 고정이고 뒤에 "(UTC+9)"를 붙여
UTC 원본과 헷갈리지 않게 한다 - 조사 보고에서 "영상 시각"이 어느 기준인지가 늘 문제라서.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional, Tuple

from engine.engine_adapter import TrackPoint

DISPLAY_UTC_OFFSET_HOURS = 9
DISPLAY_TZ_LABEL = f"UTC+{DISPLAY_UTC_OFFSET_HOURS}"
_TZ = _dt.timezone(_dt.timedelta(hours=DISPLAY_UTC_OFFSET_HOURS))

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DDMMYY_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})$")
_ISO_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?$")
_HHMMSS_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})(?:\.(\d+))?$")


def parse_date(text: str) -> Optional[_dt.date]:
    text = (text or "").strip()
    m = _ISO_DATE_RE.match(text)
    if m:
        y, mo, d = (int(g) for g in m.groups())
    else:
        m = _DDMMYY_RE.match(text)
        if not m:
            return None
        d, mo, yy = (int(g) for g in m.groups())
        y = 1900 + yy if yy >= 80 else 2000 + yy
    try:
        return _dt.date(y, mo, d)
    except ValueError:
        return None


def parse_time(text: str) -> Optional[_dt.time]:
    text = (text or "").strip()
    m = _ISO_TIME_RE.match(text) or _HHMMSS_RE.match(text)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        return None
    return _dt.time(h, mi, s)


def to_display(date_text: str, utc_time_text: str) -> Tuple[Optional[_dt.date], Optional[_dt.time]]:
    """UTC 날짜·시각을 한국 시간으로 옮긴 (날짜, 시각). 날짜가 없으면 시각만(자정을 넘겨도
    날짜를 모르니 시각만 돌린다), 시각이 없으면 (None, None)."""
    t = parse_time(utc_time_text)
    if t is None:
        return None, None
    d = parse_date(date_text)
    if d is None:
        shifted = (_dt.datetime.combine(_dt.date(2000, 1, 1), t) + _dt.timedelta(hours=DISPLAY_UTC_OFFSET_HOURS))
        return None, shifted.time()
    local = _dt.datetime.combine(d, t, tzinfo=_dt.timezone.utc).astimezone(_TZ)
    return local.date(), local.time()


def format_display(date_text: str, utc_time_text: str) -> str:
    """"2024-03-12 17:22:17 (UTC+9)" / 날짜 없으면 "17:22:17 (UTC+9)" / 시각 없으면 ""."""
    d, t = to_display(date_text, utc_time_text)
    if t is None:
        return ""
    if d is None:
        return f"{t.strftime('%H:%M:%S')} ({DISPLAY_TZ_LABEL})"
    return f"{d.isoformat()} {t.strftime('%H:%M:%S')} ({DISPLAY_TZ_LABEL})"


def format_point(point: Optional[TrackPoint]) -> str:
    if point is None:
        return ""
    return format_display(point.gps_date, point.gps_utc_time)
