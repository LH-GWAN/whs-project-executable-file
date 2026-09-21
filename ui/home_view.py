from __future__ import annotations

import os
from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.appinfo import APP_NAME
from core.video_pairs import compare_pair, find_rear_sibling
from storage.history_store import CaseRecord

VIDEO_FILTER = "블랙박스 영상 (*.mp4 *.avi);;모든 파일 (*)"


class HomeView(QWidget):
    video_selected = Signal(str, str)   # (전방 영상, 후방 영상 또는 "")
    history_item_opened = Signal(int)
    history_edit_requested = Signal(int)
    # 삭제는 확인 창과 실제 삭제를 MainWindow가 맡는다(사건 폴더·DB 경로를 아는 곳).
    history_delete_requested = Signal(list)   # 선택한 사건 id 목록
    history_clear_requested = Signal()        # 전체

    def __init__(self, parent=None):
        super().__init__(parent)

        title = QLabel(APP_NAME)
        title.setProperty("role", "title")

        heading = QLabel("Title")
        heading.setProperty("role", "heading")

        self._upload_box = QFrame()
        self._upload_box.setProperty("role", "upload-box")
        self._upload_box.setMinimumSize(360, 220)
        upload_layout = QVBoxLayout(self._upload_box)
        upload_btn = QPushButton("Upload")
        upload_btn.clicked.connect(self._on_upload_clicked)
        # 전방/후방 같이 보기: 켜 두면 전방 파일을 고른 뒤 후방 파일을 하나 더 고른다.
        # 같은 폴더에 _F/_R 짝이 있으면 그 파일을 기본값으로 띄운다.
        self._dual_cb = QCheckBox("전방/후방 영상 같이 보기 (후방 영상 파일을 하나 더 고릅니다)")
        self._dual_cb.setToolTip(
            "전방 영상을 고른 뒤 후방 영상을 고르는 창이 한 번 더 뜹니다. 같은 폴더에\n"
            "…_F / …_R 처럼 짝이 되는 파일이 있으면 자동으로 골라 둡니다.\n"
            "GPS 분석은 전방 영상으로 하고, 후방은 Tracker에서 나란히 재생만 합니다.\n"
            "파일 하나에 전방·후방 트랙이 같이 든 영상은 이 옵션과 무관하게 둘 다 보입니다.")
        upload_layout.addStretch(1)
        upload_layout.addWidget(self._dual_cb, 0, Qt.AlignHCenter)
        upload_layout.addWidget(upload_btn)
        upload_layout.addStretch(1)

        left = QVBoxLayout()
        left.addWidget(heading)
        left.addWidget(self._upload_box)
        left.addStretch(1)

        history_heading = QLabel("History")
        hint = QLabel("더블클릭: 열기 · 우클릭: 사건 정보 수정/삭제 · Delete 키: 삭제 · Ctrl/Shift 클릭: 여러 개 선택")
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

        title_row = QHBoxLayout()
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._settings_slot = QHBoxLayout()
        self._settings_slot.setContentsMargins(0, 0, 0, 0)
        title_row.addLayout(self._settings_slot)

        layout = QVBoxLayout(self)
        layout.addLayout(title_row)
        layout.addLayout(body, 1)
        self._update_buttons()

    def set_settings_menu(self, menu) -> None:
        from ui.analysis_view import make_settings_button
        self._settings_slot.addWidget(make_settings_button(menu, self))

    def _on_upload_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "블랙박스 영상 선택 (전방)", "", VIDEO_FILTER)
        if not path:
            return
        rear = self._pick_rear(path) if self._dual_cb.isChecked() else ""
        self.video_selected.emit(path, rear)

    def _pick_rear(self, front_path: str) -> str:
        """후방 파일을 고르게 하고 전방과 같은 녹화인지 검사한다. 어긋나면(길이·녹화 시각 차이)
        받지 않고 다시 고르거나 전방만 분석하게 한다 - 전혀 다른 영상을 나란히 틀면 뒤죽박죽이
        되는데 막을 방법이 없다는 검토 의견. 취소하면 전방만 분석."""
        suggested = find_rear_sibling(front_path) or ""
        while True:
            rear, _ = QFileDialog.getOpenFileName(
                self, "후방 영상 선택 (취소하면 전방만 분석)",
                suggested or os.path.dirname(front_path), VIDEO_FILTER)
            if not rear:
                return ""
            if os.path.abspath(rear) == os.path.abspath(front_path):
                problems = ["전방으로 고른 파일과 같은 파일입니다."]
                notes: List[str] = []
            else:
                check = compare_pair(front_path, rear)
                problems, notes = check.problems, check.notes
            if not problems:
                return rear
            if not self._ask_repick_rear(front_path, rear, problems, notes):
                return ""
            suggested = rear

    def _ask_repick_rear(self, front_path: str, rear_path: str,
                         problems: List[str], notes: List[str]) -> bool:
        """어긋난 후방 파일을 알리고 [다시 고르기]면 True, [전방만 분석]이면 False."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("후방 영상이 전방과 맞지 않습니다")
        box.setText("고른 후방 영상은 전방 영상과 같은 녹화로 볼 수 없어 후방으로 받지 않습니다.")
        detail = [f"전방: {os.path.basename(front_path)}", f"후방: {os.path.basename(rear_path)}", ""]
        detail += [f"• {p}" for p in problems]
        if notes:
            detail += [""] + [f"참고: {n}" for n in notes]
        box.setInformativeText("\n".join(detail))
        repick = box.addButton("다시 고르기", QMessageBox.AcceptRole)
        box.addButton("전방만 분석", QMessageBox.RejectRole)
        box.setDefaultButton(repick)
        box.exec()
        return box.clickedButton() is repick

    def dual_view_enabled(self) -> bool:
        return self._dual_cb.isChecked()

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
        menu = self.build_context_menu(item)
        menu.exec(self._history_list.mapToGlobal(pos))

    def build_context_menu(self, item) -> QMenu:
        menu = QMenu(self)
        open_action = QAction("열기", menu)
        open_action.setEnabled(item is not None)
        open_action.triggered.connect(lambda: self._on_history_double_clicked(item))
        edit_action = QAction("사건 정보 수정… (사건번호·담당자·메모)", menu)
        edit_action.setEnabled(item is not None)
        edit_action.triggered.connect(lambda: self._request_edit(item))
        delete_action = QAction("선택 삭제", menu)
        delete_action.setEnabled(bool(self.selected_case_ids()))
        delete_action.triggered.connect(self._request_delete_selected)
        clear_action = QAction("전체 삭제", menu)
        clear_action.setEnabled(self._history_list.count() > 0)
        clear_action.triggered.connect(self.history_clear_requested.emit)
        menu.addAction(open_action)
        menu.addAction(edit_action)
        menu.addSeparator()
        menu.addAction(delete_action)
        menu.addAction(clear_action)
        return menu

    def _request_edit(self, item: QListWidgetItem) -> None:
        if item is None:
            return
        case_id = item.data(Qt.UserRole)
        if case_id is not None:
            self.history_edit_requested.emit(int(case_id))

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
