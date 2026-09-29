from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import geocode
from core.driving_events import DrivingEvent, events_by_row
from core.geocode import external_map_url
from core.location_table import (COL_CHECK, COL_EVENT, COL_G, COL_LAT, COL_LINK, COL_LON,
                                 COL_SPEED, COLUMNS, SEGMENT_HEADER, row_texts)
from engine.engine_adapter import TrackPoint
from ui.address_resolver import AddressResolver
from ui.dataset_view import COMPOSED_LABEL, DatasetView, fill_view_bar, make_view_bar
from ui.map_view import MapView

_EVENT_ROW_ALPHA = 60   # 위험운전 행 배경(종류별 색을 옅게)
_OUTLIER_COLOR = QColor(200, 110, 0)
_LINK_COLOR = QColor(30, 100, 200)
_EVENT_COLUMN = COL_EVENT
_MAP_LINK_COLUMN = COL_LINK
# 이어보기의 "영상" 칸. 논리 번호는 맨 뒤(기존 칸 번호를 그대로 두려고)지만 화면에서는 맨 앞에 둔다.
_SEGMENT_COLUMN = len(COLUMNS)
_DROPOUT_COLOR = QColor(190, 110, 40)
_NOGPS_COLOR = QColor(170, 170, 170)
_IMPACT_COLOR = QColor(200, 60, 60)
_IMPACT_G = 2.0


class LocationTab(QWidget):
    # 보고 있는 묶음이 바뀌었을 때(0 = Composed 또는 영상 하나짜리 사건, 1부터 video1…)
    view_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._map = MapView()
        self._views: List[DatasetView] = []
        self._view_bar = make_view_bar(self._show_view)
        self._table = QTableWidget(0, len(COLUMNS) + 1)
        self._table.setHorizontalHeaderLabels(COLUMNS + [SEGMENT_HEADER])
        self._table.horizontalHeader().moveSection(_SEGMENT_COLUMN, 0)
        self._table.setColumnHidden(_SEGMENT_COLUMN, True)
        self._table.horizontalHeaderItem(_EVENT_COLUMN).setToolTip(
            "선택한 차종 기준(국토부 DTG 위험운전행동 판별 기준)으로 판정한 급가속·급출발·급감속·"
            "급정지·급진로변경·급좌/우회전·급U턴.\n판정에 쓰인 측정 구간의 모든 행에 표시합니다.")
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view_bar)
        layout.addWidget(splitter, 1)

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

    def load(self, records: List[TrackPoint], events: List[DrivingEvent]) -> None:
        self.load_views([DatasetView(COMPOSED_LABEL, records, events)])

    def load_views(self, views: List[DatasetView]) -> None:
        """views[0]이 Composed(이어 붙인 전체 - 지도는 이어진 궤적, 표는 video1→video2… 순), 그 뒤가
        영상별. 영상이 하나면 views는 하나."""
        self._views = list(views)
        fill_view_bar(self._view_bar, self._views)
        self._show_view(0)

    def current_view(self) -> int:
        return max(0, self._view_bar.currentIndex()) if self._views else 0

    def _show_view(self, index: int) -> None:
        if not (0 <= index < len(self._views)):
            return
        view = self._views[index]
        records, events = view.points, view.events
        self._points = records
        self._map.set_track(records, events)
        self._selected_label.setText("")
        self._selected_key = None
        self._table.setColumnHidden(_SEGMENT_COLUMN, not view.segment_labels)

        row_events = events_by_row(events, len(records))
        self._table.setRowCount(len(records))
        for row, rec in enumerate(records):
            here = row_events[row]
            texts = row_texts(rec, here)
            items = [QTableWidgetItem(t) for t in texts]
            lat_item, lon_item = items[COL_LAT], items[COL_LON]
            if rec.is_outlier:
                # 값은 지우지 않는다 - 툴팁에 원본과 판정 사유를 남긴다.
                tip = (f"원본 값: {rec.latitude:.6f}, {rec.longitude:.6f}"
                       + (f"\n속도 {rec.speed_kmh:.1f} km/h" if rec.speed_kmh is not None else "")
                       + f"\n판정: {rec.outlier_reason}")
                for it in (lat_item, lon_item, items[COL_SPEED]):
                    it.setForeground(_OUTLIER_COLOR)
                    it.setToolTip(tip)
            elif texts[COL_LAT] == "(GPS 끊김)":
                lat_item.setForeground(_DROPOUT_COLOR)
                lon_item.setForeground(_DROPOUT_COLOR)
            elif texts[COL_LAT] == "(GPS 없음)":
                lat_item.setForeground(_NOGPS_COLOR)
                lon_item.setForeground(_NOGPS_COLOR)
            g = rec.g_magnitude
            if g is not None and g >= _IMPACT_G:
                items[COL_G].setForeground(_IMPACT_COLOR)
            # 위경도만 보면 어디인지 바로 알기 어려워서, 외부 지도로 바로 열 수 있는
            # 칸을 둔다. 클릭하면 기본 브라우저에서 해당 좌표가 열린다.
            if rec.has_fix:
                items[COL_LINK].setForeground(_LINK_COLOR)
                items[COL_LINK].setToolTip("클릭하면 브라우저에서 이 좌표를 엽니다")
            items[COL_CHECK].setToolTip("실패 레코드는 지도·속도 통계·위험운전 판정에서 제외합니다. 원본 CSV는 보존합니다.")
            if here:
                font = items[COL_EVENT].font()
                font.setBold(True)
                items[COL_EVENT].setFont(font)
                items[COL_EVENT].setToolTip("\n".join(
                    f"{ev.label}: {ev.detail} "
                    f"({ev.start_time_sec:.1f}~{ev.end_time_sec:.1f}초)" for ev in here))
            labels = view.segment_labels or []
            idx = rec.segment_index
            items.append(QTableWidgetItem(labels[idx] if 0 <= idx < len(labels) else ""))
            if here:
                color = QColor(here[0].color)
                color.setAlpha(_EVENT_ROW_ALPHA)
                for item in items:
                    item.setBackground(color)
            for col, item in enumerate(items):
                self._table.setItem(row, col, item)
        self.view_changed.emit(index)
