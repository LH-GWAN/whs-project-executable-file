from __future__ import annotations

from typing import List

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.acceleration import FlaggedSegment, count_by_kind
from engine.engine_adapter import TrackPoint
from ui.speed_chart_widget import SpeedChartWidget


class SpeedTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._chart = SpeedChartWidget()

        self._avg_label = QLabel("평균 속도: -")
        self._max_label = QLabel("최고 속도: -")
        self._flag_label = QLabel("급가·감속 의심 구간: 0개")
        legend = QLabel("■ 급가속 의심 구간")
        legend.setStyleSheet("color: #cc3333;")
        legend_decel = QLabel("■ 급감속 의심 구간")
        legend_decel.setStyleSheet("color: #e08a00;")

        stats_row = QHBoxLayout()
        stats_row.addWidget(self._avg_label)
        stats_row.addWidget(self._max_label)
        stats_row.addWidget(self._flag_label)
        stats_row.addStretch(1)
        stats_row.addWidget(legend)
        stats_row.addSpacing(10)
        stats_row.addWidget(legend_decel)

        layout = QVBoxLayout(self)
        layout.addWidget(self._chart, 1)
        layout.addLayout(stats_row)

    def load(self, records: List[TrackPoint], segments: List[FlaggedSegment]) -> None:
        self._chart.set_data(records, segments)
        speeds = [r.speed_kmh for r in records if r.speed_kmh is not None]
        if speeds:
            self._avg_label.setText(f"평균 속도: {sum(speeds) / len(speeds):.1f} km/h")
            self._max_label.setText(f"최고 속도: {max(speeds):.1f} km/h")
        else:
            self._avg_label.setText("평균 속도: -")
            self._max_label.setText("최고 속도: -")
        accel_n, decel_n = count_by_kind(segments)
        self._flag_label.setText(f"급가·감속 의심 구간: {len(segments)}개 (급가속 {accel_n} · 급감속 {decel_n})")

    def grab_chart_png(self):
        return self._chart.grab_png()
