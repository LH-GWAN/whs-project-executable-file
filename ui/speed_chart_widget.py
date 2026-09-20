from __future__ import annotations

import math
from typing import List, Optional, Tuple

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from core import gpstime
from core.acceleration import FlaggedSegment, _distinct_fix_indices, compute_point_accelerations
from engine.engine_adapter import TrackPoint

_BG = QColor("#0d1117")
_LINE = QColor("#3ddc97")
_DOT = QColor("#2f8f6c")
_BAND = QColor(255, 60, 60, 90)          # 급가속
_BAND_DECEL = QColor(255, 150, 30, 95)   # 급감속
_AXIS = QColor("#9aa4ad")
# 눈금선. 처음엔 알파 22로 그렸더니 검토에서 "선이 안 나온다"는 말이 나왔다 - 검은 배경 위에
# 9% 흰색은 화면에서도 리포트 이미지에서도 사실상 보이지 않는다.
_GRID_MAJOR = QColor(255, 255, 255, 85)
_GRID_MINOR = QColor(255, 255, 255, 38)
_GRID_Y = QColor(255, 255, 255, 45)

_Y_PADDING_RATIO = 0.18

# 마우스를 점 가까이 대면 그 측정값을 말풍선으로 보여준다.
HOVER_RADIUS_PX = 12
_HOVER_RING = QColor("#ffffff")
_BUBBLE_BG = QColor(22, 30, 40, 235)
_BUBBLE_BORDER = QColor("#5b6b7a")
_BUBBLE_TEXT = QColor("#e8edf2")

# 시간 눈금 간격. 블랙박스 영상은 대개 20초~2분이라 10초 단위가 기본이고(사이에 5초 보조선),
# 더 길면 눈금이 10개 안쪽이 되는 간격을 고른다.
_LONG_TICK_STEPS = (20, 30, 60, 120, 300, 600, 900, 1800, 3600)


def _time_tick_step(span_sec: float) -> float:
    if span_sec <= 10:
        return 1.0 if span_sec <= 5 else 2.0
    if span_sec <= 100:
        return 10.0
    for step in _LONG_TICK_STEPS:
        if span_sec / step <= 10:
            return float(step)
    return float(_LONG_TICK_STEPS[-1])


def _fmt_tick(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    m, sec = divmod(total, 60)
    if m < 60:
        return f"{m}:{sec:02d}"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}"


class SpeedChartWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setMouseTracking(True)
        self._records: List[TrackPoint] = []
        self._segments: List[FlaggedSegment] = []
        self._accels: List[Optional[float]] = []
        # 마지막으로 화면에 그린 점들의 위치 (원본 인덱스, x, y). 마우스 위치와 맞춰 본다.
        self._screen_pts: List[Tuple[int, float, float]] = []
        self._hover: Optional[int] = None

    def set_data(self, records: List[TrackPoint], segments: List[FlaggedSegment]) -> None:
        self._records = records
        self._segments = segments
        self._accels = compute_point_accelerations(records)
        self._hover = None
        self._screen_pts = []
        self.update()

    # ---------- 마우스 올린 점의 정보 ----------
    def hovered_index(self) -> Optional[int]:
        return self._hover

    def hover_lines(self, index: int) -> List[str]:
        """말풍선에 들어갈 줄들. 영상 시각·GPS 시각·속도·가속도·좌표·급가감속 구간."""
        if not (0 <= index < len(self._records)):
            return []
        r = self._records[index]
        lines: List[str] = []
        if r.start_time_sec is not None:
            total = int(round(r.start_time_sec))
            m, sec = divmod(total, 60)
            lines.append(f"영상 {m:02d}:{sec:02d}")
        clock = gpstime.format_point(r)
        if clock:
            lines.append(f"GPS 시각 {clock}")
        lines.append(f"속도 {r.speed_kmh:.1f} km/h" if r.speed_kmh is not None else "속도 -")
        accel = self._accels[index] if index < len(self._accels) else None
        lines.append(f"가속도 {accel:+.2f} m/s²" if accel is not None else "가속도 - (직전 측정 없음)")
        if r.latitude is not None and r.longitude is not None:
            lines.append(f"위치 {r.latitude:.6f}, {r.longitude:.6f}")
        for seg in self._segments:
            if seg.start_index <= index <= seg.end_index:
                lines.append(f"{seg.label} 의심 구간 (최대 {seg.max_acceleration_mps2:+.2f} m/s²)")
                break
        return lines

    def _nearest_index(self, x: float, y: float) -> Optional[int]:
        best, best_d2 = None, float(HOVER_RADIUS_PX * HOVER_RADIUS_PX)
        for index, px, py in self._screen_pts:
            d2 = (px - x) ** 2 + (py - y) ** 2
            if d2 <= best_d2:
                best, best_d2 = index, d2
        return best

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        found = self._nearest_index(pos.x(), pos.y())
        if found != self._hover:
            self._hover = found
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def grab_png(self, width: int = 900, height: int = 300) -> Optional[bytes]:
        """그래프를 PNG로 캡처한다. 화면 크기와 무관하게 리포트용 크기로 그린다."""
        if len(self._plot_points()) < 2:
            return None
        image = QImage(width, height, QImage.Format_ARGB32)
        image.fill(_BG)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint_to(painter, QRect(0, 0, width, height))
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if not image.save(buffer, "PNG"):
            return None
        return bytes(buffer.data())

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
        self._paint_to(painter, self.rect(), record_positions=True)
        self._paint_hover(painter)
        painter.end()

    def _paint_to(self, painter, area, record_positions: bool = False) -> None:
        """화면과 리포트 이미지가 같은 코드로 그려지도록 분리했다.
        area만 다르고 나머지는 동일하다. record_positions는 화면에 그릴 때만 켜서
        마우스 판정용 점 위치를 남긴다(리포트 이미지 크기로 남기면 어긋난다)."""
        pts = self._plot_points()
        if record_positions:
            self._screen_pts = []
        if len(pts) < 2:
            painter.setPen(_AXIS)
            painter.drawText(area, Qt.AlignCenter, "표시할 속도 데이터가 없습니다.")
            return

        plot = area.adjusted(52, 16, -16, -26)

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

        painter.setPen(QPen(_GRID_Y, 1))
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = plot.bottom() - plot.height() * frac
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))

        # 시간 눈금. 시작/끝만 있으면 중간 지점이 몇 초인지 읽을 수 없다.
        # 주 눈금(10초 등)은 선명한 실선 + 라벨, 보조 눈금(그 절반)은 흐린 점선.
        tick_step = _time_tick_step(span_x)
        minor_step = tick_step / 2.0
        t = minor_step * math.ceil(t0 / minor_step - 1e-9)
        while t <= t1 + 1e-6:
            x = x_for_time(t)
            is_major = abs(t / tick_step - round(t / tick_step)) < 1e-6
            if is_major:
                painter.setPen(QPen(_GRID_MAJOR, 1))
                painter.drawLine(int(x), int(plot.top()), int(x), int(plot.bottom()))
                painter.setPen(_AXIS)
                painter.drawText(int(x) - 14, area.bottom() - 6, _fmt_tick(t))
            else:
                pen = QPen(_GRID_MINOR, 1)
                pen.setStyle(Qt.DashLine)
                painter.setPen(pen)
                painter.drawLine(int(x), int(plot.top()), int(x), int(plot.bottom()))
            t += minor_step

        painter.setPen(Qt.NoPen)
        for seg in self._segments:
            painter.setBrush(_BAND_DECEL if getattr(seg, "is_decel", False) else _BAND)
            x0 = x_for_index(seg.start_index)
            x1 = x_for_index(seg.end_index)
            painter.drawRect(QRectF(x0, plot.top(), max(2.0, x1 - x0), plot.height()))

        # drawPath는 현재 브러시로 경로 내부까지 칠한다. 위에서 급가속 구간을 칠하려고
        # 세워둔 브러시를 그대로 두면 속도 곡선 아래가 통째로 빨갛게 채워진다.
        painter.setBrush(Qt.NoBrush)

        painter.setPen(QPen(_LINE, 2))
        painter.drawPath(self._build_path(pts, x_for_time, y_for))

        # 실측 지점을 점으로 남긴다. 곡선은 이 점들을 정확히 지나며 그 사이만 부드럽게
        # 이은 것이라, 점이 곧 실제 측정값이다.
        if record_positions:
            self._screen_pts = [(i, x_for_time(t), y_for(v)) for i, t, v in pts]
        if len(pts) <= 400:
            painter.setPen(Qt.NoPen)
            painter.setBrush(_DOT)
            for _, t, v in pts:
                painter.drawEllipse(QPointF(x_for_time(t), y_for(v)), 2.2, 2.2)
            painter.setBrush(Qt.NoBrush)

        painter.setPen(_AXIS)
        painter.drawText(4, int(plot.top()) + 10, f"{hi:.0f} km/h")
        painter.drawText(4, int(plot.bottom()) + 4, f"{lo:.0f} km/h")

    def _paint_hover(self, painter) -> None:
        """마우스를 올린 점을 고리로 강조하고 옆에 말풍선을 그린다. 화면에만 그리고
        리포트 이미지에는 넣지 않는다."""
        if self._hover is None:
            return
        pos = next(((x, y) for i, x, y in self._screen_pts if i == self._hover), None)
        if pos is None:
            return
        lines = self.hover_lines(self._hover)
        if not lines:
            return
        px, py = pos
        painter.setPen(QPen(_HOVER_RING, 2))
        painter.setBrush(_LINE)
        painter.drawEllipse(QPointF(px, py), 4.5, 4.5)
        painter.setBrush(Qt.NoBrush)

        fm = painter.fontMetrics()
        pad, gap = 8, 3
        width = max(fm.horizontalAdvance(line) for line in lines) + pad * 2
        height = len(lines) * fm.height() + (len(lines) - 1) * gap + pad * 2
        # 기본은 점의 오른쪽 위. 화면 밖으로 나가면 왼쪽/아래로 뒤집는다.
        x = px + 14
        if x + width > self.width() - 4:
            x = px - 14 - width
        y = py - height - 10
        if y < 4:
            y = py + 12
        x = max(4, x)
        painter.setPen(QPen(_BUBBLE_BORDER, 1))
        painter.setBrush(_BUBBLE_BG)
        painter.drawRoundedRect(QRectF(x, y, width, height), 5, 5)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(_BUBBLE_TEXT)
        ty = y + pad + fm.ascent()
        for line in lines:
            painter.drawText(int(x + pad), int(ty), line)
            ty += fm.height() + gap

    def _build_path(self, pts, x_for_time, y_for) -> QPainterPath:
        """실측 지점들을 **정확히 지나는** 부드러운 곡선으로 잇는다.

        단조 3차 보간(Fritsch-Carlson)을 쓴다. 곡선이 모든 측정점을 통과하고, 두 점
        사이에서는 두 값의 범위를 벗어나지 않는다(오버슈트 없음) - 없는 값을 지어내지
        않으면서 꺾임만 둥글게 만든다. 예전의 "구간 중점을 지나는 2차 베지에"는 점을
        살짝 비켜 가서 "점과 선이 왜 다르냐"는 검토 의견이 나왔다.
        GPS 수신이 끊긴 구간에서는 선을 끊는다(그 사이 속도를 모르므로).
        """
        path = QPainterPath()
        segment: List[QPointF] = []
        prev_index: Optional[int] = None

        def flush(points: List[QPointF]) -> None:
            if not points:
                return
            path.moveTo(points[0])
            if len(points) == 1:
                path.lineTo(points[0].x() + 0.1, points[0].y())
                return
            for p1, c1, c2, p2 in monotone_cubic_segments(points):
                if c1 is None:
                    path.lineTo(p2)
                else:
                    path.cubicTo(c1, c2, p2)

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


