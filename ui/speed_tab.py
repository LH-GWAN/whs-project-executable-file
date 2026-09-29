from __future__ import annotations

from typing import List

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.acceleration import _distinct_fix_indices
from core.driving_events import (DEFAULT_VEHICLE, EVENT_COLORS, EVENT_LABELS, SPEED_EVENT_KINDS,
                                 DrivingEvent, criteria_lines, summarize_counts, vehicle_label)
from engine.engine_adapter import TrackPoint
from ui.speed_chart_widget import SpeedChartWidget


class SpeedTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._chart = SpeedChartWidget()

        self._avg_label = QLabel("평균 속도: -")
        self._max_label = QLabel("최고 속도: -")
        self._flag_label = QLabel("위험운전: -")

        stats_row = QHBoxLayout()
        stats_row.addWidget(self._avg_label)
        stats_row.addWidget(self._max_label)
        stats_row.addWidget(self._flag_label)
        stats_row.addStretch(1)
        # 이 탭은 속도 변화로 정해지는 넷(급가속·급출발·급감속·급정지)만 그린다.
        # 방향 계열(급진로변경·급회전·급U턴)은 지도와 Location 표에서 본다.
        for kind in SPEED_EVENT_KINDS:
            legend = QLabel(f"■ {EVENT_LABELS[kind]}")
            legend.setStyleSheet(f"color: {EVENT_COLORS[kind]};")
            stats_row.addSpacing(8)
            stats_row.addWidget(legend)

        layout = QVBoxLayout(self)
        layout.addWidget(self._chart, 1)
        layout.addLayout(stats_row)

    def load(self, records: List[TrackPoint], events: List[DrivingEvent],
             vehicle_type: str = DEFAULT_VEHICLE) -> None:
        speed_events = [ev for ev in events if ev.is_speed_event]
        self._chart.set_data(records, speed_events)
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
            f"위험운전({vehicle_label(vehicle_type)} 기준): "
            f"{summarize_counts(speed_events, SPEED_EVENT_KINDS)}")
        self._flag_label.setToolTip("\n".join(criteria_lines(vehicle_type)[:5]))

    def grab_chart_png(self):
        return self._chart.grab_png()
