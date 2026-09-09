from __future__ import annotations

import json
from typing import List, Optional

from PySide6.QtCore import QBuffer, QIODevice, QTimer, QUrl, Signal
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from core.acceleration import FlaggedSegment
from engine.engine_adapter import TrackPoint
from ui.map_server import ONLINE_PAGE, MapServer

_ONLINE_POLL_MS = 700
_ONLINE_POLL_LIMIT_MS = 40000


class MapView(QWidget):
    MAX_RELOAD_ATTEMPTS = 3

    # 온라인 지도(카카오맵)를 띄우지 못했을 때 (원인 종류, 서버 메시지).
    # 종류는 core/kakao_api.py의 KIND_* 값이다.
    online_map_failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loaded = False
        self._pending_js: List[str] = []
        self._reload_attempts = 0
        self._last_track_js: Optional[str] = None
        self._last_time_js: Optional[str] = None

        self._view = QWebEngineView(self)
        self._view.loadFinished.connect(self._on_load_finished)
        self._view.page().renderProcessTerminated.connect(self._on_render_process_gone)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        self._load_started = False

        # 온라인 페이지는 SDK를 인터넷에서 받아오므로 준비/실패가 나중에 결정된다.
        # 페이지 -> Python 방향 채널이 없으니(README 참고) 상태 변수를 잠시 폴링한다.
        self._online_poll = QTimer(self)
        self._online_poll.setInterval(_ONLINE_POLL_MS)
        self._online_poll.timeout.connect(self._poll_online_state)
        self._online_poll_elapsed = 0

    def ensure_loaded(self) -> None:
        if not self._load_started:
            self._load_page()

    def reload(self) -> None:
        """지도 사용 방식이 바뀐 뒤 부른다. 아직 안 띄운 지도는 다음에 보일 때 새 방식으로 뜬다."""
        if self._load_started:
            self._load_page()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.ensure_loaded()

    def _load_page(self) -> None:
        self._loaded = False
        self._load_started = True
        self._online_poll.stop()
        self._view.load(QUrl(MapServer.instance().map_url()))

    def is_online_page(self) -> bool:
        return self._view.url().path().endswith("/" + ONLINE_PAGE)

    def _on_load_finished(self, ok: bool) -> None:
        self._loaded = bool(ok)
        if not ok:
            return
        self._reload_attempts = 0
        pending = self._pending_js
        self._pending_js = []
        if pending:
            for script in pending:
                self._view.page().runJavaScript(script)
        else:
            for script in (self._last_track_js, self._last_time_js):
                if script:
                    self._view.page().runJavaScript(script)
        if self.is_online_page():
            self._online_poll_elapsed = 0
            self._online_poll.start()

    def _poll_online_state(self) -> None:
        self._online_poll_elapsed += _ONLINE_POLL_MS
        if self._online_poll_elapsed > _ONLINE_POLL_LIMIT_MS:
            self._online_poll.stop()
            return
        self._view.page().runJavaScript(
            "(window.__onlineMapState || '') + '|' + (window.__onlineMapError || '')",
            0, self._on_online_state)

    def _on_online_state(self, value) -> None:
        if not isinstance(value, str):
            return
        state, _, message = value.partition("|")
        if state == "ready":
            self._online_poll.stop()
        elif state.startswith("error:"):
            self._online_poll.stop()
            self.online_map_failed.emit(state[len("error:"):], message)

    def _on_render_process_gone(self, status, exit_code: int) -> None:
        self._loaded = False
        if self._reload_attempts >= self.MAX_RELOAD_ATTEMPTS:
            return
        self._reload_attempts += 1
        QTimer.singleShot(600, self._load_page)

    def _run_js(self, script: str) -> None:
        if self._loaded:
            self._view.page().runJavaScript(script)
        else:
            self._pending_js.append(script)

    def set_track(self, points: List[TrackPoint],
                   segments: Optional[List[FlaggedSegment]] = None) -> None:
        payload = {
            "points": [
                {
                    "t": p.start_time_sec,
                    "lat": p.latitude,
                    "lon": p.longitude,
                    "v": p.speed_kmh,
                    "d": 1 if p.is_dropout else 0,
                }
                for p in points
            ],
            "flagged": [[s.start_index, s.end_index] for s in (segments or [])],
        }
        self._last_track_js = f"renderTrack({json.dumps(payload, ensure_ascii=False)});"
        self._last_time_js = None
        self._run_js(self._last_track_js)

    def grab_png(self) -> Optional[bytes]:
        """현재 지도 화면을 PNG로 캡처한다. 리포트에 넣기 위한 것.

        지도가 아직 안 떴거나 그려지지 않았으면 None을 돌려준다 - 빈 이미지를
        리포트에 넣는 것보다 아예 넣지 않는 편이 낫다.
        """
        if not self._loaded:
            return None
        pixmap = self._view.grab()
        if pixmap.isNull() or pixmap.width() < 2 or pixmap.height() < 2:
            return None
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if not pixmap.save(buffer, "PNG"):
            return None
        return bytes(buffer.data())

    def set_playback_time(self, seconds: float) -> None:
        self._last_time_js = f"setPlaybackTime({float(seconds)});"
        self._run_js(self._last_time_js)
