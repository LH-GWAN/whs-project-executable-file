from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
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
                                 COL_SPEED, COLUMNS, IMPACT_G, SEGMENT_HEADER, gps_slot_rows,
                                 has_frame_detail, row_texts)
from engine.engine_adapter import TrackPoint
from ui.address_resolver import AddressResolver
from ui.dataset_view import COMPOSED_LABEL, DatasetView, fill_view_bar, make_view_bar
from ui.map_view import MapView, is_synthetic_point

_EVENT_ROW_ALPHA = 60   # 위험운전 행 배경(종류별 색을 옅게)
_OUTLIER_COLOR = QColor(200, 110, 0)
_LINK_COLOR = QColor(30, 100, 200)
_EVENT_COLUMN = COL_EVENT
_MAP_LINK_COLUMN = COL_LINK
# 이어보기의 "영상" 칸. 논리 번호는 맨 뒤(기존 칸 번호를 그대로 두려고)지만 화면에서는 맨 앞에 둔다.
_SEGMENT_COLUMN = len(COLUMNS)
_DROPOUT_COLOR = QColor(190, 110, 40)
_IMPACT_COLOR = QColor(200, 60, 60)
_IMPACT_G = IMPACT_G   # 충격 행 기준은 표·리포트와 같은 값(리뷰 #113)


