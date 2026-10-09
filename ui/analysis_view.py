from __future__ import annotations

import os

from typing import Dict, List, Optional, Tuple

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
from core.slack import SlackSet, build_slack_set
from ui.dataset_view import COMPOSED_LABEL, DatasetView
from ui.slack_tab import SlackTab
from ui.location_tab import LocationTab
from ui.speed_tab import SpeedTab
from ui.tracker_tab import PlaylistItem, TrackerTab


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
    작은 상자로 뜨고(바깥 클릭으로 닫힘) 클립보드에도 복사된다.

    전방·후방 같이 보기면 "전방 앞 12자… / 후방 앞 12자…"로 둘을 보인다. 이어보기(Composed)는
    파일이 여럿이라 "N개 파일 모두 일치"처럼 요약만 보이고, 마우스를 올리면 파일별 해시 목록이
    뜬다(클릭하면 목록 전체가 상자로 뜨고 복사된다)."""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self._full = ""
        self._popup: Optional[_HashPopup] = None
        self.setProperty("role", "hash")
        self.setCursor(Qt.PointingHandCursor)

    def set_hash(self, sha256: str) -> None:
        self.set_hashes([("", sha256)] if sha256 else [])

    def set_hashes(self, entries: List[Tuple[str, str]], summary: str = "") -> None:
        """entries: (이름, 해시). summary를 주면 그 글을 보이고 목록은 툴팁으로."""
        entries = [(label, sha) for label, sha in entries if sha]
        if len(entries) == 1 and not summary:
            self._full = entries[0][1]
        else:
            self._full = "\n".join(f"{label}: {sha}" if label else sha for label, sha in entries)
        if summary:
            self.setText(summary)
            self.setToolTip(self._full)
        elif not entries:
            self.setText("-")
            self.setToolTip("")
        elif len(entries) == 1:
            sha = entries[0][1]
            self.setText(f"{sha[:16]}…" if len(sha) > 16 else sha)
            self.setToolTip("")
        else:
            self.setText(" / ".join(f"{sha[:12]}…" for _label, sha in entries))
            self.setToolTip("")

    def full_text(self) -> str:
        return self._full

    def mousePressEvent(self, event):  # noqa: N802
        if self._full:
            QGuiApplication.clipboard().setText(self._full)
            if self._popup is None:
                self._popup = _HashPopup(self.window())
            self._popup.show_for(self, self._full)
        super().mousePressEvent(event)


def slack_sets(result: PipelineResult) -> List[SlackSet]:
    """따로 뽑은 슬랙 데이터. 이어보기면 영상마다 하나씩(있는 영상만)."""
    if result.is_sequence:
        sets = [build_slack_set(seg.extraction.slack_points, seg.label) for seg in result.segments]
    else:
        sets = [build_slack_set(result.extraction.slack_points)]
    return [s for s in sets if s is not None]


def dataset_views(result: PipelineResult, slacks: Optional[List[SlackSet]] = None) -> List[DatasetView]:
    """Speed/Location 탭의 묶음. 이어보기면 Composed + 영상별, 아니면 하나. 슬랙 데이터를 뽑았으면
    맨 뒤에 "… 슬랙" 묶음(위험운전 판정은 하지 않는다 - 시간축이 영상이 아니다)."""
    if not result.is_sequence:
        views = [DatasetView(COMPOSED_LABEL, result.points, result.driving_events)]
    else:
        segs = result.segments
        labels = [seg.label for seg in segs]
        composed = DatasetView(COMPOSED_LABEL, result.points, result.driving_events,
                               boundaries=[(seg.offset_sec, seg.label) for seg in segs[1:]],
                               segment_labels=labels)
        views = [composed] + [DatasetView(seg.label, seg.points, seg.driving_events) for seg in segs]
    for slack in slacks or []:
        views.append(DatasetView(slack.label, slack.points, [], map_points=slack.map_points, is_slack=True))
    return views


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
        self._report_btn = report_btn
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
        # Speed/Location에서 Composed ↔ video1… 을 바꾸면 파일 정보 줄도 그 영상으로 바꾼다.
        self._speed_tab.view_changed.connect(lambda _i: self._refresh_header())
        self._location_tab.view_changed.connect(lambda _i: self._refresh_header())
        self._file_views: List[Dict] = []
        self._settings: Dict = {}
        self._integrity_results: Dict[str, Tuple[str, str]] = {}
        self._integrity_queue: List[Tuple[str, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(header)
        layout.addWidget(file_info)
        # 슬랙 데이터를 따로 뽑은 사건: 파일 정보 바로 밑에 "슬랙 데이터: [video1] [video3]" 줄.
        # 누르면 그 슬랙 탭으로 간다. 뽑지 않았거나 슬랙에 GPS가 없으면 줄 자체를 숨긴다.
        self._slack_bar = QWidget()
        self._slack_bar.setObjectName("SlackBar")
        self._slack_bar_layout = QHBoxLayout(self._slack_bar)
        self._slack_bar_layout.setContentsMargins(12, 2, 12, 2)
        self._slack_bar_layout.setSpacing(8)
        self._slack_bar.hide()
        self._slack_tabs: List[SlackTab] = []
        layout.addWidget(self._slack_bar)
        self._analysis_status = QLabel("")
        self._analysis_status.setWordWrap(True)
        self._analysis_status.setTextFormat(Qt.PlainText)
        self._analysis_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._analysis_status)
        layout.addWidget(self._tabs, 1)

    def load_result(self, result: PipelineResult, case_number: str, settings: dict) -> None:
        self._tracker_tab.release_media()
        self._result = result
        self._settings = dict(settings)
        self._case_label.setText(f"Case Number : {case_number}")
        extraction = result.extraction
        prefix = f"이어보기 {len(result.segments)}개 영상 · " if result.is_sequence else ""
        container = (extraction.routing.container or "").lower()
        # "AVI 복구"는 AVI에만 해당하는 처리라 MP4 사건에는 적지 않는다(검토 의견).
        repair = f" · AVI 복구 {'적용' if extraction.avi_repaired else '없음'}" if container == "avi" else ""
        self._analysis_status.setText(
            f"{prefix}분석 상태: {extraction.status} · 경고 {len(extraction.warnings)}건{repair} · "
            f"슬랙 GPS {len(extraction.slack_points)}건")
        if result.artifacts_verified is False:
            self._analysis_status.setText(self._analysis_status.text() + " · ⚠ 엔진 산출물이 분석 때와 다릅니다(manifest 불일치)")
        elif result.artifacts_verified is True:
            self._analysis_status.setText(self._analysis_status.text() + " · 산출물 manifest 일치")
        self._analysis_status.setToolTip(extraction.status_message + "\n" + "\n".join(extraction.warnings))

        self._file_views = self._build_file_views(result)
        self._start_integrity_checks([f for view in self._file_views[:1] for f in view["files"]]
                                     if not result.is_sequence else
                                     [f for view in self._file_views[1:] for f in view["files"]])

        self._tabs.clear()
        if settings.get("tracker", True):
            self._tabs.addTab(self._tracker_tab, "Tracker")
            self._tracker_tab.stop()
            if result.is_sequence:
                # 이어보기: 영상을 차례로 튼다. 한 영상이 끝나면 다음 영상이 이어서 재생된다.
                self._tracker_tab.load_playlist([
                    PlaylistItem(path=seg.primary_copy_path,
                                 rear_path=seg.rear_copy_path if seg.front_copy_path else "",
                                 track_mode=seg.track_mode, duration_sec=seg.duration_sec,
                                 label=seg.label, primary_is_rear=seg.primary_is_rear)
                    for seg in result.segments])
            else:
                video_path = result.source_copy_path
                self._tracker_tab.set_duration_hint(result.duration_sec)
                if video_path and os.path.isfile(video_path):
                    rear = result.rear_copy_path if result.rear_copy_path and os.path.isfile(result.rear_copy_path) else ""
                    self._tracker_tab.load_video(video_path, rear, track_mode=result.track_mode)
                else:
                    self._tracker_tab._on_media_error("사건 영상 파일이 없습니다. 사본 경로를 확인하세요.")
            self._tracker_tab.load_track(result.extraction.points, result.driving_events)
        else:
            # 탭을 끈 사건: 이전 사건의 궤적·기준 그림이 남지 않게 비운다(리뷰 #1 - 끈 탭의 그림이
            # 다음 사건 리포트에 들어갔다).
            self._tracker_tab.stop()
            self._tracker_tab.load_track([], [])
        slacks = slack_sets(result)   # 슬랙을 뽑지 않았거나 GPS가 없으면 빈 목록
        self._rebuild_slack_tabs(slacks)
        views = dataset_views(result, slacks)
        if settings.get("speed", True):
            self._tabs.addTab(self._speed_tab, "Speed Analysis")
            self._speed_tab.load_views(views, result.vehicle_type)
        else:
            self._speed_tab.load_views([], result.vehicle_type)
        if settings.get("location", True):
            self._tabs.addTab(self._location_tab, "Location Analysis")
            self._location_tab.load_views(views)
        else:
            self._location_tab.load_views([])

        for tab in self._slack_tabs:
            self._tabs.addTab(tab, f"Slack · {tab.slack.source_label}" if tab.slack.source_label else "Slack")
        self._on_tab_changed(self._tabs.currentIndex())

    def _rebuild_slack_tabs(self, slacks: List[SlackSet]) -> None:
        for tab in self._slack_tabs:
            tab.deleteLater()
        self._slack_tabs = []
        while self._slack_bar_layout.count():
            item = self._slack_bar_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if not slacks:
            self._slack_bar.hide()
            return
        caption = QLabel("슬랙 데이터")
        caption.setProperty("role", "info-key")
        self._slack_bar_layout.addWidget(caption)
        for slack in slacks:
            tab = SlackTab(slack)
            tab.map_view().online_map_failed.connect(self.online_map_failed)
            self._slack_tabs.append(tab)
            button = QPushButton(slack.source_label or "슬랙 보기")
            button.setToolTip(slack.summary + "\n누르면 슬랙 데이터 탭으로 갑니다 (영상 없음, 지도만)")
            button.clicked.connect(lambda _c=False, t=tab: self._tabs.setCurrentWidget(t))
            self._slack_bar_layout.addWidget(button)
        self._slack_bar_layout.addStretch(1)
        self._slack_bar.show()

    # ---------- 파일 정보 줄 ----------
    def _build_file_views(self, result: PipelineResult) -> List[Dict]:
        """파일 정보 줄에 보일 내용. [0]은 사건 전체(이어보기면 Composed), [1:]은 영상별."""
        container = (result.extraction.routing.container or "").upper() or "-"

        def size_of(*paths: str) -> int:
            return sum(os.path.getsize(p) for p in paths if p and os.path.isfile(p))

        if not result.is_sequence:
            video_path = result.source_copy_path
            filename = os.path.basename(video_path) if video_path else "-"
            tag = view_tag(result.track_mode, bool(result.rear_copy_path))
            files = [("전방" if result.rear_copy_path else "", video_path, result.sha256)]
            if result.rear_copy_path:
                files.append(("후방", result.rear_copy_path, result.rear_sha256))
            tip = (video_path or "") + ({"F": "\n전방만 보기", "B": "\n후방만 보기",
                                          "F, B": "\n전방·후방 같이 보기"}.get(tag, ""))
            if result.rear_copy_path:
                tip += f"\n후방: {result.rear_copy_path}"
            sizes = [size_of(video_path)] + ([size_of(result.rear_copy_path)] if result.rear_copy_path else [])
            return [{"badge": container, "name": f"{filename} - {tag}" if tag else filename, "tip": tip,
                     "size": " / ".join(_format_size(x) for x in sizes),
                     "duration": _format_duration(result.duration_sec), "files": files, "summary": False}]

        segs = result.segments
        views: List[Dict] = []
        all_files = []
        per_segment = []
        for seg in segs:
            files = []
            if seg.front_copy_path:
                files.append((f"{seg.label} 전방", seg.front_copy_path, seg.front_sha256))
            if seg.rear_copy_path:
                files.append((f"{seg.label} 후방", seg.rear_copy_path, seg.rear_sha256))
            all_files += files
            names = " + ".join(os.path.basename(p) for _l, p, _s in files)
            per_segment.append({
                "badge": container, "name": f"{seg.label} · {names}",
                "tip": "\n".join(p for _l, p, _s in files),
                "size": " / ".join(_format_size(size_of(p)) for _l, p, _s in files),
                "duration": _format_duration(seg.duration_sec),
                "files": [(label.split(" ", 1)[1] if len(files) > 1 else "", p, sha)
                          for label, p, sha in files],
                "summary": False})
        # 구간마다 보기 방식이 다를 수 있다(전방만·후방만·같이). History와 같은 view_tag를 쓴다(리뷰 #138).
        tags = sorted({view_tag(seg.track_mode, bool(seg.rear_copy_path)) for seg in segs} - {""})
        first = os.path.basename(segs[0].primary_copy_path)
        last = os.path.basename(segs[-1].primary_copy_path)
        views.append({
            "badge": container,
            "name": f"{first} ~ {last} · 이어보기 {len(segs)}개" + (f" - {' / '.join(tags)}" if tags else ""),
            "tip": "\n".join(f"{v['name']}" for v in per_segment),
            "size": _format_size(size_of(*[p for _l, p, _s in all_files])),
            "duration": _format_duration(result.duration_sec),
            "files": all_files, "summary": True})
        return views + per_segment

    def _current_file_view(self) -> int:
        widget = self._tabs.currentWidget()
        current = getattr(widget, "current_view", None)
        return current() if callable(current) and widget is not self._tracker_tab else 0

    def _refresh_header(self) -> None:
        views = getattr(self, "_file_views", None)
        if not views:
            return
        index = self._current_file_view()
        view = views[index] if 0 <= index < len(views) else views[0]
        self._file_badge.setText(view["badge"])
        self._file_name_label.setText(view["name"])
        self._file_name_label.setToolTip(view["tip"])
        self._file_size_label.setText(view["size"])
        self._duration_label.setText(view["duration"])
        state, detail = self._integrity_of(view["files"])
        entries = [(label, sha) for label, _p, sha in view["files"]]
        if view["summary"]:
            n = len(entries)
            summary = {"ok": f"{n}개 파일 모두 일치", "bad": f"{n}개 중 불일치 있음!",
                       "pending": f"{n}개 파일 확인 중…"}.get(state, f"{n}개 파일")
            self._hash_label.set_hashes(entries, summary)
        else:
            self._hash_label.set_hashes(entries)
        self._set_integrity(state, detail)

    # ---------- 무결성 표시등 ----------
    def _integrity_of(self, files) -> Tuple[str, str]:
        """보이는 파일들의 대조 결과를 하나로: 하나라도 불일치면 bad, 확인 중이면 pending."""
        results = getattr(self, "_integrity_results", {})
        states = [results.get(path, ("pending", "")) for _label, path, _sha in files]
        details = [f"{label or os.path.basename(path)}: {d}" if d else ""
                   for (label, path, _sha), (_st, d) in zip(files, states)]
        detail = "\n".join(d for d in details if d)
        kinds = [st for st, _d in states]
        if not kinds:
            return "none", ""
        if "bad" in kinds:
            return "bad", detail
        if "pending" in kinds:
            return "pending", detail
        if all(k == "ok" for k in kinds):
            return "ok", detail
        return "none", detail

    def release_media(self) -> None:
        """보고 있던 사건이 삭제될 때 영상 파일 잠금을 푼다. 무결성 재해시가 사본을 열어 두고 있으면
        Windows에서는 폴더 삭제가 실패하므로 그 워커도 멈춘다(리뷰 #26)."""
        self._tracker_tab.release_media()
        self.shutdown()

    def shutdown(self) -> None:
        """무결성 재해시 워커를 멈추고 끝날 때까지 기다린다. 창을 닫을 때·사건을 지울 때 부른다 -
        실행 중인 QThread가 파괴되면 abort된다(리뷰 #25)."""
        self._integrity_queue = []
        worker = self._integrity_worker
        if worker is not None:
            self._integrity_worker = None
            worker.cancel()
            if worker.isRunning():
                worker.wait()
            worker.deleteLater()

    def reload_maps(self) -> None:
        """지도 사용 방식(오프라인/온라인)이 바뀐 뒤 이미 떠 있는 지도를 새 방식으로 다시 띄운다."""
        for tab in (self._tracker_tab, self._location_tab, *self._slack_tabs):
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

    def _start_integrity_checks(self, files) -> None:
        """사본을 하나씩 다시 읽어 분석 때 기록한 원본 해시와 비교한다(전방·후방·이어보기 영상 모두)."""
        self.shutdown()   # 이전 사건의 재해시가 돌고 있으면 끝내고 치운다
        self._integrity_results: Dict[str, Tuple[str, str]] = {}
        queue = []
        for _label, path, expected in files:
            if not expected or not path or not os.path.isfile(path):
                self._integrity_results[path] = ("none", "사본 파일 또는 기록된 해시가 없음")
            else:
                queue.append((path, expected))
        self._integrity_queue = queue
        self._run_next_integrity()

    def _run_next_integrity(self) -> None:
        from ui.workers import HashWorker  # 순환 import 회피

        self._refresh_header()
        if not self._integrity_queue:
            return
        path, expected = self._integrity_queue.pop(0)
        worker = HashWorker(path, self)

        def finish(state: str, detail: str, w=worker) -> None:
            if w is not self._integrity_worker:
                return  # 새 사건이 열려 이미 다른 검사가 시작됨
            self._integrity_worker = None
            w.deleteLater()
            self._integrity_results[path] = (state, detail)
            self._run_next_integrity()

        def on_done(sha: str) -> None:
            if not sha:
                finish("none", "해시 계산이 취소됨")
            elif sha == expected:
                finish("ok", f"SHA-256 {sha[:16]}…")
            else:
                finish("bad", f"원본 {expected[:16]}… / 사본 {sha[:16]}…")

        worker.finished_hash.connect(on_done)
        worker.failed.connect(lambda message: finish("none", message))
        self._integrity_worker = worker
        worker.start()

    def _on_tab_changed(self, _index: int) -> None:
        self._refresh_header()
        widget = self._tabs.currentWidget()
        loader = getattr(widget, "ensure_map_loaded", None)
        if callable(loader):
            loader()

    def capture_visuals(self) -> tuple:
        """리포트에 넣을 그래프/지도 이미지. 못 만들면 None을 돌려준다(리포트는 계속 나간다).

        지도는 분석 직후 전체 경로에 맞춰 찍어 둔 기준 그림(baseline)을 쓴다. 기준 그림이
        아직 없으면(지도 탭을 한 번도 안 열었거나 타일이 늦게 온 경우) 현재 화면을 잡는다.
        """
        settings = getattr(self, "_settings", {})
        chart = None
        if settings.get("speed", True):
            try:
                chart = self._speed_tab.grab_chart_png()
            except Exception:
                chart = None

        # 좌표가 없는 사건은 지도 절을 넣지 않는다(기본 화면이나 이전 사건 지역이 찍힌다 - 리뷰 #14).
        if self._result is None or self._result.extraction.fix_count == 0:
            return chart, None
        # 분석 직후 전체 경로에 맞춰진 기준 지도를 우선 쓴다. 켜진 탭의 지도만 본다(끈 탭에는 그림이
        # 없다). 사용자가 확대·축소한 현재 화면은 기준 그림이 없을 때만 대신 쓰되, Location이 영상별·슬랙
        # 묶음을 보여 주고 있으면 본 궤적이 아니라서 쓰지 않는다(리뷰 #1, #13).
        tabs = []
        if settings.get("tracker", True):
            tabs.append(self._tracker_tab)
        if settings.get("location", True):
            tabs.append(self._location_tab)
        map_png = None
        for tab in tabs:
            try:
                map_png = tab.map_view().baseline_png()
            except Exception:
                map_png = None
            if map_png:
                return chart, map_png
        for tab in tabs:
            if tab is self._location_tab and self._location_tab.current_view() != 0:
                continue
            try:
                map_png = tab.grab_map_png()
            except Exception:
                map_png = None
            if map_png:
                break
        return chart, map_png

    def set_report_enabled(self, enabled: bool) -> None:
        self._report_btn.setEnabled(enabled)

    def _on_report_clicked(self) -> None:
        if self._result is not None:
            self.report_requested.emit(self._result)
