from __future__ import annotations

import os
from typing import List, Optional

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
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.appinfo import APP_NAME
from core.video_pairs import PairCheck, find_rear_sibling
from core.video_sequence import (SequencePlan, check_pair_probes, probe_and_plan, probe_single,
                                 slot_start_text)
from core.video_tracks import TRACK_MODE_BOTH, TRACK_MODE_FRONT, TRACK_MODE_REAR, has_dual_video_tracks, view_tag
from ui.pair_check_worker import PairCheckWorker
from storage.history_store import CaseRecord

VIDEO_FILTER = "블랙박스 영상 (*.mp4 *.avi);;모든 파일 (*)"


class HomeView(QWidget):
    video_selected = Signal(str, str, str)   # (전방 영상, 후방 영상 또는 "", 2트랙 보기 방식 또는 "")
    sequence_selected = Signal(list)         # 연속 영상 이어보기: core/video_sequence.SequenceItem 목록(순서대로)
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
        # 연속 영상 이어보기: 블랙박스가 1분씩 나눠 쓴 파일 여러 개를 골라 한 사건으로 잇는다.
        self._seq_cb = QCheckBox("연속 영상 이어보기 (여러 파일을 골라 이어서 봅니다)")
        self._seq_cb.setToolTip(
            "블랙박스가 1분 단위로 나눠 저장한 영상 여러 개를 한 번에 고릅니다. 녹화 시각순으로\n"
            "자동 정렬하고, 끊김 없이 이어진 녹화인지(기기·형식·시각·위치) 검사한 뒤 분석합니다.\n"
            "전방/후방 같이 보기와 함께 켜면 전방 영상들 → 후방 영상들 순으로 고르고 자동으로 짝짓습니다.")
        upload_layout.addStretch(1)
        upload_layout.addWidget(self._dual_cb, 0, Qt.AlignHCenter)
        upload_layout.addWidget(self._seq_cb, 0, Qt.AlignHCenter)
        upload_layout.addWidget(upload_btn)
        recovery_btn = QPushButton("손상 AVI 복원")
        recovery_btn.clicked.connect(self._open_recovery)
        upload_layout.addWidget(recovery_btn)
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

    def _open_recovery(self) -> None:
        from ui.recovery_dialog import RecoveryDialog
        RecoveryDialog(self).exec()

    def _on_upload_clicked(self) -> None:
        if self._seq_cb.isChecked():
            self._upload_sequence()
            return
        if self._dual_cb.isChecked():
            self._notice("전방 영상을 선택해 주세요.")
        path, _ = QFileDialog.getOpenFileName(self, "블랙박스 영상 선택 (전방)", "", VIDEO_FILTER)
        if not path:
            return
        self._start_single(path)

    def _notice(self, text: str, title: str = "영상 선택") -> None:
        """파일 창의 제목만으로는 뭘 고르는지 잘 안 보인다는 의견으로, 창을 띄우기 전에 알린다."""
        QMessageBox.information(self, title, text)

    def _start_single(self, path: str) -> None:
        track_mode = ""
        if has_dual_video_tracks(path):
            # 전·후방이 한 파일에 든 영상: 어떻게 볼지 묻는다. 따로 고른 후방 파일은 받지 않는다
            # (이미 후방이 들어 있다).
            track_mode = self._ask_dual_track_mode(path) or ""
            if not track_mode:
                return  # 아니요(취소)
            rear = ""
        else:
            rear = self._pick_rear(path) if self._dual_cb.isChecked() else ""
        self.video_selected.emit(path, rear, track_mode)

    def _ask_dual_track_mode(self, path: str, count: int = 1) -> Optional[str]:
        """전·후방 트랙이 한 파일에 든 영상을 어떻게 볼지. both/front/rear, 취소면 None.
        이어보기에서 그런 파일이 여러 개면(count) 한 번만 묻고 모두에 적용한다."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("전방/후방 영상이 붙어 있는 영상")
        if count > 1:
            box.setText(f"선택한 영상 중 {count}개는 전방/후방 영상이 붙어 있는 영상입니다.\n"
                        "전방/후방 영상을 동시에 보여드릴까요? (모두 같은 방식으로 봅니다)")
        else:
            box.setText("해당 영상은 전방/후방 영상이 붙어 있는 영상입니다.\n전방/후방 영상을 동시에 보여드릴까요?")
        box.setInformativeText(
            f"{os.path.basename(path)}\n\n같이 보기를 고르면 Tracker에서 왼쪽 전방·오른쪽 후방으로 재생하고, "
            "전방만/후방만을 고르면 그 영상 하나만 보여 줍니다. GPS 분석은 어느 쪽을 골라도 같습니다. "
            "선택은 사건에 저장돼 다시 열 때도 유지됩니다.")
        # 셋 다 AcceptRole로 두어야 넣은 순서대로 나란히 놓인다(역할별로 재배치되므로 ActionRole을
        # 섞으면 취소가 가운데로 온다). 아니요는 RejectRole이라 Esc로도 닫힌다.
        both = box.addButton("예, 같이 보기", QMessageBox.AcceptRole)
        front = box.addButton("전방만 보기", QMessageBox.AcceptRole)
        rear = box.addButton("후방만 보기", QMessageBox.AcceptRole)
        box.addButton("아니요 (취소)", QMessageBox.RejectRole)
        box.setDefaultButton(both)
        box.exec()
        clicked = box.clickedButton()
        if clicked is both:
            return TRACK_MODE_BOTH
        if clicked is front:
            return TRACK_MODE_FRONT
        if clicked is rear:
            return TRACK_MODE_REAR
        return None

    def _pick_rear(self, front_path: str) -> str:
        """후방 파일을 고르게 하고 전방과 같은 녹화인지 검사한다. 어긋나면(길이·녹화 시각 차이)
        받지 않고 다시 고르거나 전방만 분석하게 한다 - 전혀 다른 영상을 나란히 틀면 뒤죽박죽이
        되는데 막을 방법이 없다는 검토 의견. 취소하면 전방만 분석."""
        suggested = find_rear_sibling(front_path) or ""
        self._notice("후방 영상을 선택해 주세요.\n(취소하면 전방만 분석합니다)")
        while True:
            rear, _ = QFileDialog.getOpenFileName(
                self, "후방 영상 선택 (취소하면 전방만 분석)",
                suggested or os.path.dirname(front_path), VIDEO_FILTER)
            if not rear:
                return ""
            check = self._run_pair_check(front_path, rear)
            if check is None or check.cancelled:
                return ""  # 대조를 취소하면 전방만 분석
            if check.ok:
                return rear
            if not self._ask_repick_rear(front_path, rear, check.problems, check.notes):
                return ""
            suggested = rear

    def _run_pair_check(self, front_path: str, rear_path: str) -> Optional[PairCheck]:
        """전방/후방 대조를 워커에서 돌리고 진행 창을 띄운다. 사용자가 취소하면 None."""
        if os.path.abspath(front_path) == os.path.abspath(rear_path):
            check = PairCheck(front_path=front_path, rear_path=rear_path)
            check.problems.append("전방으로 고른 파일과 같은 파일입니다.")
            return check
        dialog = QProgressDialog("전방/후방 영상을 대조하는 중...", "취소", 0, 0, self)
        dialog.setWindowTitle("전방/후방 영상 대조")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        worker = PairCheckWorker(front_path, rear_path, self)
        result: List[Optional[PairCheck]] = [None]
        user_cancelled = [False]

        def on_done(check: PairCheck) -> None:
            result[0] = check
            dialog.close()

        def on_failed(message: str) -> None:
            check = PairCheck(front_path=front_path, rear_path=rear_path)
            check.problems.append(f"대조 중 오류가 나 후방을 받지 않습니다: {message}")
            result[0] = check
            dialog.close()

        def on_cancel() -> None:
            # QProgressDialog는 close()에도 canceled를 내므로 결과가 없을 때만 사용자 취소로 본다.
            if result[0] is None:
                user_cancelled[0] = True
                worker.cancel()

        worker.status.connect(dialog.setLabelText)
        worker.finished_check.connect(on_done)
        worker.failed.connect(on_failed)
        dialog.canceled.connect(on_cancel)
        worker.start()
        dialog.exec()
        worker.wait(30000)
        if user_cancelled[0] or result[0] is None:
            return None
        return result[0]

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

    # ---------- 연속 영상 이어보기 ----------
    def _run_task(self, title: str, first_text: str, fn):
        """fn(cancel_event, progress)을 워커에서 돌리고 진행 창을 띄운다. (결과, 오류). 취소면 (None, "")."""
        from ui.workers import TaskWorker

        dialog = QProgressDialog(first_text, "취소", 0, 0, self)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        worker = TaskWorker(fn, self)
        result = {"value": None, "error": "", "done": False}

        def on_done(value) -> None:
            result["value"], result["done"] = value, True
            dialog.close()

        def on_failed(message: str) -> None:
            result["error"], result["done"] = message, True
            dialog.close()

        def on_cancel() -> None:
            if not result["done"]:
                worker.cancel()
                dialog.setLabelText("취소하는 중...")

        worker.status.connect(dialog.setLabelText)
        worker.finished_task.connect(on_done)
        worker.failed.connect(on_failed)
        dialog.canceled.connect(on_cancel)
        worker.start()
        dialog.exec()
        if not result["done"]:
            worker.cancel()
        worker.wait()
        worker.deleteLater()
        if worker.is_cancelled() and not result["error"]:
            return None, ""
        return result["value"], result["error"]

    def _upload_sequence(self) -> None:
        dual = self._dual_cb.isChecked()
        self._notice("전방 영상들을 선택해 주세요. (여러 개 선택)" if dual else "이어볼 영상들을 선택해 주세요. (여러 개 선택)")
        fronts, _ = QFileDialog.getOpenFileNames(
            self, "전방 영상들을 선택해 주세요 (여러 개)" if dual else "이어볼 영상들을 선택해 주세요 (여러 개)",
            "", VIDEO_FILTER)
        if not fronts:
            return
        if len(fronts) == 1:
            QMessageBox.information(self, "연속 영상 이어보기",
                                    "영상을 하나만 골라 이어보기 없이 이 영상만 분석합니다.")
            self._start_single(fronts[0])
            return
        dual_track = [f for f in fronts if has_dual_video_tracks(f)]
        track_mode = ""
        if dual_track:
            track_mode = self._ask_dual_track_mode(dual_track[0], len(dual_track)) or ""
            if not track_mode:
                return
        rears: List[str] = []
        if dual and not (len(dual_track) == len(fronts) and track_mode == TRACK_MODE_BOTH):
            self._notice("후방 영상들을 선택해 주세요. (여러 개 선택)\n(취소하면 전방만 이어봅니다)")
            rears, _ = QFileDialog.getOpenFileNames(
                self, "후방 영상들을 선택해 주세요 (여러 개, 취소하면 전방만 이어봅니다)",
                os.path.dirname(fronts[0]), VIDEO_FILTER)

        plan, error = self._run_task(
            "연속 영상 검사", "영상을 검사하는 중...",
            lambda cancel, progress: probe_and_plan(fronts, rears, cancel, progress))
        if error:
            QMessageBox.critical(self, "연속 영상 검사", f"영상을 검사하다 오류가 났습니다:\n{error}")
            return
        if plan is None or plan.cancelled:
            return
        if not plan.ok:
            if self._show_sequence_problems(plan):
                self._upload_sequence()   # 다시 고르기
            return
        if rears and not self._fill_missing_pairs(plan):
            return
        items = plan.items()
        for item in items:
            item.track_mode = track_mode if item.primary in dual_track else ""
        if not self._confirm_sequence(plan):
            return
        self.sequence_selected.emit(items)

    def _slot_lines(self, plan: SequencePlan) -> List[str]:
        lines = []
        for n, slot in enumerate(plan.slots, start=1):
            names = " + ".join(x.name for x in (slot.front, slot.rear) if x is not None)
            only = "  (후방만)" if slot.front is None else ""
            start = slot_start_text(slot, plan.basis) if plan.basis else ""
            dur = slot.primary.duration
            length = f", {int(dur // 60)}분 {int(round(dur % 60)):02d}초" if dur else ""
            lines.append(f"{n}. {names}{only}" + (f"  - {start} 시작{length}" if start else ""))
        return lines

    def _show_sequence_problems(self, plan: SequencePlan) -> bool:
        """이어볼 수 없는 이유를 알리고 막는다. [다시 고르기]면 True."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("이어볼 수 없는 영상")
        box.setText("선택한 영상들은 끊김 없이 이어진 녹화로 볼 수 없어 이어보기를 할 수 없습니다.")
        detail = [f"• {p}" for p in plan.problems]
        if plan.slots:
            detail = ["[시각순 정렬]"] + self._slot_lines(plan) + [""] + detail
        box.setInformativeText("\n".join(detail))
        repick = box.addButton("다시 고르기", QMessageBox.AcceptRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(repick)
        box.exec()
        return box.clickedButton() is repick

    def _fill_missing_pairs(self, plan: SequencePlan) -> bool:
        """짝이 없는 구간마다 묻는다: 빠진 쪽 영상을 고르거나, 그 구간은 한쪽만 띄운다. 취소면 False."""
        used = {os.path.normcase(os.path.abspath(x.path))
                for slot in plan.slots for x in (slot.front, slot.rear) if x is not None}
        for n, slot in enumerate(plan.slots, start=1):
            while slot.front is None or slot.rear is None:
                missing = "후방" if slot.rear is None else "전방"
                have = slot.primary
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Warning)
                box.setWindowTitle(f"{missing} 영상 없음")
                box.setText(f"{n}번 영상의 {missing} 영상이 없습니다.")
                box.setInformativeText(
                    f"{n}번 영상: {have.name}\n\n해당 영상의 {missing} 영상을 선택해주세요. 없으시다면 "
                    f"{n}번 영상은 {missing} 영상을 제외하고 띄웁니다"
                    f"({'전방' if missing == '후방' else '후방'}만 재생).")
                pick = box.addButton(f"{missing} 영상 선택", QMessageBox.AcceptRole)
                skip = box.addButton(f"{missing} 제외하고 진행", QMessageBox.AcceptRole)
                box.addButton("취소", QMessageBox.RejectRole)
                box.setDefaultButton(pick)
                box.exec()
                clicked = box.clickedButton()
                if clicked is skip:
                    break
                if clicked is not pick:
                    return False
                path, _ = QFileDialog.getOpenFileName(
                    self, f"{n}번 영상의 {missing} 영상 선택", os.path.dirname(have.path), VIDEO_FILTER)
                if not path:
                    continue
                if os.path.normcase(os.path.abspath(path)) in used:
                    QMessageBox.warning(self, "이미 고른 영상", "이미 다른 자리에 쓰인 영상입니다.")
                    continue
                probe, error = self._run_task(
                    f"{missing} 영상 대조", f"{os.path.basename(path)} 읽는 중...",
                    lambda cancel, _progress, p=path: probe_single(p, cancel))
                if error or probe is None:
                    if error:
                        QMessageBox.critical(self, f"{missing} 영상 대조", error)
                    continue
                front, rear = (probe, have) if missing == "전방" else (have, probe)
                check = check_pair_probes(front, rear)
                if check.ok:
                    if missing == "전방":
                        slot.front = probe
                    else:
                        slot.rear = probe
                    used.add(os.path.normcase(os.path.abspath(path)))
                    break
                self._ask_repick_rear(front.path, rear.path, check.problems, check.notes)
        return True

    def _confirm_sequence(self, plan: SequencePlan) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("연속 영상 이어보기")
        box.setText(f"영상 {len(plan.slots)}개를 녹화 시각순으로 이어서 분석합니다.")
        detail = self._slot_lines(plan)
        if plan.notes:
            detail += [""] + [f"참고: {n}" for n in plan.notes]
        box.setInformativeText("\n".join(detail))
        start = box.addButton("분석 시작", QMessageBox.AcceptRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(start)
        box.exec()
        return box.clickedButton() is start

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
            if case.segments:
                n = len(case.segments)
                label = (f"{case.case_number} - {case.source_video_filename} 외 {n - 1}개 · 이어보기 "
                         f"({case.created_at})")
                has_rear = any(seg.get("rear_filename") for seg in case.segments)
            else:
                label = f"{case.case_number} - {case.source_video_filename} ({case.created_at})"
                has_rear = bool(case.rear_video_filename)
            tag = view_tag(case.track_mode, has_rear)
            if tag:
                label += f" - {tag}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, case.id)
            self._history_list.addItem(item)
        self._update_buttons()
