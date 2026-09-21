from __future__ import annotations

import os

from typing import Optional

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.appinfo import APP_NAME
from core.video_tracks import view_tag
from core.pipeline import PipelineResult
from ui.location_tab import LocationTab
from ui.speed_tab import SpeedTab
from ui.tracker_tab import TrackerTab


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def _format_duration(seconds) -> str:
    if seconds is None:
        return "알 수 없음"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def make_settings_button(menu: QMenu, parent=None) -> QToolButton:
    """화면 우측 상단의 '⚙ 설정' 버튼. 메뉴바는 눈에 안 띄어 찾기 어렵다는 피드백으로 옮겼다."""
    button = QToolButton(parent)
    button.setText("⚙ 설정")
    button.setProperty("role", "settings")
    button.setPopupMode(QToolButton.InstantPopup)
    button.setToolButtonStyle(Qt.ToolButtonTextOnly)
    button.setMenu(menu)
    button.setCursor(Qt.PointingHandCursor)
    button.setToolTip("지도 사용 방식, 온라인 지도 키, 오프라인 지도 파일 안내")
    return button


def _vertical_separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.VLine)
    line.setProperty("role", "vsep")
    line.setFixedHeight(14)
    return line


class _HashPopup(QFrame):
    """해시 전체 값을 보여주는 작은 상자. 바깥을 클릭하면 닫힌다(웹의 팝오버처럼)."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("HashPopup")
        self._label = QLabel("", self)
        self._label.setProperty("role", "hash")
        self._label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._note = QLabel("클립보드에 복사됨 · 바깥을 클릭하면 닫힙니다", self)
        self._note.setProperty("role", "info-key")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addWidget(QLabel("SHA-256", self))
        layout.addWidget(self._label)
        layout.addWidget(self._note)

    def show_for(self, anchor: QWidget, text: str) -> None:
        self._label.setText(text)
        self.adjustSize()
        self.move(anchor.mapToGlobal(anchor.rect().bottomLeft()) + QPoint(0, 4))
        self.show()

    @property
    def text(self) -> str:
        return self._label.text()


class _HashLabel(QLabel):
    """해시는 앞 16자만 보이고, 마우스를 올려도 아무것도 뜨지 않는다. 클릭하면 전체 값이
    작은 상자로 뜨고(바깥 클릭으로 닫힘) 클립보드에도 복사된다."""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self._full = ""
        self._popup: Optional[_HashPopup] = None
        self.setProperty("role", "hash")
        self.setCursor(Qt.PointingHandCursor)

    def set_hash(self, sha256: str) -> None:
        self._full = sha256 or ""
        self.setText(f"{self._full[:16]}…" if len(self._full) > 16 else (self._full or "-"))

    def mousePressEvent(self, event):  # noqa: N802
        if self._full:
            QGuiApplication.clipboard().setText(self._full)
            if self._popup is None:
                self._popup = _HashPopup(self.window())
            self._popup.show_for(self, self._full)
        super().mousePressEvent(event)


class AnalysisView(QWidget):
    home_requested = Signal()
    report_requested = Signal(object)
    # 온라인 지도(카카오맵)를 띄우지 못했을 때 (원인 종류, 서버 메시지). 두 탭의 지도가
    # 각각 내는 신호를 하나로 모은다. 안내는 MainWindow가 한다.
    online_map_failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result: PipelineResult | None = None

        title = QLabel(APP_NAME)
        title.setProperty("role", "title")
        self._case_label = QLabel("")

        report_btn = QPushButton("Report")
        report_btn.setProperty("role", "primary")
        report_btn.clicked.connect(self._on_report_clicked)
        home_btn = QPushButton("Home")
        home_btn.setProperty("role", "primary")
        home_btn.clicked.connect(self.home_requested.emit)

        header = QWidget()
        header.setObjectName("HeaderBar")
        header_layout = QHBoxLayout(header)
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(self._case_label)
        header_layout.addWidget(report_btn)
        header_layout.addWidget(home_btn)

        # 파일 정보 줄: 항목마다 일정한 간격과 구분선을 두고, 이름표는 흐리게, 값은 진하게.
        self._file_badge = QLabel("-")
        self._file_badge.setProperty("role", "badge")
        self._file_name_label = QLabel("")
        self._file_size_label = QLabel("")
        self._duration_label = QLabel("")
        self._hash_label = _HashLabel()
        # 원본과 사본의 해시가 같은지 알려주는 작은 불. 사본을 다시 읽어 비교하므로 잠깐
        # 회색이었다가 초록(일치)/빨강(불일치)이 된다. 사본이 없으면 회색으로 남는다.
        self._integrity_dot = QLabel("●")
        self._integrity_dot.setProperty("role", "integrity")
        self._integrity_dot.setToolTip("원본과 사본 해시 비교 대기 중")
        self._integrity_worker = None
        self._set_integrity("pending", "")

        file_info = QWidget()
        file_info.setObjectName("FileInfoBar")
        file_info_layout = QHBoxLayout(file_info)
        file_info_layout.setContentsMargins(12, 6, 12, 6)
        file_info_layout.setSpacing(14)

        def add_item(key_text, value_widget, first=False):
            if not first:
                file_info_layout.addWidget(_vertical_separator())
            if key_text:
                key = QLabel(key_text)
                key.setProperty("role", "info-key")
                file_info_layout.addWidget(key)
            file_info_layout.addWidget(value_widget)

        add_item("", self._file_badge, first=True)
        add_item("", self._file_name_label)
        add_item("크기", self._file_size_label)
        add_item("길이", self._duration_label)
        add_item("해시", self._hash_label)
        file_info_layout.addWidget(self._integrity_dot)
        file_info_layout.addStretch(1)

        self._tabs = QTabWidget()
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._tracker_tab = TrackerTab()
        self._speed_tab = SpeedTab()
        self._location_tab = LocationTab()
        for tab in (self._tracker_tab, self._location_tab):
            tab.map_view().online_map_failed.connect(self.online_map_failed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(header)
        layout.addWidget(file_info)
        layout.addWidget(self._tabs, 1)

    def load_result(self, result: PipelineResult, case_number: str, settings: dict) -> None:
        self._result = result
        self._case_label.setText(f"Case Number : {case_number}")

        video_path = result.source_copy_path or result.extraction.used_input_path
        filename = os.path.basename(video_path) if video_path else "-"
        size_bytes = os.path.getsize(video_path) if video_path and os.path.isfile(video_path) else 0

        container = (result.extraction.routing.container or "").upper()
        self._file_badge.setText(container or "-")
        tag = view_tag(result.track_mode, bool(result.rear_copy_path))
        self._file_name_label.setText(f"{filename} - {tag}" if tag else filename)
        self._file_name_label.setToolTip(
            (video_path or "") + ({"F": "\n전방만 보기", "B": "\n후방만 보기", "F, B": "\n전방·후방 같이 보기"}.get(tag, "")))
        self._file_size_label.setText(_format_size(size_bytes))
        self._duration_label.setText(_format_duration(result.duration_sec))
        self._hash_label.set_hash(result.sha256)

        self._start_integrity_check(result)

        self._tabs.clear()
        if settings.get("tracker", True):
            self._tabs.addTab(self._tracker_tab, "Tracker")
            self._tracker_tab.stop()
            self._tracker_tab.set_duration_hint(result.duration_sec)
            if video_path and os.path.isfile(video_path):
                rear = result.rear_copy_path if result.rear_copy_path and os.path.isfile(result.rear_copy_path) else ""
                self._tracker_tab.load_video(video_path, rear, track_mode=result.track_mode)
            self._tracker_tab.load_track(result.extraction.points, result.flagged_segments)
        else:
            self._tracker_tab.stop()
        if settings.get("speed", True):
            self._tabs.addTab(self._speed_tab, "Speed Analysis")
            self._speed_tab.load(result.extraction.points, result.flagged_segments)
        if settings.get("location", True):
            self._tabs.addTab(self._location_tab, "Location Analysis")
            self._location_tab.load(result.extraction.points, result.flagged_segments)

        self._on_tab_changed(self._tabs.currentIndex())

    def release_media(self) -> None:
        """보고 있던 사건이 삭제될 때 영상 파일 잠금을 푼다."""
        self._tracker_tab.release_media()

    def reload_maps(self) -> None:
        """지도 사용 방식(오프라인/온라인)이 바뀐 뒤 이미 떠 있는 지도를 새 방식으로 다시 띄운다."""
        for tab in (self._tracker_tab, self._location_tab):
            tab.map_view().reload()

    def set_case_number(self, case_number: str) -> None:
        self._case_label.setText(f"Case Number : {case_number}")

    # ---------- 무결성 표시등 ----------
    def _set_integrity(self, state: str, detail: str) -> None:
        colors = {"pending": "#b0b0b0", "ok": "#2fb344", "bad": "#e03131", "none": "#b0b0b0"}
        self._integrity_dot.setStyleSheet(f"color: {colors.get(state, '#b0b0b0')}; font-size: 14px;")
        tips = {
            "pending": "원본과 사본 해시 비교 중…",
            "ok": "원본 해시와 사건 폴더 사본의 해시가 같습니다 (무결성 확인)",
            "bad": "원본 해시와 사본 해시가 다릅니다! 사본이 변조·손상됐을 수 있습니다",
            "none": "사본 파일이 없어 비교하지 못했습니다",
        }
        self._integrity_dot.setToolTip(tips.get(state, "") + (f"\n{detail}" if detail else ""))
        self._integrity_state = state

    def integrity_state(self) -> str:
        return getattr(self, "_integrity_state", "pending")

    def _start_integrity_check(self, result: PipelineResult) -> None:
        from ui.workers import HashWorker  # 순환 import 회피

        if self._integrity_worker is not None:
            self._integrity_worker.cancel()
            self._integrity_worker = None
        copy_path = result.source_copy_path
        if not result.sha256 or not copy_path or not os.path.isfile(copy_path):
            self._set_integrity("none", copy_path or "")
            return
        self._set_integrity("pending", "")
        expected = result.sha256
        worker = HashWorker(copy_path, self)

        def on_done(sha: str, w=worker) -> None:
            if w is not self._integrity_worker:
                return  # 새 사건이 열려 이미 다른 검사가 시작됨
            self._integrity_worker = None
            w.deleteLater()
            if not sha:
                self._set_integrity("none", "해시 계산이 취소됨")
            elif sha == expected:
                self._set_integrity("ok", f"SHA-256 {sha[:16]}…")
            else:
                self._set_integrity("bad", f"원본 {expected[:16]}… / 사본 {sha[:16]}…")

        def on_failed(message: str, w=worker) -> None:
            if w is self._integrity_worker:
                self._integrity_worker = None
                w.deleteLater()
                self._set_integrity("none", message)

        worker.finished_hash.connect(on_done)
        worker.failed.connect(on_failed)
        self._integrity_worker = worker
        worker.start()

    def _on_tab_changed(self, _index: int) -> None:
        widget = self._tabs.currentWidget()
        loader = getattr(widget, "ensure_map_loaded", None)
        if callable(loader):
            loader()

    def capture_visuals(self) -> tuple:
        """리포트에 넣을 그래프/지도 이미지. 못 만들면 None을 돌려준다(리포트는 계속 나간다).

        지도는 분석 직후 전체 경로에 맞춰 찍어 둔 기준 그림(baseline)을 쓴다. 기준 그림이
        아직 없으면(지도 탭을 한 번도 안 열었거나 타일이 늦게 온 경우) 현재 화면을 잡는다.
        """
        chart = None
        try:
            chart = self._speed_tab.grab_chart_png()
        except Exception:
            chart = None

        # 분석 직후 전체 경로에 맞춰진 기준 지도를 우선 쓴다. 사용자가 확대·축소한 현재
        # 화면은 기준 그림이 없을 때만 대신 쓴다.
        map_png = None
        for tab in (self._tracker_tab, self._location_tab):
            try:
                map_png = tab.map_view().baseline_png()
            except Exception:
                map_png = None
            if map_png:
                return chart, map_png
        for tab in (self._location_tab, self._tracker_tab):
            try:
                map_png = tab.grab_map_png()
            except Exception:
                map_png = None
            if map_png:
                break
        return chart, map_png

    def _on_report_clicked(self) -> None:
        if self._result is not None:
            self.report_requested.emit(self._result)
