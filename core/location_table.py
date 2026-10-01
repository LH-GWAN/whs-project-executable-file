"""Location Analysis 표의 칸 글자 - 화면 표와 CSV 내보내기가 같은 글자를 쓴다(CSV는 화면 표 그대로).

화면 표의 색·툴팁·링크는 ui/location_tab.py가 입히고, 여기서는 글자만 만든다.
"""
from __future__ import annotations

import csv
from typing import List, Optional, Sequence

from core.driving_events import DrivingEvent, events_by_row
from engine.engine_adapter import TrackPoint

COLUMNS = ["시각(초)", "위도", "경도", "속도(km/h)", "위험운전", "충격(g)", "지도", "GPS 검증"]
COL_TIME, COL_LAT, COL_LON, COL_SPEED, COL_EVENT, COL_G, COL_LINK, COL_CHECK = range(8)
SEGMENT_HEADER = "영상"
LINK_TEXT = "지도에서 보기"


def validation_text(rec: TrackPoint) -> str:
    if rec.gps_checksum_ok is False or rec.gps_trusted is False:
        return "실패"
    return "정상" if rec.gps_checksum_ok is True else "미제공"


def row_texts(rec: TrackPoint, here: Sequence[DrivingEvent]) -> List[str]:
    """COLUMNS 순서의 칸 글자."""
    failed = rec.gps_checksum_ok is False or rec.gps_trusted is False
    time_text = f"{rec.start_time_sec:.2f}" if rec.start_time_sec is not None else "-"
    if rec.is_outlier:
        lat, lon = "(이상치)", "(이상치)"
    elif failed:
        lat, lon = "(검증 실패)", "-"
    elif rec.has_fix:
        lat, lon = f"{rec.latitude:.6f}", f"{rec.longitude:.6f}"
    elif rec.is_dropout:
        lat, lon = "(GPS 끊김)", "-"
    else:
        lat, lon = "(GPS 없음)", "-"
    if failed:
        speed = "(검증 실패)"
    elif rec.is_outlier:
        speed = "(이상치)"
    else:
        speed = f"{rec.speed_kmh:.1f}" if rec.speed_kmh is not None else "-"
    g = rec.g_magnitude
    return [
        time_text, lat, lon, speed,
        ", ".join(ev.label for ev in here),
        f"{g:.2f}" if g is not None else "-",
        LINK_TEXT if rec.has_fix else "-",
        validation_text(rec),
    ]


def csv_table(points: List[TrackPoint], events: List[DrivingEvent],
              segment_labels: Optional[Sequence[str]] = None):
    """(머리글, 행들). 화면 표와 같은 칸이되 '지도' 링크 칸은 뺀다. 이어보기면 앞에 영상 칸."""
    keep = [i for i in range(len(COLUMNS)) if i != COL_LINK]
    header = ([SEGMENT_HEADER] if segment_labels else []) + [COLUMNS[i] for i in keep]
    rows = []
    for rec, here in zip(points, events_by_row(events, len(points))):
        cells = row_texts(rec, here)
        row = [cells[i] for i in keep]
        if segment_labels:
            idx = rec.segment_index
            row.insert(0, segment_labels[idx] if 0 <= idx < len(segment_labels) else "")
        rows.append(row)
    return header, rows


def write_csv(path: str, points: List[TrackPoint], events: List[DrivingEvent],
              segment_labels: Optional[Sequence[str]] = None) -> int:
    """UTF-8(BOM) CSV로 쓴다 - 엑셀에서 한글이 깨지지 않는다. 쓴 행 수를 돌려준다."""
    header, rows = csv_table(points, events, segment_labels)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return len(rows)
