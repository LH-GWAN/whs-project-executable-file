from __future__ import annotations

import os
from typing import Optional, Set

from PySide6.QtCore import QEventLoop, QSettings, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QStackedWidget,
)

from core import geocode
from core.appinfo import APP_NAME, SETTINGS_APP, SETTINGS_ORG
from core.case_deletion import case_folder_of, delete_cases
from core.appconfig import (
    MAP_MODE_OFFLINE,
    MAP_SERVER_PREFERRED_PORTS,
    get_map_mode,
    reset_map_mode,
    set_map_mode,
)
from core.pipeline import PipelineResult, reopen_case, update_case_json
from report.report_builder import ReportExporter, render_report_html
from storage.history_store import HistoryStore, default_app_data_dir
from ui.address_resolver import AddressResolver
from ui.analysis_view import AnalysisView
from ui.basemap_notice import should_show_notice, show_basemap_notice
from ui.case_edit_dialog import CaseEditDialog
from ui.case_info_dialog import CaseInfoDialog
from ui.home_view import HomeView
from ui.map_mode_dialog import ask_map_mode
from ui.map_server import MapServer
from ui.online_keys_notice import (
    should_show_notice as should_show_keys_notice,
    show_online_keys_notice,
)
from ui.workers import AnalysisWorker, HashWorker


