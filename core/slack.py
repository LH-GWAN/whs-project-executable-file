"""슬랙 데이터(컨테이너가 참조하지 않는 영역에서 카빙한 옛 녹화 GPS)를 화면용 궤적으로.

슬랙 기록에는 영상 재생 시각이 없다(sample table 밖이라 byte offset만 있다). 지도·그래프·표가
쓰는 시간축은 **슬랙 첫 기록의 GPS 시각 기준 경과 초**로 만든다. 시각이 없는 행은 뒤에 붙이고
시간축에는 올리지 않는다. 본 궤적과는 절대 합치지 않는다(과거 녹화분).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import List, Optional

from core import gpstime
from engine.engine_adapter import TrackPoint


@dataclass
class SlackSet:
    label: str                  # "슬랙" 또는 "video2 슬랙"
    source_label: str           # 어느 영상의 슬랙인지 ("", "video2")
    points: List[TrackPoint] = field(default_factory=list)   # 시간축이 매겨진 순서
    dates: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        coords = sum(1 for p in self.points if p.has_fix)
        text = f"GPS 기록 {len(self.points)}건 (좌표 {coords}개)"
        if self.dates:
            text += f" · 기록일 {', '.join(self.dates[:4])}" + (" …" if len(self.dates) > 4 else "")
        return text


def _utc_of(p: TrackPoint) -> Optional[_dt.datetime]:
    d, t = gpstime.parse_date(p.gps_date), gpstime.parse_time(p.gps_utc_time)
    if d is None or t is None:
        return None
    return _dt.datetime.combine(d, t, tzinfo=_dt.timezone.utc)


def slack_track(raw_points: List[TrackPoint]) -> List[TrackPoint]:
    """GPS 시각순으로 세우고 첫 기록 기준 경과 초를 start_time_sec에 넣는다."""
    timed = [(utc, p) for p in raw_points if (utc := _utc_of(p)) is not None]
    untimed = [p for p in raw_points if _utc_of(p) is None]
    timed.sort(key=lambda x: x[0])
    out: List[TrackPoint] = []
    base = timed[0][0] if timed else None
    for utc, p in timed:
        p.start_time_sec = (utc - base).total_seconds()
        p.end_time_sec = None
        out.append(p)
    for p in untimed:
        p.start_time_sec = None
        out.append(p)
    return out


def build_slack_set(raw_points: List[TrackPoint], source_label: str = "") -> Optional[SlackSet]:
    if not raw_points:
        return None
    points = slack_track(list(raw_points))
    dates = sorted({p.gps_date for p in points if p.gps_date})
    label = f"{source_label} 슬랙" if source_label else "슬랙"
    return SlackSet(label=label, source_label=source_label, points=points, dates=dates)
