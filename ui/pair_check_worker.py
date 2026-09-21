"""전방/후방 영상 대조를 워커에서 돌린다. 엔진을 두 파일에 돌리므로 UI 스레드에서 하면 창이 멈춘다."""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from core.video_pairs import PairCheck, compare_pair_full


class PairCheckWorker(QThread):
    status = Signal(str)
    finished_check = Signal(object)   # PairCheck
    failed = Signal(str)

    def __init__(self, front_path: str, rear_path: str, parent=None):
        super().__init__(parent)
        self._front = front_path
        self._rear = rear_path
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            check: PairCheck = compare_pair_full(
                self._front, self._rear, cancel_event=self._cancel, progress=self.status.emit)
        except Exception as exc:  # noqa: BLE001 - 어떤 예외든 UI에 알린다
            self.failed.emit(str(exc))
            return
        self.finished_check.emit(check)
