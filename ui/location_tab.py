from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import geocode
from core.acceleration import FlaggedSegment
from core.geocode import external_map_url
from engine.engine_adapter import TrackPoint
from ui.address_resolver import AddressResolver
from ui.map_view import MapView

_FLAG_COLOR = QColor(255, 200, 200)
_LINK_COLOR = QColor(30, 100, 200)
_MAP_LINK_COLUMN = 5
_DROPOUT_COLOR = QColor(190, 110, 40)
_NOGPS_COLOR = QColor(170, 170, 170)
_IMPACT_COLOR = QColor(200, 60, 60)
_IMPACT_G = 2.0


class LocationTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._map = MapView()
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["시각(초)", "위도", "경도", "속도(km/h)", "충격(g)", "지도"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self._points: List[TrackPoint] = []

        # 고른 행의 주소. 표 전체에 주소 열을 두면 지점 수만큼(수천 건) 조회가 나가므로
        # 사용자가 고른 한 지점만 조회한다. 온라인 모드에서만 채워진다.
        self._selected_label = QLabel("")
        self._selected_label.setStyleSheet("color: #555; padding: 2px 4px;")
        self._selected_key = None
        self._resolver = AddressResolver.instance()
        self._resolver.resolved.connect(self._on_address_resolved)

        table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(2)
        table_layout.addWidget(self._selected_label)
        table_layout.addWidget(self._table, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._map)
        splitter.addWidget(table_panel)
        splitter.setSizes([500, 500])

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    def map_view(self) -> MapView:
        return self._map

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        if column != _MAP_LINK_COLUMN or not (0 <= row < len(self._points)):
            return
        point = self._points[row]
        if not point.has_fix:
            return
        QDesktopServices.openUrl(QUrl(external_map_url(point.latitude, point.longitude)))

    def grab_map_png(self):
        return self._map.grab_png()

    def ensure_map_loaded(self) -> None:
        self._map.ensure_loaded()

    def _on_row_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return
        index = rows[0].row()
        if 0 <= index < len(self._points):
            point = self._points[index]
            if point.start_time_sec is not None:
                self._map.set_playback_time(point.start_time_sec)
            self._show_selected(index, point)

    def _show_selected(self, index: int, point: TrackPoint) -> None:
        if not point.has_fix:
            self._selected_label.setText(f"#{index + 1}: 좌표 없음")
            self._selected_key = None
            return
        base = f"#{index + 1}: {point.latitude:.6f}, {point.longitude:.6f}"
        if not geocode.is_available():
            self._selected_label.setText(base)
            self._selected_key = None
            return
        self._selected_key = geocode.cache_key(point.latitude, point.longitude)
        cached = geocode.cached_address(point.latitude, point.longitude)
        if cached is not None:
            self._selected_label.setText(f"{base}  ({cached})" if cached else base)
            return
        self._selected_label.setText(f"{base}  (주소 조회 중…)")
        self._resolver.request(point.latitude, point.longitude)

    def _on_address_resolved(self, lat: float, lon: float, address: str) -> None:
        if self._selected_key is None or geocode.cache_key(lat, lon) != self._selected_key:
            return
        text = self._selected_label.text().split("  (", 1)[0]
        self._selected_label.setText(f"{text}  ({address})" if address else text)

    def load(self, records: List[TrackPoint], segments: List[FlaggedSegment]) -> None:
        self._points = records
        self._map.set_track(records, segments)
        self._selected_label.setText("")
        self._selected_key = None

        flagged_indices = set()
        for seg in segments:
            flagged_indices.update(range(seg.start_index, seg.end_index + 1))

        self._table.setRowCount(len(records))
        for row, rec in enumerate(records):
            time_item = QTableWidgetItem(
                f"{rec.start_time_sec:.2f}" if rec.start_time_sec is not None else "-")
            if rec.has_fix:
                lat_item = QTableWidgetItem(f"{rec.latitude:.6f}")
                lon_item = QTableWidgetItem(f"{rec.longitude:.6f}")
            elif rec.is_dropout:
                lat_item = QTableWidgetItem("(GPS 끊김)")
                lon_item = QTableWidgetItem("-")
                lat_item.setForeground(_DROPOUT_COLOR)
                lon_item.setForeground(_DROPOUT_COLOR)
            else:
                lat_item = QTableWidgetItem("(GPS 없음)")
                lon_item = QTableWidgetItem("-")
                lat_item.setForeground(_NOGPS_COLOR)
                lon_item.setForeground(_NOGPS_COLOR)
            speed_item = QTableWidgetItem(
                f"{rec.speed_kmh:.1f}" if rec.speed_kmh is not None else "-")
            g = rec.g_magnitude
            g_item = QTableWidgetItem(f"{g:.2f}" if g is not None else "-")
            if g is not None and g >= _IMPACT_G:
                g_item.setForeground(_IMPACT_COLOR)
            # 위경도만 보면 어디인지 바로 알기 어려워서, 외부 지도로 바로 열 수 있는
            # 칸을 둔다. 클릭하면 기본 브라우저에서 해당 좌표가 열린다.
            if rec.has_fix:
                link_item = QTableWidgetItem("지도에서 보기")
                link_item.setForeground(_LINK_COLOR)
                link_item.setToolTip("클릭하면 브라우저에서 이 좌표를 엽니다")
            else:
                link_item = QTableWidgetItem("-")
            items = (time_item, lat_item, lon_item, speed_item, g_item, link_item)
            if row in flagged_indices:
                for item in items:
                    item.setBackground(_FLAG_COLOR)
            for col, item in enumerate(items):
                self._table.setItem(row, col, item)