def monotone_cubic_segments(points: List[QPointF]):
    """(시작점, 제어점1, 제어점2, 끝점) 목록. x가 같은 이웃(dt=0)은 직선(제어점 None)으로 잇는다.

    Fritsch-Carlson 접선으로 만든 3차 에르미트 곡선을 베지에 제어점으로 바꾼다.
    """
    n = len(points)
    if n < 2:
        return []
    xs = [p.x() for p in points]
    ys = [p.y() for p in points]
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [((ys[i + 1] - ys[i]) / h[i]) if h[i] > 1e-9 else 0.0 for i in range(n - 1)]

    m = [0.0] * n
    m[0] = d[0]
    m[-1] = d[-1]
    for i in range(1, n - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0.0  # 극점(최고/최저)에서는 접선을 눕혀 오버슈트를 막는다
        else:
            w1 = 2 * h[i] + h[i - 1]
            w2 = h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    # 단조 조건(alpha^2 + beta^2 <= 9) 보정
    for i in range(n - 1):
        if abs(d[i]) < 1e-12:
            m[i] = 0.0
            m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        s2 = a * a + b * b
        if s2 > 9.0:
            tau = 3.0 / math.sqrt(s2)
            m[i] = tau * a * d[i]
            m[i + 1] = tau * b * d[i]

    out = []
    for i in range(n - 1):
        p1, p2 = points[i], points[i + 1]
        if h[i] <= 1e-9:
            out.append((p1, None, None, p2))
            continue
        c1 = QPointF(xs[i] + h[i] / 3.0, ys[i] + m[i] * h[i] / 3.0)
        c2 = QPointF(xs[i + 1] - h[i] / 3.0, ys[i + 1] - m[i + 1] * h[i] / 3.0)
        out.append((p1, c1, c2, p2))
    return out
