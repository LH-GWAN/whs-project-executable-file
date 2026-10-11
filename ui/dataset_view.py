"""Speed/Location 탭이 보여 줄 데이터 묶음. 이어보기면 Composed(이어 붙인 전체) + 영상별 묶음이 있다."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PySide6.QtWidgets import QTabBar

COMPOSED_LABEL = "Composed"


@dataclass
class DatasetView:
    label: str
    points: list
    events: list
    # 속도 그래프의 영상 경계 (시각, 영상 이름). Composed에만 있다.
    boundaries: List[Tuple[float, str]] = field(default_factory=list)
    # Location 표의 "영상" 칸에 쓸 영상 이름(TrackPoint.segment_index 순서). Composed에만 있다.
    segment_labels: Optional[List[str]] = None
    # 지도에 올릴 행이 표의 행과 다를 때(슬랙: 시각 없는 행은 표에만). None이면 points 그대로.
    map_points: Optional[list] = None
    # 슬랙 묶음: 위험운전을 판정하지 않고 시간축이 영상이 아니다(리뷰 #119).
    is_slack: bool = False
    # 손상 파일: 구간별 안내 문구("12.0~15.0초: 손상으로 인한 임의복구"). 없으면 빈 목록.
    damage_notes: List[str] = field(default_factory=list)
    # 영상별 묶음: 이 묶음의 첫 행이 Composed(리포트 '#')에서 몇 번째인지(0부터). Composed·슬랙은 0.
    # 표의 행 번호를 리포트 번호와 맞추는 데 쓴다(리뷰 #119).
    row_offset: int = 0


def make_view_bar(on_change) -> QTabBar:
    """탭 위쪽의 "Composed | video1 | video2 …" 줄. 영상이 하나면 숨긴다."""
    bar = QTabBar()
    bar.setExpanding(False)
    bar.setDrawBase(False)
    bar.currentChanged.connect(on_change)
    bar.hide()
    return bar


def fill_view_bar(bar: QTabBar, views: List[DatasetView]) -> None:
    bar.blockSignals(True)
    while bar.count():
        bar.removeTab(0)
    for view in views:
        bar.addTab(view.label)
    bar.setCurrentIndex(0)
    bar.blockSignals(False)
    bar.setVisible(len(views) > 1)
