from __future__ import annotations

from typing import List

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.acceleration import _distinct_fix_indices
from core.driving_events import (DEFAULT_VEHICLE, EVENT_COLORS, EVENT_LABELS, SPEED_EVENT_KINDS,
                                 DrivingEvent, criteria_lines, summarize_counts, vehicle_label)
from engine.engine_adapter import TrackPoint
from ui.dataset_view import COMPOSED_LABEL, DatasetView, fill_view_bar, make_view_bar
from ui.speed_chart_widget import SpeedChartWidget


def _speed_only(events: List[DrivingEvent]) -> List[DrivingEvent]:
    return [ev for ev in events if ev.is_speed_event]


class SpeedTab(QWidget):
    # 보고 있는 묶음이 바뀌었을 때(0 = Composed 또는 영상 하나짜리 사건, 1부터 video1…)
    view_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chart = SpeedChartWidget()
        self._views: List[DatasetView] = []
        self._vehicle_type = DEFAULT_VEHICLE
        self._view_bar = make_view_bar(self._show_view)

        self._avg_label = QLabel("평균 속도: -")
        self._max_label = QLabel("최고 속도: -")
        self._flag_label = QLabel("위험운전: -")

        stats_row = QHBoxLayout()
        stats_row.addWidget(self._avg_label)
        stats_row.addWidget(self._max_label)
        stats_row.addWidget(self._flag_label)
        stats_row.addStretch(1)
        # 이 탭은 속도 변화로 정해지는 넷(급가속·급출발·급감속·급정지)만 그린다 - 그 구간의 속도
        # 선 색이 바뀐다. 방향 계열(급진로변경·급회전·급U턴)은 지도와 Location 표에서 본다.
        for kind in SPEED_EVENT_KINDS:
            legend = QLabel(f"━ {EVENT_LABELS[kind]}")
            legend.setStyleSheet(f"color: {EVENT_COLORS[kind]}; font-weight: bold;")
            stats_row.addSpacing(8)
            stats_row.addWidget(legend)

        layout = QVBoxLayout(self)
        layout.addWidget(self._view_bar)
        layout.addWidget(self._chart, 1)
        layout.addLayout(stats_row)

    def load(self, records: List[TrackPoint], events: List[DrivingEvent],
             vehicle_type: str = DEFAULT_VEHICLE) -> None:
        self.load_views([DatasetView(COMPOSED_LABEL, records, events)], vehicle_type)

    def load_views(self, views: List[DatasetView], vehicle_type: str = DEFAULT_VEHICLE) -> None:
        """views[0]이 Composed(이어 붙인 전체), 그 뒤가 영상별. 영상이 하나면 views는 하나."""
        self._views = list(views)
        self._vehicle_type = vehicle_type
        fill_view_bar(self._view_bar, self._views)
        self._show_view(0)

    def current_view(self) -> int:
        return max(0, self._view_bar.currentIndex()) if self._views else 0

    def _show_view(self, index: int) -> None:
        if not (0 <= index < len(self._views)):
            return
        view = self._views[index]
        records = view.points
        speed_events = _speed_only(view.events)
        self._chart.set_data(records, speed_events, view.boundaries)
        indices = _distinct_fix_indices(records)
        speeds = [records[i].speed_kmh for i in indices]
        excluded = sum(r.speed_kmh is not None for r in records) - len(indices)
        self._avg_label.setToolTip(
            f"그래프와 같은 유효 GPS 측정의 산술평균. 반복·무효 속도 행 {excluded}개 제외 (시간 가중 평균 아님).")
        if speeds:
            self._avg_label.setText(f"평균 속도: {sum(speeds) / len(speeds):.1f} km/h")
            self._max_label.setText(f"최고 속도: {max(speeds):.1f} km/h")
        else:
            self._avg_label.setText("평균 속도: -")
            self._max_label.setText("최고 속도: -")
        self._flag_label.setText(
            f"위험운전({vehicle_label(self._vehicle_type)} 기준): "
            f"{summarize_counts(speed_events, SPEED_EVENT_KINDS)}")
        self._flag_label.setToolTip("\n".join(criteria_lines(self._vehicle_type)[:5]))
        self.view_changed.emit(index)

    def grab_chart_png(self):
        """리포트용 그래프. 영상별 묶음을 보고 있어도 Composed(전체)로 그린다."""
        if not self._views or self.current_view() == 0:
            return self._chart.grab_png()
        composed = self._views[0]
        current = self._views[self.current_view()]
        self._chart.set_data(composed.points, _speed_only(composed.events), composed.boundaries)
        try:
            return self._chart.grab_png()
        finally:
            self._chart.set_data(current.points, _speed_only(current.events), current.boundaries)
