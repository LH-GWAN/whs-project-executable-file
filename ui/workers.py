from __future__ import annotations

import threading
from typing import Dict, Optional

from PySide6.QtCore import QThread, Signal

from core import hashing
from core.pipeline import PipelineResult, run_analysis_pipeline
from engine.engine_adapter import CancelledError
from storage.history_store import HistoryStore


class HashWorker(QThread):
    """영상 SHA-256을 화면 밖에서 계산한다. 같은 파일을 다시 올렸는지 확인하는 데 쓴다."""

    progress = Signal(int)        # 0~100
    finished_hash = Signal(str)   # 취소하면 빈 문자열
    failed = Signal(str)          # 파일을 읽지 못함 (사유)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        def on_progress(done: int, total: int) -> bool:
            self.progress.emit(int(done * 100 / total) if total else 100)
            return not self._cancel.is_set()

        try:
            self.finished_hash.emit(hashing.sha256_file(self._path, progress_cb=on_progress))
        except Exception as exc:  # noqa: BLE001 - 어떤 이유든 화면에 알려야 한다
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class AnalysisWorker(QThread):
    progress = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, video_path: str, case_number: str, examiner: str, memo: str,
                 settings: Dict, cases_root_dir: str, history_db_path: Optional[str],
                 accel_threshold_mps2: float, carve_slack: bool = False,
                 sha256: str = "", rear_video_path: str = "", track_mode: str = "", parent=None):
        super().__init__(parent)
        self._video_path = video_path
        self._sha256 = sha256
        self._rear_video_path = rear_video_path
        self._track_mode = track_mode
        self._case_number = case_number
        self._examiner = examiner
        self._memo = memo
        self._settings = settings
        self._cases_root_dir = cases_root_dir
        self._history_db_path = history_db_path
        self._accel_threshold_mps2 = accel_threshold_mps2
        self._carve_slack = carve_slack
        self._cancel_event = threading.Event()

    def run(self) -> None:
        try:
            with HistoryStore(self._history_db_path) as store:
                result: PipelineResult = run_analysis_pipeline(
                    video_path=self._video_path,
                    case_number=self._case_number,
                    examiner=self._examiner,
                    memo=self._memo,
                    settings=self._settings,
                    cases_root_dir=self._cases_root_dir,
                    history_store=store,
                    accel_threshold_mps2=self._accel_threshold_mps2,
                    carve_slack=self._carve_slack,
                    cancel_event=self._cancel_event,
                    progress_cb=self.progress.emit,
                    precomputed_sha256=self._sha256,
                    rear_video_path=self._rear_video_path,
                    track_mode=self._track_mode,
                )
            self.finished_ok.emit(result)
        except CancelledError:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def cancel(self) -> None:
        self._cancel_event.set()
