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
    def map_points(self) -> List[TrackPoint]:
        """지도에 올릴 행: 시각이 있는 행만(시각 없는 행은 어느 녹화인지 몰라 표에만 둔다)."""
        return [p for p in self.points if p.start_time_sec is not None]

    @property
    def recordings(self) -> int:
        return len({p.segment_index for p in self.points if p.start_time_sec is not None})

    @property
    def summary(self) -> str:
        coords = sum(1 for p in self.points if p.has_fix)
        text = f"GPS 기록 {len(self.points)}건 (좌표 {coords}개, 녹화 {self.recordings}묶음)"
        if self.dates:
            text += f" · 기록일 {', '.join(self.dates[:4])}" + (" …" if len(self.dates) > 4 else "") + " (UTC+9)"
        return text


def _utc_of(p: TrackPoint) -> Optional[_dt.datetime]:
    d, t = gpstime.parse_date(p.gps_date), gpstime.parse_time(p.gps_utc_time)
    if d is None or t is None:
        return None
    return _dt.datetime.combine(d, t, tzinfo=_dt.timezone.utc)


# 슬랙 기록 사이가 이만큼 벌어지면 다른 녹화로 본다(지도·그래프에서 잇지 않는다).
RECORDING_GAP_SEC = 120.0


def slack_track(raw_points: List[TrackPoint]) -> List[TrackPoint]:
    """GPS 시각순으로 세우고 첫 기록 기준 경과 초를 start_time_sec에 넣는다. 시각이 2분 넘게 비거나
    날짜가 바뀌면 segment_index를 올려 다른 녹화로 나눈다 - 2023년 서울 기록과 2024년 부산 기록이
    실선 하나로 이어지던 문제(리뷰 #12). 녹화마다 이상치 판정도 거친다(리뷰 #33)."""
    from core.outliers import mark_outliers

    timed = [(utc, p) for p in raw_points if (utc := _utc_of(p)) is not None]
    untimed = [p for p in raw_points if _utc_of(p) is None]
    timed.sort(key=lambda x: x[0])
    out: List[TrackPoint] = []
    base = timed[0][0] if timed else None
    segment = 0
    prev_utc = None
    for utc, p in timed:
        if prev_utc is not None and ((utc - prev_utc).total_seconds() > RECORDING_GAP_SEC
                                     or utc.date() != prev_utc.date()):
            segment += 1
        p.start_time_sec = (utc - base).total_seconds()
        p.end_time_sec = None
        p.segment_index = segment
        out.append(p)
        prev_utc = utc
    for seg in range(segment + 1):
        mark_outliers([p for p in out if p.segment_index == seg])
    for p in untimed:
        p.start_time_sec = None
        p.segment_index = segment + 1
        out.append(p)
    return out


def build_slack_set(raw_points: List[TrackPoint], source_label: str = "") -> Optional[SlackSet]:
    if not raw_points:
        return None
    points = slack_track(list(raw_points))
    # 기록일은 한국 시간 날짜로(UTC 날짜 그대로면 새벽 기록이 하루 앞으로 보인다, 리뷰 #137)
    dates = sorted({utc.astimezone(gpstime._TZ).strftime("%Y-%m-%d") for p in points
                    if (utc := _utc_of(p)) is not None})
    label = f"{source_label} 슬랙" if source_label else "슬랙"
    return SlackSet(label=label, source_label=source_label, points=points, dates=dates)
