from __future__ import annotations

import os
from typing import Optional, Set

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox, QProgressDialog, QStackedWidget

from core import geocode
from core.appconfig import (
    MAP_MODE_OFFLINE,
    MAP_SERVER_PREFERRED_PORTS,
    get_map_mode,
    set_map_mode,
)
from core.pipeline import PipelineResult, reopen_case
from report.report_builder import ReportExporter, render_report_html
from storage.history_store import HistoryStore, default_app_data_dir
from ui.address_resolver import AddressResolver
from ui.analysis_view import AnalysisView
from ui.basemap_notice import should_show_notice, show_basemap_notice
from ui.case_info_dialog import CaseInfoDialog
from ui.home_view import HomeView
from ui.map_mode_dialog import ask_map_mode
from ui.map_server import MapServer
from ui.workers import AnalysisWorker


class MainWindow(QMainWindow):
    def __init__(self, app_data_dir: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GPS Tracer")
        self.resize(1100, 720)

        self._app_data_dir = app_data_dir or default_app_data_dir()
        self._cases_root_dir = os.path.join(self._app_data_dir, "cases")
        self._history_db_path = os.path.join(self._app_data_dir, "history.db")
        os.makedirs(self._cases_root_dir, exist_ok=True)

        self._home = HomeView()
        self._home.video_selected.connect(self._on_video_selected)
        self._home.history_item_opened.connect(self._on_history_item_opened)

        self._analysis_view = AnalysisView()
        self._analysis_view.home_requested.connect(self._show_home)
        self._analysis_view.report_requested.connect(self._on_report_requested)
        self._analysis_view.online_map_failed.connect(self._on_online_map_failed)

        AddressResolver.instance().failed.connect(self._on_address_failed)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._home)
        self._stack.addWidget(self._analysis_view)
        self.setCentralWidget(self._stack)

        settings_menu = self.menuBar().addMenu("설정")
        map_mode_action = QAction("지도 사용 방식…", self)
        map_mode_action.triggered.connect(self._on_map_mode_action)
        settings_menu.addAction(map_mode_action)

        self._worker: Optional[AnalysisWorker] = None
        self._progress: Optional[QProgressDialog] = None
        self._report_exporter: Optional[ReportExporter] = None

        self._current_case_id: Optional[int] = None
        self._current_case_number: str = ""
        self._current_examiner: str = ""
        self._current_memo: str = ""
        self._current_settings: dict = {}

        self._pending_settings: dict = {}
        self._pending_case_number: str = ""
        self._pending_examiner: str = ""
        self._pending_memo: str = ""

        # 온라인 지도 실패 안내는 원인별로 한 번만 띄운다. 두 탭의 지도가 같은 이유로
        # 동시에 실패하면 같은 창이 두 번 뜨기 때문이다.
        self._online_notified: Set[str] = set()

        self._refresh_history()

        # 배경지도가 없으면 처음 한 번 안내한다. 없어도 분석은 정상 동작하므로
        # 막는 게 아니라 알려주기만 하고, 사용자가 끄면 다시 띄우지 않는다.
        QTimer.singleShot(0, self._first_run_setup)

    def _first_run_setup(self) -> None:
        # 처음 실행이면 지도 사용 방식부터 고르게 한다. 외부 접속 여부는 사용자가
        # 알고 선택해야 하는 사항이라 조용히 정하지 않는다.
        mode = get_map_mode()
        if mode is None:
            mode = ask_map_mode(self)
        # 오프라인인데 지도 파일이 없으면 받는 방법을 안내한다.
        if mode == MAP_MODE_OFFLINE and should_show_notice():
            show_basemap_notice(self)

    def _on_map_mode_action(self) -> None:
        current = get_map_mode()
        mode = ask_map_mode(self, current)
        if mode != current:
            self._apply_map_mode(mode)

    def _apply_map_mode(self, mode: str) -> None:
        set_map_mode(mode)
        # 방식을 다시 골랐으면 이전 실패(한도 초과 등)를 잊고 다시 시도한다.
        geocode.reset_block()
        AddressResolver.instance().reset()
        self._online_notified.clear()
        self._analysis_view.reload_maps()
        if mode == MAP_MODE_OFFLINE and should_show_notice():
            show_basemap_notice(self)

    def _on_online_map_failed(self, kind: str, message: str) -> None:
        if kind in self._online_notified:
            return
        self._online_notified.add(kind)

        origin = MapServer.instance().base_url
        registered = "\n".join(f"  http://127.0.0.1:{p}" for p in MAP_SERVER_PREFERRED_PORTS)
        texts = {
            "quota": (
                "온라인 지도 사용 한도 초과",
                "카카오맵 일일 사용 한도를 초과하여 오늘은 온라인 지도를 사용할 수 없습니다.\n"
                "궤적은 배경지도 없이 표시됩니다.\n\n"
                "오프라인 지도로 바꾸면 계속 사용할 수 있습니다 (설정 > 지도 사용 방식).",
            ),
            "domain": (
                "온라인 지도 도메인 미등록",
                "카카오 디벨로퍼스에 이 프로그램의 주소가 등록되어 있지 않아 온라인 지도를\n"
                f"사용할 수 없습니다.\n\n현재 주소: {origin}\n\n"
                "카카오 디벨로퍼스 > 내 애플리케이션 > [앱] > [플랫폼 키] > 프로그램이 쓰는\n"
                "JavaScript 키의 [JavaScript SDK 도메인]에 아래 주소를 모두 등록한 뒤 프로그램을\n"
                f"다시 시작하세요:\n{registered}\n\n"
                "※ [제품 링크 관리 > 웹 도메인]이나 다른 JavaScript 키에 등록하면 인식되지 않습니다.",
            ),
            "disabled": (
                "카카오맵 사용 설정 꺼짐",
                "카카오 디벨로퍼스 앱에서 카카오맵 사용 설정이 꺼져 있어 온라인 지도를 사용할 수\n"
                "없습니다.\n\n카카오 디벨로퍼스 > 내 애플리케이션 > 카카오맵 > 사용 설정을 ON으로\n"
                "바꾼 뒤 프로그램을 다시 시작하세요.",
            ),
            "key": (
                "온라인 지도 키 오류",
                "카카오맵 JavaScript 키가 없거나 올바르지 않습니다.\n"
                "assets/online_keys.json 의 kakao_js_key 값을 확인하세요.",
            ),
            "network": (
                "온라인 지도 연결 실패",
                "인터넷에 연결할 수 없어 온라인 지도를 불러오지 못했습니다.\n"
                "궤적은 배경지도 없이 표시됩니다.",
            ),
        }
        title, text = texts.get(kind, ("온라인 지도 오류", "온라인 지도를 불러오지 못했습니다."))
        if message:
            text += f"\n\n(서버 응답: {message})"

        box = QMessageBox(QMessageBox.Warning, title, text, parent=self)
        switch_btn = box.addButton("오프라인 지도로 전환", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.setDefaultButton(switch_btn if kind == "quota" else None)
        box.exec()
        if box.clickedButton() is switch_btn:
            self._apply_map_mode(MAP_MODE_OFFLINE)

    def _on_address_failed(self, status: str, message: str) -> None:
        if status == geocode.STATUS_QUOTA:
            title = "주소 표시 한도 초과"
            text = ("카카오 주소 변환 API 일일 사용 한도를 초과하여 오늘은 주소가 표시되지 않습니다.\n"
                    "지도와 좌표는 계속 사용할 수 있습니다.")
        else:
            title = "주소 표시 사용 불가"
            text = ("REST API 키가 올바르지 않거나 카카오맵 사용 설정이 꺼져 있어 주소를 조회할 수\n"
                    "없습니다. assets/online_keys.json 의 kakao_rest_key 값과 카카오 디벨로퍼스\n"
                    "앱 설정을 확인하세요.")
        if message:
            text += f"\n\n(서버 응답: {message})"
        QMessageBox.warning(self, title, text)

    def _refresh_history(self) -> None:
        with HistoryStore(self._history_db_path) as store:
            self._home.set_history(store.list_cases())

    def _show_home(self) -> None:
        self._refresh_history()
        self._stack.setCurrentWidget(self._home)

    def _on_video_selected(self, video_path: str) -> None:
        dialog = CaseInfoDialog(self)
        if dialog.exec() != CaseInfoDialog.Accepted or dialog.result_input is None:
            return
        info = dialog.result_input
        self._pending_settings = info.settings
        self._pending_case_number = info.case_number
        self._pending_examiner = info.examiner
        self._pending_memo = info.memo

        self._progress = QProgressDialog("분석 준비 중...", "취소", 0, 0, self)
        self._progress.setWindowTitle("GPS Tracer")
        self._progress.setMinimumDuration(0)
        self._progress.canceled.connect(self._on_cancel_requested)
        self._progress.show()

        self._worker = AnalysisWorker(
            video_path=video_path,
            case_number=info.case_number,
            examiner=info.examiner,
            memo=info.memo,
            settings=info.settings,
            cases_root_dir=self._cases_root_dir,
            history_db_path=self._history_db_path,
            accel_threshold_mps2=info.accel_threshold_mps2,
            carve_slack=info.carve_slack,
        )
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.finished_ok.connect(self._on_worker_finished)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.cancelled.connect(self._on_worker_cancelled)
        self._worker.start()

    def _on_worker_progress(self, message: str) -> None:
        if self._progress is not None:
            self._progress.setLabelText(message)

    def _on_worker_finished(self, result: PipelineResult) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        extraction = result.extraction
        if not extraction.succeeded:
            box = QMessageBox.warning if extraction.status == "no_gps" else QMessageBox.critical
            box(self, "분석 결과", extraction.status_message)
        self._current_case_id = result.case_id
        self._current_case_number = self._pending_case_number
        self._current_examiner = self._pending_examiner
        self._current_memo = self._pending_memo
        self._current_settings = self._pending_settings
        self._analysis_view.load_result(result, self._pending_case_number, self._pending_settings)
        self._stack.setCurrentWidget(self._analysis_view)

    def _on_cancel_requested(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._progress is not None:
            self._progress.setLabelText("취소하는 중...")

    def _on_worker_cancelled(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        self._refresh_history()

    def _on_worker_failed(self, message: str) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        QMessageBox.critical(self, "분석 실패", f"분석 중 오류가 발생했습니다:\n{message}")

    def _on_history_item_opened(self, case_id: int) -> None:
        with HistoryStore(self._history_db_path) as store:
            case = store.get_case(case_id)
            if case is None:
                return
            store.touch_last_opened(case_id)
        result = reopen_case(case)
        self._current_case_id = case.id
        self._current_case_number = case.case_number
        self._current_examiner = case.examiner
        self._current_memo = case.memo
        self._current_settings = case.analysis_settings
        self._analysis_view.load_result(result, case.case_number, case.analysis_settings)
        self._stack.setCurrentWidget(self._analysis_view)

    def _on_report_requested(self, result: PipelineResult) -> None:
        default_name = f"{self._current_case_number or 'case'}_report.pdf"
        out_path, _ = QFileDialog.getSaveFileName(self, "리포트 저장", default_name, "PDF (*.pdf)")
        if not out_path:
            return

        chart_png, map_png = self._analysis_view.capture_visuals()
        html_str = render_report_html(
            result, self._current_case_number, self._current_examiner, self._current_memo,
            chart_png=chart_png, map_png=map_png,
        )

        def on_done(success: bool, error_message: str) -> None:
            exporter, self._report_exporter = self._report_exporter, None
            if exporter is not None:
                exporter.deleteLater()
            if not success:
                QMessageBox.critical(self, "Report", f"리포트 생성에 실패했습니다: {error_message}")
                return
            if self._current_case_id is not None:
                with HistoryStore(self._history_db_path) as store:
                    store.set_report_path(self._current_case_id, out_path)
            QMessageBox.information(self, "Report", f"리포트를 저장했습니다:\n{out_path}")

        self._report_exporter = ReportExporter(html_str, out_path, on_done, parent=self)

    def closeEvent(self, event):  # noqa: N802
        AddressResolver.instance().stop()
        MapServer.shutdown_if_running()
        super().closeEvent(event)
