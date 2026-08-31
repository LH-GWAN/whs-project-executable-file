from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from core.acceleration import FlaggedSegment, _distinct_fix_indices
from engine.engine_adapter import TrackPoint

_BG = QColor("#0d1117")
_LINE = QColor("#3ddc97")
_DOT = QColor("#2f8f6c")
_BAND = QColor(255, 60, 60, 90)
_AXIS = QColor("#9aa4ad")
_GRID = QColor(255, 255, 255, 22)

_Y_PADDING_RATIO = 0.18


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

    def _plot_points(self) -> List[Tuple[int, float, float]]:
        """(원본 인덱스, 시각, 속도) 목록.

        같은 GPS 측정값이 반복 기록된 행은 걷어낸다. 기기가 GPS 갱신 주기보다 훨씬
        빠르게 레코드를 쓰면(VUGERA는 초당 31행) 같은 속도가 수십 번 반복되다가 한
        번에 뛰어서, 그대로 그리면 실제로는 완만한 주행이 계단처럼 보인다.
        """
        out: List[Tuple[int, float, float]] = []
        for i in _distinct_fix_indices(self._records):
            r = self._records[i]
            if r.speed_kmh is None or r.start_time_sec is None:
                continue
            out.append((i, r.start_time_sec, r.speed_kmh))
        return out

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), _BG)

        pts = self._plot_points()
        if len(pts) < 2:
            painter.setPen(_AXIS)
            painter.drawText(self.rect(), Qt.AlignCenter, "표시할 속도 데이터가 없습니다.")
            painter.end()
            return

        plot = self.rect().adjusted(52, 16, -16, -26)

        speeds = [v for _, _, v in pts]
        lo_raw, hi_raw = min(speeds), max(speeds)
        # 0부터 최고속도까지 다 그리면 시속 100km 근처 주행은 선이 위쪽에 붙어버린다.
        # 실제 속도 범위에 여유를 둬서 그래프가 화면 가운데를 지나가게 한다.
        pad = max((hi_raw - lo_raw) * _Y_PADDING_RATIO, 2.0)
        lo, hi = max(0.0, lo_raw - pad), hi_raw + pad
        span_y = (hi - lo) or 1.0

        times = [t for _, t, _ in pts]
        t0, t1 = min(times), max(times)
        span_x = (t1 - t0) or 1.0

        def x_for_time(t: float) -> float:
            return plot.left() + plot.width() * ((t - t0) / span_x)

        def x_for_index(i: int) -> float:
            r = self._records[i] if 0 <= i < len(self._records) else None
            t = r.start_time_sec if r is not None else None
            if t is None:
                return plot.left()
            return x_for_time(t)

        def y_for(v: float) -> float:
            return plot.bottom() - plot.height() * ((v - lo) / span_y)

        painter.setPen(QPen(_GRID, 1))
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = plot.bottom() - plot.height() * frac
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))

        painter.setPen(Qt.NoPen)
        painter.setBrush(_BAND)
        for seg in self._segments:
            x0 = x_for_index(seg.start_index)
            x1 = x_for_index(seg.end_index)
            painter.drawRect(QRectF(x0, plot.top(), max(2.0, x1 - x0), plot.height()))

        # drawPath는 현재 브러시로 경로 내부까지 칠한다. 위에서 급가속 구간을 칠하려고
        # 세워둔 브러시를 그대로 두면 속도 곡선 아래가 통째로 빨갛게 채워진다.
        painter.setBrush(Qt.NoBrush)

        painter.setPen(QPen(_LINE, 2))
        painter.drawPath(self._build_path(pts, x_for_time, y_for))

        # 실측 지점을 점으로 남긴다. 곡선은 점 사이를 부드럽게 이은 것일 뿐이고
        # 실제 측정이 어디였는지 눈으로 확인할 수 있어야 한다.
        if len(pts) <= 400:
            painter.setPen(Qt.NoPen)
            painter.setBrush(_DOT)
            for _, t, v in pts:
                painter.drawEllipse(QPointF(x_for_time(t), y_for(v)), 2.2, 2.2)
            painter.setBrush(Qt.NoBrush)

        painter.setPen(_AXIS)
        painter.drawText(4, int(plot.top()) + 10, f"{hi:.0f} km/h")
        painter.drawText(4, int(plot.bottom()) + 4, f"{lo:.0f} km/h")
        painter.drawText(int(plot.left()), self.rect().bottom() - 6, f"{t0:.0f}s")
        painter.drawText(int(plot.right()) - 34, self.rect().bottom() - 6, f"{t1:.0f}s")
        painter.end()

    def _build_path(self, pts, x_for_time, y_for) -> QPainterPath:
        """실측 지점들을 부드러운 곡선으로 잇는다.

        각 구간의 중점을 지나는 2차 베지에를 쓴다 - 측정값 자체를 건드리지 않고
        꺾인 모서리만 둥글게 만드는 방식이라, 없는 값을 지어내지 않는다.
        GPS 수신이 끊긴 구간에서는 선을 끊는다(그 사이 속도를 모르므로).
        """
        path = QPainterPath()
        segment: List[QPointF] = []
        prev_index: Optional[int] = None

        def flush(points: List[QPointF]) -> None:
            if not points:
                return
            if len(points) == 1:
                path.moveTo(points[0])
                path.lineTo(points[0].x() + 0.1, points[0].y())
                return
            path.moveTo(points[0])
            if len(points) == 2:
                path.lineTo(points[1])
                return
            for i in range(1, len(points) - 1):
                mid = QPointF((points[i].x() + points[i + 1].x()) / 2.0,
                              (points[i].y() + points[i + 1].y()) / 2.0)
                path.quadTo(points[i], mid)
            path.lineTo(points[-1])

        for index, t, v in pts:
            if prev_index is not None:
                gap_has_dropout = any(
                    self._records[j].is_dropout
                    for j in range(prev_index + 1, index)
                )
                if gap_has_dropout:
                    flush(segment)
                    segment = []
            segment.append(QPointF(x_for_time(t), y_for(v)))
            prev_index = index

        flush(segment)
        return path
