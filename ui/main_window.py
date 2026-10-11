from __future__ import annotations

import inspect
import os
from typing import Optional, Set

from PySide6.QtCore import QEventLoop, QSettings, QStandardPaths, Qt, QTimer
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
from core.video_tracks import same_view, view_tag
from core.appconfig import (
    MAP_MODE_OFFLINE,
    MAP_SERVER_PREFERRED_PORTS,
    get_map_mode,
    reset_map_mode,
    set_map_mode,
)
from core.location_table import has_frame_detail
from core.pipeline import PipelineResult, update_case_json
from engine.engine_adapter import detect_slack
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
from ui.workers import AnalysisWorker, HashWorker, ReopenWorker


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
        QTimer.singleShot(0, self._notify_db_recovery)
        self._home.video_selected.connect(self._on_video_selected)
        self._home.sequence_selected.connect(self._on_sequence_selected)
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
        self._reopen_worker: Optional[ReopenWorker] = None   # 사건 다시 열기(리뷰 #45)
        self._reopen_progress: Optional[QProgressDialog] = None
        # 마지막으로 시작한 분석의 인자. 이식 사본에서 슬랙이 확인되면 슬랙 추출을 켜고 다시 돌리는 데 쓴다(리뷰 #85).
        self._last_analysis_args: dict = {}
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

    # settings.json에 지도 사용 방식을 저장하지 못했을 때 레지스트리(QSettings)에 남기는 표시. 다음 실행에서
    # 파일에 남은 이전 값(온라인일 수 있다)을 쓰지 않고 다시 묻는다(리뷰 #47). 저장이 되면 지운다.
    _MODE_UNSAVED_KEY = "map_mode/unsaved"

    @classmethod
    def _mode_save_pending(cls) -> bool:
        return bool(QSettings(SETTINGS_ORG, SETTINGS_APP).value(cls._MODE_UNSAVED_KEY, False, type=bool))

    @classmethod
    def _record_mode_saved(cls, ok: bool) -> None:
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        if ok:
            settings.remove(cls._MODE_UNSAVED_KEY)
        else:
            settings.setValue(cls._MODE_UNSAVED_KEY, True)
        settings.sync()

    def _first_run_setup(self) -> None:
        # 처음 실행이면 지도 사용 방식부터 고르게 한다. 외부 접속 여부는 사용자가
        # 알고 선택해야 하는 사항이라 조용히 정하지 않는다.
        mode = get_map_mode()
        if mode is None or self._mode_save_pending():
            # 지난 실행에서 저장에 실패했으면 파일의 옛 값으로 시작하지 않고 다시 묻는다(리뷰 #47).
            mode = ask_map_mode(self, mode)
            # ask_map_mode는 저장 결과를 돌려주지 않는다. 같은 값을 한 번 더 써서 성공 여부를 기록한다.
            self._record_mode_saved(set_map_mode(mode))
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
            geocode.reset_block()   # 키를 고쳤으면 이전 차단(401 등)을 풀고 다시 시도한다(리뷰 #104)
            AddressResolver.instance().reset()
            self._analysis_view.reload_maps()

    def _on_map_mode_action(self) -> None:
        current = get_map_mode()
        mode = ask_map_mode(self, current)
        if mode != current:
            self._apply_map_mode(mode)
        else:
            # 같은 방식을 다시 골라도 차단(한도 초과·키 오류)을 풀어 다시 시도하게 한다(리뷰 #104).
            geocode.reset_block()
            AddressResolver.instance().reset()

    def _on_reset_map_settings(self) -> None:
        """지도 사용 방식 선택과 '다시 표시하지 않음' 표시를 지우고 첫 실행 절차를 다시 밟는다.

        이 값들은 %LOCALAPPDATA%/IDAS 와 레지스트리(QSettings)에 있어서 clean 스크립트나
        재빌드로는 지워지지 않는다. 사건 이력·증거 폴더는 건드리지 않는다.
        """
        previous = get_map_mode()
        if not reset_map_mode():
            QMessageBox.warning(self, "설정 초기화 실패",
                                "settings.json에서 지도 사용 방식을 지우지 못했습니다(읽기 전용?).\n"
                                "이번 실행에서는 다시 묻지만, 저장이 될 때까지 다음 실행에서도 매번 다시 묻습니다.")
            self._record_mode_saved(False)
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        settings.remove("basemap_notice/suppressed")
        settings.remove("online_keys_notice/suppressed")
        self._online_notified.clear()
        self._first_run_setup()
        if get_map_mode() != previous:
            self._apply_map_mode(get_map_mode() or MAP_MODE_OFFLINE)

    def _apply_map_mode(self, mode: str) -> None:
        saved = set_map_mode(mode)
        self._record_mode_saved(saved)
        if not saved:
            QMessageBox.warning(self, "설정 저장 실패",
                                "지도 사용 방식을 settings.json에 저장하지 못했습니다(읽기 전용?). 이번 실행에는 적용되지만\n"
                                "파일에는 남지 않아, 저장이 될 때까지 다음 실행마다 다시 묻습니다.")
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
                "온라인 지도 화면에는 궤적도 표시되지 않고, 리포트에 지도 그림도 들어가지 않습니다.\n\n"
                "오프라인 지도로 바꾸면 궤적을 볼 수 있습니다 (설정 > 지도 사용 방식).",
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
                "온라인 지도 화면에는 궤적도 표시되지 않고, 리포트에 지도 그림도 들어가지 않습니다.\n"
                "오프라인 지도로 바꾸면 궤적을 볼 수 있습니다 (설정 > 지도 사용 방식).",
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
        # 조회 중에 손상이 드러나 DB를 옆으로 옮기고 새로 만든 경우도 같은 안내를 한다(리뷰 #57).
        self._notify_recovered_db()

    def _notify_recovered_db(self) -> None:
        """HistoryStore가 손상된 history.db를 옆으로 옮겨 두고 빈 이력으로 다시 만들었으면 한 번 알린다."""
        aside = HistoryStore.recovered_from
        if not aside:
            return
        HistoryStore.recovered_from = ""
        QMessageBox.warning(
            self, "사건 이력 복구",
            "사건 이력 DB(history.db)가 손상돼 열 수 없어 옆에 옮겨 두고 빈 이력으로 시작했습니다.\n"
            f"옮긴 파일: {aside}\n\n사건 폴더(cases\\)의 사본·산출물·case.json은 그대로 있습니다.")

    def _show_home(self) -> None:
        self._analysis_view._tracker_tab.stop()
        self._refresh_history()
        self._stack.setCurrentWidget(self._home)

    def _compute_hash_with_progress(self, video_path: str, label: str = "") -> tuple:
        """영상 해시를 진행률 창과 함께 계산한다. (해시, 오류) - 취소하면 ("", ""), 실패하면 ("", 사유)."""
        progress = QProgressDialog(f"파일 확인 중 (SHA-256)...{label}", "취소", 0, 100, self)
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
        lines = []
        for c in shown:
            tag = view_tag(c.track_mode, bool(c.rear_video_filename))
            lines.append(f"  · {c.case_number} ({c.created_at})" + (f" - {tag}" if tag else ""))
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

    def _on_video_selected(self, video_path: str, rear_path: str = "", track_mode: str = "") -> None:
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
        # 같은 파일이라도 보기 방식(전방만/후방만/같이)이 다르면 다른 분석이다 - 경고하지 않는다.
        previous = [c for c in previous
                    if same_view(c.track_mode, track_mode) and not c.segments
                    and bool(c.rear_video_filename) == bool(rear_path)]   # 후방 유무도 같아야 같은 분석(리뷰 #141)
        if previous and not self._confirm_reanalysis(previous):
            return

        # 손상 검사: 파일 전체를 훑어 손상이면 알리고 복구 방식을 고르게 한다(사용자 결정 2026-10-11).
        damage = self._check_damage(video_path, "전방 영상" if rear_path else "")
        if damage is None:
            return
        report, mode, reference = damage
        extra_settings = {}
        pair_check = self._home.take_pair_check() if hasattr(self._home, "take_pair_check") else {}
        if rear_path and pair_check:
            extra_settings["pair_check"] = pair_check   # 손상 때문에 대조 없이 받은 후방(리뷰 #131)
        if rear_path:
            # 후방도 검사한다(리뷰 #101). 후방은 GPS 추출 대상이 아니라 복구 방식을 고르지 않고 '복구하지 않음'으로
            # 원본을 그대로 튼다. 손상 사실은 사건 설정(analysis_settings)에 남겨 화면·사건 정보에 보인다.
            rear_damage = self._check_rear_damage(rear_path)
            if rear_damage is None:
                return
            if rear_damage:
                extra_settings["rear_damage"] = rear_damage
        carve = self._ask_slack([("전방 영상" if rear_path else "영상", video_path)])
        if carve is None:
            return
        self._start_analysis(video_path=video_path, sha256=sha256, carve_slack=carve,
                             extra_settings=extra_settings,
                             rear_video_path=rear_path or "", track_mode=track_mode or "",
                             damage_report=report, recovery_mode=mode, reference_path=reference)

    def _check_rear_damage(self, rear_path: str) -> Optional[dict]:
        """후방 영상 손상 검사. 취소면 None, 정상이면 {}, 손상이면 사건 설정에 남길 dict(계속할 때)."""
        report = self._scan_damage_with_progress(rear_path, "후방 영상")
        if report is None:
            return None
        if not report.damaged:
            return {}
        lines = list(report.summary_lines())
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("손상 파일 - 후방 영상")
        box.setText(f"{os.path.basename(rear_path)}\n\n후방 영상이 손상된 파일입니다.")
        box.setInformativeText(
            "\n".join(f"· {line}" for line in lines)
            + "\n\n후방 영상은 GPS를 뽑지 않으므로 복구하지 않고 원본을 그대로 재생합니다(손상 구간은 깨져 보이거나 "
              "재생되지 않을 수 있습니다). 손상 사실은 사건 정보에 남깁니다.")
        go = box.addButton("계속 (복구하지 않음)", QMessageBox.AcceptRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(go)
        box.exec()
        if box.clickedButton() is not go:
            return None
        return {"damaged": True, "mode": "none", "summary": lines, "report": report.to_dict()}

    def _on_sequence_selected(self, items: list) -> None:
        """연속 영상 이어보기: 영상마다 해시를 내고 사건 정보를 받아 분석한다."""
        paths = []
        for item in items:
            for path in (item.front, item.rear):
                if path and path not in paths:
                    paths.append(path)
        hashes = {}
        for n, path in enumerate(paths, start=1):
            sha256, error = self._compute_hash_with_progress(
                path, f"\n{n}/{len(paths)} {os.path.basename(path)}")
            if error:
                QMessageBox.critical(
                    self, "파일 읽기 실패",
                    f"영상 파일을 읽을 수 없습니다:\n{path}\n\n{error}")
                return
            if not sha256:
                return  # 취소
            hashes[path] = sha256
        # 이어보기는 손상 파일을 받지 않는다(구간마다 손상 처리·헤더 이식이 얽힌다). 하나씩 분석하게 안내.
        for n, item in enumerate(items, start=1):
            report = self._scan_damage_with_progress(item.primary, f"{n}번 영상")
            if report is None:
                return
            if report.damaged:
                QMessageBox.warning(
                    self, "손상 파일",
                    f"{n}번 영상({os.path.basename(item.primary)})이 손상된 파일입니다:\n"
                    + "\n".join(f"· {line}" for line in report.summary_lines())
                    + "\n\n손상 영상은 연속 영상 이어보기로 분석할 수 없습니다. 영상 하나씩 분석하세요.")
                return
        carve = self._ask_slack([(f"{n}번 영상", item.primary) for n, item in enumerate(items, start=1)])
        if carve is None:
            return
        self._start_analysis(video_path=items[0].primary, sha256=hashes[items[0].primary],
                             carve_slack=carve, sequence=items, sequence_sha256=hashes)

    def _run_task_with_progress(self, fn, text: str) -> dict:
        """오래 걸리는 함수 fn(cancel_event, progress_cb)를 진행 창과 함께 화면 밖에서 돌린다.
        {"value": 결과, "error": 실패 사유, "cancelled": 취소 여부}.

        진행 창의 exec() 대신 QEventLoop로 스레드가 끝날 때까지 기다린다. 예전엔 취소를 누르면 창이 바로 닫히고
        worker.wait()에서 GUI 스레드가 막혔다(리뷰 #48) - 취소 확인이 느린 단계(수 GB 파일 판별)에서 수 초 동안
        응답이 없었다. 취소를 누르면 '취소하는 중...'을 띄워 둔 채 루프를 계속 돌려 홈 화면 조작만 막는다."""
        from ui.workers import TaskWorker

        progress = QProgressDialog(text, "취소", 0, 0, self)
        progress.setWindowTitle(APP_NAME)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        worker = TaskWorker(fn, self)
        result = {"value": None, "error": "", "cancelled": False}
        loop = QEventLoop()

        def done(value) -> None:
            result["value"] = value

        def failed(message: str) -> None:
            result["error"] = message

        def on_cancel() -> None:
            # QProgressDialog는 close()할 때도 canceled를 내므로, 아직 결과가 없을 때만 취소로 본다.
            if result["value"] is None and not result["error"] and worker.isRunning():
                result["cancelled"] = True
                worker.cancel()
                progress.setLabelText("취소하는 중...")
                progress.show()

        worker.finished_task.connect(done)
        worker.failed.connect(failed)
        worker.status.connect(progress.setLabelText)
        progress.canceled.connect(on_cancel)
        worker.finished.connect(loop.quit)
        self._home.setEnabled(False)
        try:
            worker.start()
            progress.show()
            loop.exec()
            if not worker.wait(2000):
                worker.wait()   # 실행 중인 QThread를 지우면 프로세스가 죽는다 - 끝날 때까지 기다린다
        finally:
            self._home.setEnabled(True)
            progress.canceled.disconnect(on_cancel)
            progress.close()
            worker.deleteLater()
            progress.deleteLater()
        if worker.is_cancelled():
            result["cancelled"] = True
        return result

    def _scan_damage_with_progress(self, path: str, label: str = ""):
        """파일 전체를 훑는 손상 검사를 진행 창과 함께 돌린다. 취소·실패면 None."""
        from core.damage import scan_damage

        result = self._run_task_with_progress(
            lambda cancel, _progress: scan_damage(path, cancel_event=cancel),
            f"파일 손상 검사 중... {label}".rstrip())
        if result["cancelled"]:
            return None
        if result["value"] is None:
            QMessageBox.critical(self, "손상 검사", f"파일 손상 검사에 실패했습니다:\n{result['error']}")
            return None
        return result["value"]

    @staticmethod
    def _call_with_cancel(fn, *args, cancel_event=None):
        """cancel_event 인자를 받는 함수면 넘기고, 아니면 그냥 부른다(core/damage_repair가 바뀌는 중)."""
        try:
            accepts = "cancel_event" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            accepts = False
        return fn(*args, cancel_event=cancel_event) if accepts else fn(*args)

    def _find_reference_candidates_with_progress(self, video_path: str, report) -> Optional[list]:
        """같은 폴더에서 참조 후보를 찾는다(이웃 파일을 읽으므로 화면 밖에서 - 리뷰 #48). 취소면 None."""
        from core.damage_repair import find_reference_candidates

        result = self._run_task_with_progress(
            lambda cancel, _progress: self._call_with_cancel(find_reference_candidates, video_path, report,
                                                             cancel_event=cancel),
            "같은 기기의 정상 영상(헤더 참조) 찾는 중...")
        if result["cancelled"]:
            return None
        if result["value"] is None:
            # 후보를 못 찾은 것으로 보고 직접 고르게 한다 - 탐색 실패가 업로드를 막을 이유는 아니다.
            return []
        return list(result["value"])

    def _validate_reference_with_progress(self, report, reference: str) -> Optional[tuple]:
        """참조 영상 검증(파일 구조를 읽는다 - 리뷰 #48). (ok, 사유), 취소면 None."""
        from core.damage_repair import validate_reference

        result = self._run_task_with_progress(
            lambda cancel, _progress: self._call_with_cancel(validate_reference, report, reference,
                                                             cancel_event=cancel),
            f"참조 영상 확인 중... {os.path.basename(reference)}")
        if result["cancelled"]:
            return None
        if result["value"] is None:
            return False, f"참조 영상을 읽지 못했습니다: {result['error']}"
        return tuple(result["value"])

    def _check_damage(self, video_path: str, label: str = ""):
        """손상 검사 → (검사 결과, 복구 방식, 참조 영상 경로). 취소면 None. 정상 파일이면 (report, 'none', '')."""
        from core.damage_marks import MODE_NONE
        from ui.damage_dialog import ask_recovery_mode, ask_reference, confirm_reference_problem

        report = self._scan_damage_with_progress(video_path, label)
        if report is None:
            return None
        if not report.damaged:
            return report, MODE_NONE, ""
        mode = ask_recovery_mode(self, report, label)
        if mode is None:
            return None
        reference = ""
        if report.needs_reference:
            if report.container == "mp4" and not report.fragmented:
                QMessageBox.information(
                    self, "헤더 참조 영상",
                    "moov(헤더)가 파일 끝에 있던 일반 MP4라 다른 영상의 헤더를 이식할 수 없습니다.\n"
                    "영상은 재생되지 않고, 남아 있는 GPS 문장만 UTC 기준 시간축으로 회수합니다.")
            else:
                candidates = self._find_reference_candidates_with_progress(video_path, report)
                if candidates is None:
                    return None
                while True:
                    reference, cancelled = ask_reference(self, report, candidates)
                    if cancelled:
                        return None
                    if reference is None:
                        continue   # 파일 선택 창만 닫음(취소 아님): 참조 창으로 돌아간다(리뷰 #51)
                    if not reference:
                        break
                    checked = self._validate_reference_with_progress(report, reference)
                    if checked is None:
                        return None
                    ok, reason = checked
                    if ok:
                        break
                    if confirm_reference_problem(self, reason):
                        reference = ""
                        break
                    # 실패한 후보만 빼고 남은 후보로 다시 묻는다 - 예전엔 전부 지워 '찾지 못했습니다'가 떴다(리뷰 #51).
                    failed_key = os.path.normcase(os.path.abspath(reference))
                    candidates = [c for c in candidates if os.path.normcase(os.path.abspath(c)) != failed_key]
        return report, mode, reference

    def _ask_slack(self, targets: list) -> Optional[bool]:
        """분석할 영상들의 슬랙(컨테이너가 참조하지 않는 영역) 유무를 보고, 있으면 따로 뽑을지 묻는다.
        True=뽑는다, False=안 뽑는다(없으면 묻지 않고 False), None=취소."""
        paths = [p for _label, p in targets]
        # 취소하면 cancel_event로 자식 프로세스를 끝낸다(예전엔 끝날 때까지 GUI가 멈췄다, 리뷰 #43).
        result = self._run_task_with_progress(
            lambda cancel, _progress: detect_slack(paths, cancel_event=cancel), "슬랙 데이터 확인 중...")
        if result["cancelled"]:
            return None
        info = result["value"]
        if info is None:
            QMessageBox.warning(self, "슬랙 데이터 확인", f"슬랙 데이터를 확인하지 못했습니다:\n{result['error']}\n\n"
                                "슬랙 추출 없이 계속합니다.")
            return False
        errors = [(label, info[p].get("error")) for label, p in targets if info.get(p, {}).get("error")]
        if errors:
            QMessageBox.warning(self, "슬랙 데이터 확인",
                                "일부 영상의 슬랙 데이터를 확인하지 못했습니다:\n"
                                + "\n".join(f"  · {label}: {err}" for label, err in errors))
        found = [(label, p, info[p]) for label, p in targets if info.get(p, {}).get("has_slack")]
        if not found:
            return False
        lines = [f"  · {label} ({os.path.basename(p)}): {d.get('slack_bytes', 0) / 1048576:.1f} MB"
                 for label, p, d in found]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("슬랙 데이터")
        box.setText("슬랙 데이터(영상 파일이 참조하지 않는 영역)가 있습니다. 따로 뽑을까요?")
        box.setInformativeText(
            "\n".join(lines) + "\n\n"
            "옛 녹화의 GPS가 남아 있을 수 있습니다. 뽑으면 본 분석과 별개로 '슬랙 데이터' 칸에서 "
            "지도·표·그래프로 볼 수 있습니다(영상은 없습니다). 원본 영상은 자르거나 고치지 않습니다.")
        yes = box.addButton("예, 따로 뽑기", QMessageBox.AcceptRole)
        box.addButton("아니요", QMessageBox.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes

    def _start_analysis(self, video_path: str, sha256: str, carve_slack: bool = False,
                        extra_settings: Optional[dict] = None, **worker_kwargs) -> None:
        if self._analysis_running():
            # 취소 직후 워커가 정리 중인 동안 새 분석을 시작하면 이전 QThread 객체가 실행 중에 덮여
            # abort됐다(리뷰 #15). 끝날 때까지 받지 않는다.
            QMessageBox.information(self, APP_NAME, "이전 분석이 아직 정리 중입니다. 잠시 뒤 다시 시도하세요.")
            return
        dialog = CaseInfoDialog(self)
        if dialog.exec() != CaseInfoDialog.Accepted or dialog.result_input is None:
            return
        info = dialog.result_input
        info.settings["carve_slack"] = carve_slack
        info.settings.update(extra_settings or {})
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

        self._last_analysis_args = dict(video_path=video_path, sha256=sha256, carve_slack=carve_slack,
                                        extra_settings=dict(extra_settings or {}), **worker_kwargs)
        self._worker = AnalysisWorker(
            parent=self,
            video_path=video_path,
            case_number=info.case_number,
            examiner=info.examiner,
            memo=info.memo,
            settings=info.settings,
            cases_root_dir=self._cases_root_dir,
            history_db_path=self._history_db_path,
            vehicle_type=info.vehicle_type,
            carve_slack=carve_slack,
            sha256=sha256,
            **worker_kwargs,
        )
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.finished_ok.connect(self._on_worker_finished)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.cancelled.connect(self._on_worker_cancelled)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_worker_done(self) -> None:
        """스레드가 완전히 끝난 뒤 객체를 지운다(실행 중 파괴 금지)."""
        worker = self.sender()
        if worker is self._worker:
            self._worker = None
        if worker is not None:
            worker.deleteLater()

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
        if self._pending_settings.get("location", True):
            self._notify_frame_detail(result)   # Location 탭이 없으면 안내하지 않는다(리뷰 #140)
        self._offer_slack_reanalysis(result)

    def _offer_slack_reanalysis(self, result: PipelineResult) -> None:
        """헤더 이식 사본에서 슬랙(옛 녹화 흔적)이 확인됐는데 슬랙 추출 없이 분석했으면 한 번 묻고, 예면 같은 인자로
        슬랙 추출을 켜서 다시 분석한다(리뷰 #85). 다시 열 때는 묻지 않는다(분석 직후에만)."""
        damage = result.damage or {}
        if not (damage.get("slack_available") and not damage.get("slack_carved")):
            return
        args = dict(self._last_analysis_args)
        if not args or args.get("carve_slack") or args.get("sequence"):
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("슬랙 데이터")
        box.setText("이식 사본에서 슬랙(옛 녹화 흔적)이 확인됐습니다. 슬랙 카빙 포함으로 다시 분석할까요?")
        box.setInformativeText("예를 누르면 같은 설정으로 새 사건을 만들어 슬랙 추출을 켜고 다시 분석합니다(지금 사건은 그대로 남습니다). "
                               "슬랙 GPS는 본 분석과 별개로 '슬랙 데이터' 칸에 표시됩니다.")
        yes = box.addButton("예, 다시 분석", QMessageBox.AcceptRole)
        box.addButton("아니요", QMessageBox.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        if box.clickedButton() is not yes:
            return
        args["carve_slack"] = True
        self._start_analysis(**args)

    def _notify_frame_detail(self, result: PipelineResult) -> None:
        """프레임·G센서 단위로 행을 쓰는 영상이면 Location Analysis에 상세보기가 생긴다고 알린다."""
        if result.is_sequence:
            numbers = [str(seg.index + 1) for seg in result.segments if has_frame_detail(seg.points)]
            if not numbers:
                return
            text = (f"{', '.join(numbers)}번 영상은 프레임 단위 기록이 있어 Location Analysis에서 "
                    "해당 영상을 고르면 상세보기(프레임 단위)를 켤 수 있습니다.")
        else:
            if not has_frame_detail(result.points):
                return
            text = ("이 영상은 프레임 단위 기록이 있어 Location Analysis에 상세보기가 추가됩니다.\n"
                    "기본은 1초(GPS 기록)마다 한 행이고, 상세보기를 켜면 모든 행이 보입니다.")
        QMessageBox.information(self, "상세보기", text)

    def _on_cancel_requested(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._progress is not None and self._analysis_running():
            # QProgressDialog는 취소를 누르면 스스로 숨는다. 워커가 실제로 멈출 때까지(복사 청크·해시
            # 단위로 취소를 확인한다) 모달을 다시 띄워 홈 조작·새 분석을 막는다(리뷰 #15).
            self._progress.setLabelText("취소하는 중...")
            self._progress.show()

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
        has_case_json = bool(case_folder) and os.path.isfile(os.path.join(case_folder, "case.json"))
        if has_case_json and not update_case_json(case_folder, number, examiner, memo):
            # case.json(DB 유실 대비 사본)에 반영하지 못하면 둘이 어긋난다. DB도 되돌리고 알린다(리뷰 #114).
            with HistoryStore(self._history_db_path) as store:
                store.update_case_info(case_id, case.case_number, case.examiner, case.memo)
            QMessageBox.warning(
                self, "사건 정보 수정 실패",
                f"사건 폴더의 case.json을 고치지 못해 수정을 취소했습니다(파일이 잠겨 있거나 읽기 전용?):\n"
                f"{os.path.join(case_folder, 'case.json')}\n\n원인을 해결한 뒤 다시 시도하세요.")
            self._refresh_history()
            return
        self._refresh_history()
        if self._current_case_id == case_id:
            self._current_case_number = number
            self._current_examiner = examiner
            self._current_memo = memo
            self._analysis_view.set_case_number(number)

    def _notify_db_recovery(self) -> None:
        """history.db가 손상돼 비켜 두고 새로 시작했으면 알린다(리뷰 #67)."""
        try:
            with HistoryStore(self._history_db_path):
                pass
        except Exception as exc:  # noqa: BLE001 - 옮기지도 못하면(권한) 이력 없이 계속 쓴다
            QMessageBox.warning(self, "사건 이력", f"사건 이력 DB를 열 수 없습니다:\n{exc}\n\n이력 없이 계속합니다.")
            return
        if HistoryStore.recovered_from:
            self._notify_recovered_db()
            self._refresh_history()   # 예전엔 없는 메서드(_refresh_home)를 불러 AttributeError가 났다(리뷰 #57)
        self._notify_migration()

    def _notify_migration(self) -> None:
        """데이터 폴더 이전(core/appinfo.migrate_legacy_app_data)에서 생긴 안내를 한 번 보여 준다(리뷰 #58)."""
        from core import appinfo
        notice = getattr(appinfo, "MIGRATION_NOTICE", "")
        if not notice:
            return
        appinfo.MIGRATION_NOTICE = ""
        QMessageBox.warning(self, "데이터 폴더 이전", notice)

    def _on_history_item_opened(self, case_id: int) -> None:
        if self._reopen_worker is not None and self._reopen_worker.isRunning():
            return   # 이미 여는 중
        try:
            with HistoryStore(self._history_db_path) as store:
                case = store.get_case(case_id)
                if case is None:
                    return
                try:
                    store.touch_last_opened(case_id)
                except Exception:  # noqa: BLE001 - 열람 시각 기록 실패(읽기 전용 DB)는 열기를 막지 않는다
                    pass
        except Exception as exc:  # noqa: BLE001 - 손상된 DB. 조용히 홈에 남지 않게 알린다(리뷰 #68)
            QMessageBox.critical(self, "사건 열기 실패", f"사건을 열지 못했습니다:\n{type(exc).__name__}: {exc}")
            return
        # manifest 대조·행 복원은 수십 초 걸릴 수 있어 워커에서 연다(리뷰 #45). 취소하면 홈에 남는다.
        progress = QProgressDialog(f"사건 여는 중... {case.case_number}", "취소", 0, 0, self)
        progress.setWindowTitle(APP_NAME)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        worker = ReopenWorker(case, self)
        self._reopen_worker = worker
        self._reopen_progress = progress

        def finish() -> None:
            if self._reopen_worker is worker:
                self._reopen_worker = None
                self._reopen_progress = None
            progress.close()
            progress.deleteLater()
            worker.deleteLater()

        def on_ok(result) -> None:
            finish()
            self._current_case_id = case.id
            self._current_case_number = case.case_number
            self._current_examiner = case.examiner
            self._current_memo = case.memo
            self._current_settings = case.analysis_settings
            self._analysis_view.load_result(result, case.case_number, case.analysis_settings)
            self._stack.setCurrentWidget(self._analysis_view)

        def on_failed(message: str) -> None:
            finish()
            QMessageBox.critical(self, "사건 열기 실패", f"사건을 열지 못했습니다:\n{message}")

        def on_cancel() -> None:
            if worker.isRunning():
                worker.cancel()
                progress.setLabelText("취소하는 중...")
                progress.show()

        worker.finished_ok.connect(on_ok)
        worker.failed.connect(on_failed)
        worker.cancelled.connect(finish)
        progress.canceled.connect(on_cancel)
        worker.start()
        progress.show()

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

    def _ask_report_format(self) -> str:
        """리포트를 무엇으로 뽑을지: pdf / csv / both. 취소면 빈 문자열."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Report")
        box.setText("리포트를 어떤 형식으로 저장할까요?")
        box.setInformativeText(
            "PDF: 사건 정보·위험운전·그래프·지도·좌표 표가 든 보고서\n"
            "CSV: Location Analysis 표 전체(모든 행)를 그대로 - 엑셀에서 열 수 있습니다")
        pdf = box.addButton("PDF", QMessageBox.AcceptRole)
        csv = box.addButton("CSV", QMessageBox.AcceptRole)
        both = box.addButton("PDF + CSV", QMessageBox.AcceptRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(pdf)
        box.exec()
        return {pdf: "pdf", csv: "csv", both: "both"}.get(box.clickedButton(), "")

    def _save_location_csv(self, result: PipelineResult, suggested: str) -> str:
        """Location Analysis 표를 CSV로. 저장한 경로(취소·실패면 빈 문자열)."""
        from core.location_table import write_csv

        out_path, _ = QFileDialog.getSaveFileName(self, "Location Analysis CSV 저장", suggested, "CSV (*.csv)")
        if not out_path:
            return ""
        labels = [seg.label for seg in result.segments] if result.is_sequence else None
        try:
            write_csv(out_path, result.points, result.driving_events, labels)
        except OSError as exc:
            QMessageBox.critical(self, "Report", f"CSV를 저장하지 못했습니다: {exc}")
            return ""
        return out_path

    def _on_report_requested(self, result: PipelineResult) -> None:
        fmt = self._ask_report_format()
        if not fmt:
            return
        if self._report_exporter is not None:
            QMessageBox.information(self, "Report", "이전 리포트를 아직 만드는 중입니다. 끝난 뒤 다시 시도하세요.")
            return
        base = self._report_basename()
        if fmt == "csv":
            saved = self._save_location_csv(result, os.path.join(self._report_dir(), f"{base}_location.csv"))
            if saved:
                QMessageBox.information(self, "Report", f"CSV를 저장했습니다:\n{saved}")
            return
        default_name = os.path.join(self._report_dir(), f"{base}_report.pdf")
        out_path, _ = QFileDialog.getSaveFileName(self, "리포트 저장", default_name, "PDF (*.pdf)")
        if not out_path:
            return
        self._last_report_dir = os.path.dirname(out_path)
        csv_path = ""
        if fmt == "both":
            csv_path = self._save_location_csv(result, os.path.splitext(out_path)[0] + "_location.csv")

        chart_png, map_png = self._analysis_view.capture_visuals()
        html_str = render_report_html(
            result, self._current_case_number, self._current_examiner, self._current_memo,
            chart_png=chart_png, map_png=map_png,
            analysis_settings=self._current_settings,   # 후방 손상(#101)·전후방 대조 불가(#131) 줄이 리포트에 실린다
        )

        # 완료 콜백은 '그때의' 사건 id와 익스포터를 쓴다. 만드는 동안 사건을 바꾸면 경로가 다른 사건에
        # 기록되던 문제(리뷰 #121). 만드는 동안 Report 버튼은 잠근다.
        case_id_at_request = self._current_case_id
        self._analysis_view.set_report_enabled(False)

        def on_done(success: bool, error_message: str) -> None:
            exporter, self._report_exporter = self._report_exporter, None
            if exporter is not None:
                exporter.deleteLater()
            self._analysis_view.set_report_enabled(True)
            if not success:
                QMessageBox.critical(self, "Report", f"리포트 생성에 실패했습니다: {error_message}")
                return
            if case_id_at_request is not None:
                with HistoryStore(self._history_db_path) as store:
                    store.set_report_path(case_id_at_request, out_path)
            QMessageBox.information(self, "Report", f"리포트를 저장했습니다:\n{out_path}"
                                    + (f"\n{csv_path}" if csv_path else ""))

        self._report_exporter = ReportExporter(html_str, out_path, on_done, parent=self)

    def _report_basename(self) -> str:
        """사건번호에서 파일 이름에 못 쓰는 문자를 걷어낸다."""
        raw = self._current_case_number or "case"
        safe = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in raw).strip(" .") or "case"
        return safe[:60]

    def _report_dir(self) -> str:
        """리포트 기본 저장 폴더: 마지막으로 저장한 곳 > 문서 폴더. 프로그램 폴더(cwd)는 쓰지 않는다 -
        dist\\IDAS에 저장된 리포트가 재빌드 때 지워졌다(리뷰 #64)."""
        last = getattr(self, "_last_report_dir", "")
        if last and os.path.isdir(last):
            return last
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        return docs or os.path.expanduser("~")

    def closeEvent(self, event):  # noqa: N802
        # 실행 중인 QThread를 파괴하면 abort된다. 분석 워커는 취소하고 끝날 때까지 기다린다(복사·해시가
        # 청크 단위로 취소를 확인하므로 곧 끝난다). 무결성 재해시 워커도 같다(리뷰 #15, #25).
        if self._worker is not None:
            self._worker.cancel()
            if self._worker.isRunning():
                self._worker.wait()
        if self._reopen_worker is not None:
            self._reopen_worker.cancel()
            if self._reopen_worker.isRunning():
                self._reopen_worker.wait()
        if self._report_exporter is not None:
            self._report_exporter.close()   # 만드는 중이던 리포트의 임시 HTML을 지운다(리뷰 #102)
        self._analysis_view.shutdown()
        AddressResolver.instance().stop()
        MapServer.shutdown_if_running()
        super().closeEvent(event)