class LocationTab(QWidget):
    # 보고 있는 묶음이 바뀌었을 때(0 = Composed 또는 영상 하나짜리 사건, 1부터 video1…)
    view_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._map = MapView()
        self._views: List[DatasetView] = []
        self._view_bar = make_view_bar(self._show_view)
        # 프레임·G센서 단위로 행을 쓰는 영상은 기본으로 1초(GPS 기록)마다 한 행만 보인다.
        # 상세보기를 켜면 모든 행이 나온다(검토 의견: 기본은 1초, 필요할 때만 프레임 단위).
        self._detail_btn = QPushButton("상세보기 (프레임 단위)")
        self._detail_btn.setCheckable(True)
        self._detail_btn.setToolTip("이 영상은 GPS보다 자주 행을 기록합니다. 켜면 모든 행(프레임·G센서 단위)을,\n"
                                    "끄면 GPS 기록마다 한 행(1초 단위)만 보입니다.")
        self._detail_btn.toggled.connect(lambda _on: self._show_view(self.current_view()))
        self._detail_btn.hide()
        self._row_index: List[int] = []   # 표 행 → 지점 번호
        self._table = QTableWidget(0, len(COLUMNS) + 1)
        self._table.setHorizontalHeaderLabels(COLUMNS + [SEGMENT_HEADER])
        self._table.horizontalHeader().moveSection(_SEGMENT_COLUMN, 0)
        self._table.setColumnHidden(_SEGMENT_COLUMN, True)
        self._table.horizontalHeaderItem(_EVENT_COLUMN).setToolTip(
            "선택한 차종 기준(국토부 DTG 위험운전행동 판별 기준)으로 판정한 급가속·급출발·급감속·급정지.\n"
            "방향 계열(급진로변경·급회전·급U턴)과 과속은 판정하지 않습니다.\n판정에 쓰인 측정 구간의 모든 행에 표시합니다.")
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
        # 손상 파일: 어느 구간이 손상이고 어떻게 처리했는지(임의복구/복구하지 않음) 표 위에 적는다.
        self._damage_label = QLabel("")
        self._damage_label.setWordWrap(True)
        self._damage_label.setStyleSheet("color: #c92a2a; padding: 2px 4px;")
        self._damage_label.hide()
        table_layout.addWidget(self._damage_label)
        table_layout.addWidget(self._selected_label)
        table_layout.addWidget(self._table, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._map)
        splitter.addWidget(table_panel)
        splitter.setSizes([500, 500])

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 4, 0)
        top.addWidget(self._view_bar, 1)
        top.addWidget(self._detail_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)

    def map_view(self) -> MapView:
        return self._map

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        if column != _MAP_LINK_COLUMN or not (0 <= row < len(self._row_index)):
            return
        point = self._points[self._row_index[row]]
        if not point.has_fix or is_synthetic_point(point):
            return   # 추정 좌표는 측정값이 아니라 외부 지도로 열지 않는다(리뷰 #21)
        QDesktopServices.openUrl(QUrl(external_map_url(point.latitude, point.longitude)))

    def grab_map_png(self):
        return self._map.grab_png()

    def ensure_map_loaded(self) -> None:
        self._map.ensure_loaded()

    def _on_row_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return
        row = rows[0].row()
        if 0 <= row < len(self._row_index):
            index = self._row_index[row]
            point = self._points[index]
            if point.start_time_sec is not None:
                self._map.set_playback_time(point.start_time_sec)
            self._show_selected(index, point)

    def _row_number(self, index: int) -> int:
        """표 세로 머리글·선택 줄의 번호. 영상별 묶음도 리포트 '#'와 같은 전체 번호(리뷰 #119, #129)."""
        offset = self._views[self.current_view()].row_offset if self._views else 0
        return offset + index + 1

    def _show_selected(self, index: int, point: TrackPoint) -> None:
        number = self._row_number(index)
        if point.recovery_state == "damaged":
            self._selected_label.setText(f"#{number}: 좌표 없음 (손상 구간)")
            self._selected_key = None
            return
        if not point.has_fix:
            self._selected_label.setText(f"#{number}: 좌표 없음")
            self._selected_key = None
            return
        if is_synthetic_point(point):
            # 임의 복구의 추정 좌표: 측정값이 아니므로 표시만 하고 주소 조회(외부 전송)는 하지 않는다(리뷰 #21).
            self._selected_label.setText(f"#{number}: {point.latitude:.6f}, {point.longitude:.6f} (추정 - 임의 복구, 측정값 아님)")
            self._selected_key = None
            return
        base = f"#{number}: {point.latitude:.6f}, {point.longitude:.6f}"
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
        self._resolver.request(point.latitude, point.longitude, owner="location")

    def _on_address_resolved(self, lat: float, lon: float, address: str) -> None:
        if self._selected_key is None or geocode.cache_key(lat, lon) != self._selected_key:
            return
        text = self._selected_label.text().split("  (", 1)[0]
        self._selected_label.setText(f"{text}  ({address})" if address else text)

    def load(self, records: List[TrackPoint], events: List[DrivingEvent]) -> None:
        self.load_views([DatasetView(COMPOSED_LABEL, records, events)])

    def load_views(self, views: List[DatasetView]) -> None:
        """views[0]이 Composed(이어 붙인 전체 - 지도는 이어진 궤적, 표는 video1→video2… 순), 그 뒤가
        영상별. 영상이 하나면 views는 하나. 빈 목록이면 표·지도를 비운다(끈 탭에 이전 사건이 남지 않게)."""
        self._views = list(views)
        fill_view_bar(self._view_bar, self._views)
        if not self._views:
            self._points = []
            self._row_index = []
            self._table.setRowCount(0)
            self._map.set_track([], [])
            self._selected_label.setText("")
            self._detail_btn.hide()
            return
        self._show_view(0)

    def current_view(self) -> int:
        return max(0, self._view_bar.currentIndex()) if self._views else 0

    def _show_view(self, index: int) -> None:
        if not (0 <= index < len(self._views)):
            return
        view = self._views[index]
        records, events = view.points, view.events
        self._points = records
        self._map.set_track(view.map_points if view.map_points is not None else records, events,
                            baseline=(index == 0))
        self._selected_label.setText("")
        self._selected_key = None
        if view.damage_notes:
            self._damage_label.setText("손상 구간: " + " / ".join(view.damage_notes))
            self._damage_label.show()
        else:
            self._damage_label.hide()
        self._table.setColumnHidden(_SEGMENT_COLUMN, not view.segment_labels)

        detail = has_frame_detail(records)
        self._detail_btn.setVisible(detail)
        self._row_index = list(range(len(records))) if (not detail or self._detail_btn.isChecked()) \
            else gps_slot_rows(records)
        row_events = events_by_row(events, len(records))
        self._table.setRowCount(len(self._row_index))
        for row, index in enumerate(self._row_index):
            rec = records[index]
            here = row_events[index]
            texts = row_texts(rec, here)
            # 1초 보기·영상별 묶음에서도 세로 머리글은 리포트 '#'와 같은 전체 지점 번호(리뷰 #129, #119).
            self._table.setVerticalHeaderItem(row, QTableWidgetItem(str(view.row_offset + index + 1)))
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
            elif rec.recovery_state == "damaged":
                for it in items:
                    it.setForeground(QColor("#c92a2a"))
                    it.setToolTip("손상 구간: 파일의 이 부분을 읽을 수 없어 기록이 없습니다.")
            elif rec.recovery_state == "interpolated":
                for it in (lat_item, lon_item, items[COL_CHECK]):
                    it.setForeground(QColor("#9c36b5"))
                    it.setToolTip("임의 복구: 손상 직전 좌표와 손상 뒤 첫 좌표를 직선으로 이은 추정값입니다. 측정값이 아닙니다.")
            g = rec.g_magnitude
            if g is not None and g >= _IMPACT_G:
                items[COL_G].setForeground(_IMPACT_COLOR)
            # 위경도만 보면 어디인지 바로 알기 어려워서, 외부 지도로 바로 열 수 있는
            # 칸을 둔다. 클릭하면 기본 브라우저에서 해당 좌표가 열린다.
            if rec.has_fix and not is_synthetic_point(rec):
                items[COL_LINK].setForeground(_LINK_COLOR)
                items[COL_LINK].setToolTip("클릭하면 브라우저에서 이 좌표를 엽니다")
            elif rec.has_fix:
                items[COL_LINK].setToolTip("추정 좌표(임의 복구)는 측정값이 아니라 외부 지도로 열지 않습니다")
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
