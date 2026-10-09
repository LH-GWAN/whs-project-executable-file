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


def _is_gps_row(p: TrackPoint) -> bool:
    return p.has_gps_record or p.has_coords


def _gps_key(p: TrackPoint):
    return p.record_key()


def gps_slot_rows(points: List[TrackPoint]) -> List[int]:
    """GPS 기록이 바뀌는 행(기록 하나당 첫 행)의 번호 - "1초마다" 보기. 같은 기록을 반복해 쓴
    행(VUGERA는 초당 31행, FineVu 17행)과 G센서 전용 행(INAVI 0.1초)은 빠진다."""
    out: List[int] = []
    prev_key = None
    for i, p in enumerate(points):
        if not _is_gps_row(p):
            continue
        key = _gps_key(p)
        if key != prev_key:
            out.append(i)
            prev_key = key
    return out


def has_frame_detail(points: List[TrackPoint]) -> bool:
    """GPS 기록보다 훨씬 자주 행을 쓰는 영상(프레임·G센서 단위)인가. 그러면 Location 표는 기본으로
    1초 단위 행만 보이고 '상세보기'로 전체 행을 연다."""
    slots = len(gps_slot_rows(points))
    if slots == 0:
        return False   # GPS가 전혀 없는 G센서 전용 영상은 전체 행을 그대로 보인다(리뷰 #47)
    extra = len(points) - slots
    return extra >= 5 and len(points) >= 1.5 * slots


def validation_text(rec: TrackPoint) -> str:
    if rec.is_dropout and not rec.has_coords:
        return "끊김"   # 측위 실패(status=V) 행은 검증 실패가 아니라 수신 끊김이다(리뷰 #93)
    if rec.gps_checksum_ok is False or rec.gps_trusted is False:
        return "실패"
    return "정상" if rec.gps_checksum_ok is True else "미제공"


def row_texts(rec: TrackPoint, here: Sequence[DrivingEvent]) -> List[str]:
    """COLUMNS 순서의 칸 글자."""
    dropout = rec.is_dropout and not rec.has_coords
    failed = (rec.gps_checksum_ok is False or rec.gps_trusted is False) and not dropout
    time_text = f"{rec.start_time_sec:.2f}" if rec.start_time_sec is not None else "-"
    if rec.is_outlier:
        lat, lon = "(이상치)", "(이상치)"
    elif dropout:
        lat, lon = "(GPS 끊김)", "-"   # 좌표 없는 끊김은 검증 실패보다 먼저 본다(리뷰 #93)
    elif failed:
        lat, lon = "(검증 실패)", "-"
    elif rec.has_fix:
        lat, lon = f"{rec.latitude:.6f}", f"{rec.longitude:.6f}"
    else:
        lat, lon = "-", "-"   # GPS 기록이 없는 행(G센서 전용 등). 예전엔 "(GPS 없음)"이었다.
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