class MainWindow(QMainWindow):
    def __init__(self, app_data_dir: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 720)

        self._app_data_dir = app_data_dir or default_app_data_dir()
        self._cases_root_dir = os.path.join(self._app_data_dir, "cases")
        self._history_db_path = os.path.join(self._app_data_dir, "history.db")
        os.makedirs(self._cases_root_dir, exist_ok=True)

        self._home = HomeView()
        self._home.video_selected.connect(self._on_video_selected)
        self._home.history_item_opened.connect(self._on_history_item_opened)
        self._home.history_delete_requested.connect(self._on_history_delete_requested)
        self._home.history_clear_requested.connect(self._on_history_clear_requested)
        self._home.history_edit_requested.connect(self._on_history_edit_requested)

        self._analysis_view = AnalysisView()
        self._analysis_view.home_requested.connect(self._show_home)
        self._analysis_view.report_requested.connect(self._on_report_requested)
        self._analysis_view.online_map_failed.connect(self._on_online_map_failed)

        AddressResolver.instance().failed.connect(self._on_address_failed)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._home)
        self._stack.addWidget(self._analysis_view)
        self.setCentralWidget(self._stack)

        # 메뉴바는 눈에 안 띄어 찾기 어렵다는 피드백으로, 홈 화면 우측 상단의 '⚙ 설정' 버튼에
        # 메뉴를 단다. 메뉴바는 만들지 않는다.
        settings_menu = QMenu("설정", self)
        self._settings_menu = settings_menu
        map_mode_action = QAction("지도 사용 방식…", self)
        map_mode_action.triggered.connect(self._on_map_mode_action)
        settings_menu.addAction(map_mode_action)
        keys_action = QAction("온라인 지도 키 설정 안내…", self)
        keys_action.triggered.connect(self._on_keys_notice_action)
        settings_menu.addAction(keys_action)
        basemap_action = QAction("오프라인 지도 파일 안내…", self)
        basemap_action.triggered.connect(lambda: show_basemap_notice(self))
        settings_menu.addAction(basemap_action)
        settings_menu.addSeparator()
        reset_action = QAction("지도 설정 초기화 (처음 실행처럼 다시 묻기)", self)
        reset_action.triggered.connect(self._on_reset_map_settings)
        settings_menu.addAction(reset_action)
        # 메뉴 항목 위에서도 손가락 커서가 보이게 (기본은 화살표라 눌리는지 알기 어렵다)
        settings_menu.setCursor(Qt.PointingHandCursor)
        self._home.set_settings_menu(settings_menu)

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
        # 오프라인인데 지도 파일이 없으면 받는 방법을, 온라인인데 키가 없으면
        # 발급받아 넣는 방법을 안내한다. 둘 다 없어도 분석은 정상 동작한다.
        self._show_map_resource_notice(mode)

    def _show_map_resource_notice(self, mode: str) -> None:
        if mode == MAP_MODE_OFFLINE and should_show_notice():
            show_basemap_notice(self)
        elif mode != MAP_MODE_OFFLINE and should_show_keys_notice():
            # 안내 창에서 [다시 확인]으로 키가 인식되면 떠 있는 지도를 온라인으로 바꿔 띄운다.
            if show_online_keys_notice(self):
                self._analysis_view.reload_maps()

    def _on_keys_notice_action(self) -> None:
        if show_online_keys_notice(self, allow_suppress=False):
            self._analysis_view.reload_maps()

    def _on_map_mode_action(self) -> None:
        current = get_map_mode()
        mode = ask_map_mode(self, current)
        if mode != current:
            self._apply_map_mode(mode)

    def _on_reset_map_settings(self) -> None:
        """지도 사용 방식 선택과 '다시 표시하지 않음' 표시를 지우고 첫 실행 절차를 다시 밟는다.

        이 값들은 %LOCALAPPDATA%/IDAS 와 레지스트리(QSettings)에 있어서 clean 스크립트나
        재빌드로는 지워지지 않는다. 사건 이력·증거 폴더는 건드리지 않는다.
        """
        previous = get_map_mode()
        reset_map_mode()
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        settings.remove("basemap_notice/suppressed")
        settings.remove("online_keys_notice/suppressed")
        self._online_notified.clear()
        self._first_run_setup()
        if get_map_mode() != previous:
            self._apply_map_mode(get_map_mode() or MAP_MODE_OFFLINE)

    def _apply_map_mode(self, mode: str) -> None:
        set_map_mode(mode)
        # 방식을 다시 골랐으면 이전 실패(한도 초과 등)를 잊고 다시 시도한다.
        geocode.reset_block()
        AddressResolver.instance().reset()
        self._online_notified.clear()
        self._analysis_view.reload_maps()
        self._show_map_resource_notice(mode)

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

    def _compute_hash_with_progress(self, video_path: str) -> tuple:
        """영상 해시를 진행률 창과 함께 계산한다. (해시, 오류) - 취소하면 ("", ""), 실패하면 ("", 사유)."""
        progress = QProgressDialog("파일 확인 중 (SHA-256)...", "취소", 0, 100, self)
        progress.setWindowTitle(APP_NAME)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)
        worker = HashWorker(video_path, self)
        worker.progress.connect(progress.setValue)
        result = {"sha": "", "error": "", "cancelled": False}
        loop = QEventLoop()

        def on_cancel() -> None:
            result["cancelled"] = True
            worker.cancel()
            if worker.isRunning():
                # 같은 canceled 시그널에 먼저 연결된 QProgressDialog.cancel()이 창을 이미 숨겼다.
                # 읽기가 막혀 있으면 스레드가 바로 안 끝나므로, 끝날 때까지 모달 창을 다시 띄워
                # 홈 화면 조작을 막는다.
                progress.setLabelText("취소하는 중...")
                progress.show()

        def on_done(sha: str) -> None:
            result["sha"] = sha

        def on_failed(message: str) -> None:
            result["error"] = message

        # QProgressDialog는 close()할 때도 canceled를 내고 wasCanceled()가 True가 되므로,
        # 사용자가 누른 취소만 따로 기록한다. 루프 종료는 결과 시그널이 아니라 스레드 종료에
        # 걸어, 워커가 어떤 경로로 끝나든 여기서 멈추지 않게 한다.
        progress.canceled.connect(on_cancel)
        worker.finished_hash.connect(on_done)
        worker.failed.connect(on_failed)
        worker.finished.connect(loop.quit)
        # 계산하는 동안 홈 화면을 잠가 Upload를 또 누르는 중첩 진입을 막는다.
        self._home.setEnabled(False)
        try:
            worker.start()
            loop.exec()
            if not worker.wait(2000):
                # 앱 종료 등으로 루프가 먼저 끝났는데 읽기가 아직 막혀 있으면, 실행 중인 QThread를
                # 지우는 순간 프로세스가 죽는다("Destroyed while thread is still running"). 끝날 때까지 기다린다.
                worker.wait()
        finally:
            self._home.setEnabled(True)
            progress.canceled.disconnect(on_cancel)
            progress.close()
            worker.deleteLater()
        if result["cancelled"]:
            return "", ""
        return result["sha"], result["error"]

    def _confirm_reanalysis(self, previous: list) -> bool:
        """같은 파일을 이미 분석한 적이 있을 때. 예: 새 사건으로 다시 분석, 아니요: 홈으로."""
        shown = previous[:5]
        lines = [f"  · {c.case_number} ({c.created_at})" for c in shown]
        if len(previous) > len(shown):
            lines.append(f"  · … 외 {len(previous) - len(shown)}건")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("이미 분석한 파일")
        box.setText("이미 분석한 파일입니다. 새롭게 다시 분석하시겠습니까?")
        box.setInformativeText(
            "같은 내용(SHA-256 동일)의 영상을 분석한 이력이 있습니다:\n" + "\n".join(lines) + "\n\n"
            "'예'를 누르면 새 사건으로 다시 분석합니다(기존 이력은 그대로 남습니다).\n"
            "'아니요'를 누르면 홈으로 돌아갑니다. 기존 결과는 History에서 열 수 있습니다.")
        yes_btn = box.addButton("예", QMessageBox.YesRole)
        no_btn = box.addButton("아니요", QMessageBox.NoRole)
        box.setDefaultButton(no_btn)
        box.setEscapeButton(no_btn)
        box.exec()
        return box.clickedButton() is yes_btn

    def _on_video_selected(self, video_path: str, rear_path: str = "") -> None:
        sha256, error = self._compute_hash_with_progress(video_path)
        if error:
            QMessageBox.critical(
                self, "파일 읽기 실패",
                f"영상 파일을 읽을 수 없습니다:\n{video_path}\n\n{error}\n\n"
                "이동식 매체가 빠졌거나 네트워크 경로가 끊겼는지, 읽기 권한이 있는지 확인하세요.")
            return
        if not sha256:
            return  # 취소
        with HistoryStore(self._history_db_path) as store:
            previous = store.find_cases_by_sha256(sha256)
        if previous and not self._confirm_reanalysis(previous):
            return

        dialog = CaseInfoDialog(self)
        if dialog.exec() != CaseInfoDialog.Accepted or dialog.result_input is None:
            return
        info = dialog.result_input
        self._pending_settings = info.settings
        self._pending_case_number = info.case_number
        self._pending_examiner = info.examiner
        self._pending_memo = info.memo

        self._progress = QProgressDialog("분석 준비 중...", "취소", 0, 0, self)
        self._progress.setWindowTitle(APP_NAME)
        # 모달로 띄워 분석 중에 Home의 삭제 조작이 안 되게 한다. 진행 중인 사건의 레코드가
        # 지워지면 워커가 마지막에 외래키 오류로 죽고 사건 폴더만 고아로 남는다.
        self._progress.setWindowModality(Qt.WindowModal)
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
            sha256=sha256,
            rear_video_path=rear_path or "",
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

    def _on_history_edit_requested(self, case_id: int) -> None:
        with HistoryStore(self._history_db_path) as store:
            case = store.get_case(case_id)
        if case is None:
            return
        dialog = CaseEditDialog(case.case_number, case.examiner, case.memo, self)
        if dialog.exec() != CaseEditDialog.Accepted or dialog.result_values is None:
            return
        number, examiner, memo = dialog.result_values
        with HistoryStore(self._history_db_path) as store:
            store.update_case_info(case_id, number, examiner, memo)
        case_folder = os.path.dirname(case.output_folder) if case.output_folder else ""
        update_case_json(case_folder, number, examiner, memo)
        self._refresh_history()
        if self._current_case_id == case_id:
            self._current_case_number = number
            self._current_examiner = examiner
            self._current_memo = memo
            self._analysis_view.set_case_number(number)

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

    def _analysis_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _refuse_delete_while_running(self) -> bool:
        if not self._analysis_running():
            return False
        QMessageBox.information(self, "이력 삭제",
                                "분석이 진행 중일 때는 이력을 지울 수 없습니다. 분석이 끝난 뒤 다시 시도하세요.")
        return True

    def _on_history_delete_requested(self, case_ids: list) -> None:
        if self._refuse_delete_while_running():
            return
        with HistoryStore(self._history_db_path) as store:
            cases = [c for c in (store.get_case(int(i)) for i in case_ids) if c is not None]
        if cases:
            self._confirm_and_delete(cases, clear_all=False)

    def _on_history_clear_requested(self) -> None:
        if self._refuse_delete_while_running():
            return
        with HistoryStore(self._history_db_path) as store:
            cases = store.list_cases()
        if cases:
            self._confirm_and_delete(cases, clear_all=True)

    def _confirm_and_delete(self, cases: list, clear_all: bool) -> None:
        """되돌릴 수 없는 동작이라 무엇을 지우는지 보여주고 확인받는다. 기본 버튼은 취소."""
        n = len(cases)
        shown = cases[:8]
        lines = [f"  · {c.case_number} - {c.source_video_filename} ({c.created_at})" for c in shown]
        if n > len(shown):
            lines.append(f"  · … 외 {n - len(shown)}건")
        with_folder = sum(1 for c in cases if case_folder_of(c))

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("이력 전체 삭제" if clear_all else "이력 삭제")
        box.setText(("이력 전체 " if clear_all else "선택한 이력 ") + f"{n}건을 삭제합니다. 되돌릴 수 없습니다.")
        box.setInformativeText(
            "\n".join(lines) + "\n\n"
            "사건 폴더에는 원본 영상 사본, 엔진 산출물(CSV·로그), case.json이 들어 있습니다.\n"
            "함께 지우면 이 사건은 다시 열 수 없고, 남기면 목록에서만 사라지고 폴더는 그대로\n"
            "남습니다. 직접 다른 곳에 저장한 리포트 PDF는 어느 쪽이든 지우지 않습니다."
        )
        folder_check = QCheckBox(f"사건 폴더도 함께 삭제 ({with_folder}건)")
        folder_check.setChecked(with_folder > 0)
        folder_check.setEnabled(with_folder > 0)
        box.setCheckBox(folder_check)
        delete_btn = box.addButton("전체 삭제" if clear_all else "삭제", QMessageBox.DestructiveRole)
        cancel_btn = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        if box.clickedButton() is not delete_btn:
            return

        remove_folders = folder_check.isChecked()
        ids = [c.id for c in cases]
        if self._analysis_running():
            return
        # 재생기는 마지막으로 Tracker를 켠 사건의 영상을 계속 잡고 있을 수 있다(Tracker를 끈
        # 사건을 열면 재생기를 건드리지 않는다). 어느 사건을 지우든 먼저 잠금을 푼다 -
        # Windows에서는 열린 파일이 있으면 폴더 삭제가 실패한다.
        self._analysis_view.release_media()
        if self._current_case_id in ids:
            self._current_case_id = None
        with HistoryStore(self._history_db_path) as store:
            results = delete_cases(store, self._cases_root_dir, ids, remove_folders)
        self._refresh_history()

        failed = [r for r in results if not r.ok]
        if failed:
            kept = [r for r in failed if not r.record_removed]
            detail = "\n\n".join(f"· {r.case_number or r.case_id}: {r.error}" for r in failed[:6])
            summary = f"{len(results) - len(failed)}건 삭제, {len(failed)}건은 문제가 있었습니다."
            if kept:
                summary += f"\n목록에 남은 {len(kept)}건은 원인을 해결한 뒤 다시 지우면 됩니다."
            QMessageBox.warning(self, "일부 삭제 실패", f"{summary}\n\n{detail}")

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
