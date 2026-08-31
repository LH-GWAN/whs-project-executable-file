from __future__ import annotations

from typing import List

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from core.acceleration import FlaggedSegment
from engine.engine_adapter import TrackPoint

_BG = QColor("#0d1117")
_LINE = QColor("#3ddc97")
_BAND = QColor(255, 60, 60, 90)
_AXIS = QColor("#9aa4ad")


class SpeedChartWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self._records: List[TrackPoint] = []
        self._segments: List[FlaggedSegment] = []

    def set_data(self, records: List[TrackPoint], segments: List[FlaggedSegment]) -> None:
        self._records = records
        self._segments = segments
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), _BG)

        speeds = [r.speed_kmh for r in self._records if r.speed_kmh is not None]
        if not self._records or not speeds:
            painter.setPen(_AXIS)
            painter.drawText(self.rect(), Qt.AlignCenter, "표시할 속도 데이터가 없습니다.")
            painter.end()
            return

        plot = self.rect().adjusted(45, 12, -12, -24)
        max_speed = max(speeds) or 1.0
        n = len(self._records)

        times = [r.start_time_sec for r in self._records if r.start_time_sec is not None]
        t0, t1 = (min(times), max(times)) if times else (0.0, 0.0)
        span = (t1 - t0) or 1.0

        def x_for(i: int) -> float:
            t = self._records[i].start_time_sec if 0 <= i < n else None
            if t is None or not times:
                return plot.left() + plot.width() * (i / max(1, n - 1))
            return plot.left() + plot.width() * ((t - t0) / span)

        def y_for(v: float) -> float:
            return plot.bottom() - plot.height() * (v / max_speed)

        painter.setPen(Qt.NoPen)
        painter.setBrush(_BAND)
        for seg in self._segments:
            x0 = x_for(seg.start_index)
            x1 = x_for(seg.end_index)
            painter.drawRect(QRectF(x0, plot.top(), max(2.0, x1 - x0), plot.height()))

        # drawPath는 현재 브러시로 경로 내부까지 칠한다. 위에서 급가속 구간을 칠하려고
        # 세워둔 브러시를 그대로 두면 속도 곡선 아래가 통째로 빨갛게 채워진다.
        painter.setBrush(Qt.NoBrush)

        path = QPainterPath()
        started = False
        saw_dropout = False
        for i, r in enumerate(self._records):
            if r.speed_kmh is None:
                if r.is_dropout:
                    saw_dropout = True
                continue
            x, y = x_for(i), y_for(r.speed_kmh)
            if not started or saw_dropout:
                path.moveTo(x, y)
                started = True
            else:
                path.lineTo(x, y)
            saw_dropout = False
        painter.setPen(QPen(_LINE, 2))
        painter.drawPath(path)

        painter.setPen(_AXIS)
        painter.drawText(4, int(plot.top()) + 10, f"{max_speed:.0f} km/h")
        painter.drawText(4, int(plot.bottom()), "0")
        if times:
            painter.drawText(plot.left(), self.rect().bottom() - 6, f"{t0:.0f}s")
            painter.drawText(plot.right() - 30, self.rect().bottom() - 6, f"{t1:.0f}s")
        painter.end()
