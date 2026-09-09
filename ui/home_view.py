from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from storage.history_store import CaseRecord

VIDEO_FILTER = "블랙박스 영상 (*.mp4 *.avi);;모든 파일 (*)"


class HomeView(QWidget):
    video_selected = Signal(str)
    history_item_opened = Signal(int)
    # 삭제는 확인 창과 실제 삭제를 MainWindow가 맡는다(사건 폴더·DB 경로를 아는 곳).
    history_delete_requested = Signal(list)   # 선택한 사건 id 목록
    history_clear_requested = Signal()        # 전체

    def __init__(self, parent=None):
        super().__init__(parent)

        title = QLabel("GPS Tracer")
        title.setProperty("role", "title")

        heading = QLabel("Title")
        heading.setProperty("role", "heading")

        self._upload_box = QFrame()
        self._upload_box.setProperty("role", "upload-box")
        self._upload_box.setMinimumSize(360, 220)
        upload_layout = QVBoxLayout(self._upload_box)
        upload_btn = QPushButton("Upload")
        upload_btn.clicked.connect(self._on_upload_clicked)
        upload_layout.addStretch(1)
        upload_layout.addWidget(upload_btn)
        upload_layout.addStretch(1)

        left = QVBoxLayout()
        left.addWidget(heading)
        left.addWidget(self._upload_box)
        left.addStretch(1)

        history_heading = QLabel("History")
        hint = QLabel("더블클릭: 열기 · Delete 키/우클릭: 삭제 · Ctrl/Shift 클릭: 여러 개 선택")
        hint.setStyleSheet("color: #777; font-size: 11px;")

        self._history_list = QListWidget()
        self._history_list.setSelectionMode(QListWidget.ExtendedSelection)
        self._history_list.itemDoubleClicked.connect(self._on_history_double_clicked)
        self._history_list.itemSelectionChanged.connect(self._update_buttons)
        self._history_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._history_list.customContextMenuRequested.connect(self._on_context_menu)

        self._delete_btn = QPushButton("선택 삭제")
        self._delete_btn.clicked.connect(self._request_delete_selected)
        self._clear_btn = QPushButton("전체 삭제")
        self._clear_btn.clicked.connect(self.history_clear_requested.emit)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._delete_btn)
        buttons.addWidget(self._clear_btn)

        # 목록에 포커스가 있을 때 Delete 키로도 지운다(확인 창은 어차피 뜬다).
        delete_shortcut = QShortcut(QKeySequence(QKeySequence.Delete), self._history_list)
        delete_shortcut.setContext(Qt.WidgetShortcut)
        delete_shortcut.activated.connect(self._request_delete_selected)

        right = QVBoxLayout()
        right.addWidget(history_heading)
        right.addWidget(hint)
        right.addWidget(self._history_list, 1)
        right.addLayout(buttons)

        body = QHBoxLayout()
        body.addLayout(left, 2)
        body.addLayout(right, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(body, 1)
        self._update_buttons()

    def _on_upload_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "블랙박스 영상 선택", "", VIDEO_FILTER)
        if path:
            self.video_selected.emit(path)

    def _on_history_double_clicked(self, item: QListWidgetItem) -> None:
        case_id = item.data(Qt.UserRole)
        if case_id is not None:
            self.history_item_opened.emit(case_id)

    def selected_case_ids(self) -> List[int]:
        ids = []
        for item in self._history_list.selectedItems():
            case_id = item.data(Qt.UserRole)
            if case_id is not None:
                ids.append(int(case_id))
        return ids

    def _request_delete_selected(self) -> None:
        ids = self.selected_case_ids()
        if ids:
            self.history_delete_requested.emit(ids)

    def _on_context_menu(self, pos) -> None:
        item = self._history_list.itemAt(pos)
        if item is not None and not item.isSelected():
            self._history_list.setCurrentItem(item)
        menu = QMenu(self)
        open_action = QAction("열기", menu)
        open_action.setEnabled(item is not None)
        open_action.triggered.connect(lambda: self._on_history_double_clicked(item))
        delete_action = QAction("선택 삭제", menu)
        delete_action.setEnabled(bool(self.selected_case_ids()))
        delete_action.triggered.connect(self._request_delete_selected)
        clear_action = QAction("전체 삭제", menu)
        clear_action.setEnabled(self._history_list.count() > 0)
        clear_action.triggered.connect(self.history_clear_requested.emit)
        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(delete_action)
        menu.addAction(clear_action)
        menu.exec(self._history_list.mapToGlobal(pos))

    def _update_buttons(self) -> None:
        self._delete_btn.setEnabled(bool(self._history_list.selectedItems()))
        self._clear_btn.setEnabled(self._history_list.count() > 0)

    def set_history(self, cases: List[CaseRecord]) -> None:
        self._history_list.clear()
        for case in cases:
            label = f"{case.case_number} - {case.source_video_filename} ({case.created_at})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, case.id)
            self._history_list.addItem(item)
        self._update_buttons()
