from __future__ import annotations

import json
import math
from typing import List, Optional

from PySide6.QtCore import QBuffer, QIODevice, QTimer, QUrl, Signal
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from core.acceleration import FlaggedSegment
from engine.engine_adapter import TrackPoint
from ui.map_server import ONLINE_PAGE, MapServer

_ONLINE_POLL_MS = 700
_ONLINE_POLL_LIMIT_MS = 40000
_BASELINE_POLL_MS = 500
_BASELINE_IDLE_WAIT_LIMIT_MS = 8000   # 타일이 이만큼 안 와도 일단 찍는다


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """진북 기준 방위각(0~360, 시계 방향)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compute_headings(points: List[TrackPoint]) -> List[Optional[float]]:
    """지점별 진행 방향(도). 지도 위치 마커를 화살표로 그리기 위한 값.

    이동 중에는 실측 진행각 또는 직전 좌표로 추정한다. 정지 중에는 마지막 방향을
    유지하고, 처음부터 방향을 모르면 None(원형 마커). 5초 초과 공백은 연결하지 않는다.
    """
    out = [None] * len(points)
    last = None
    previous = None
    for i, p in enumerate(points):
        if not p.has_fix or p.start_time_sec is None or not math.isfinite(p.start_time_sec):
            continue
        if previous is not None and not 0 <= p.start_time_sec - previous.start_time_sec <= 5.0:
            last = None
            previous = None
        moving = p.speed_kmh is not None and math.isfinite(p.speed_kmh) and p.speed_kmh >= 3.0
        if moving and p.track_deg is not None and math.isfinite(p.track_deg):
            last = p.track_deg % 360.0
        elif moving and previous is not None:
            from core.outliers import haversine_m
            if haversine_m(previous.latitude, previous.longitude, p.latitude, p.longitude) >= 2.0:
                last = _bearing_deg(previous.latitude, previous.longitude, p.latitude, p.longitude)
        out[i] = last
        previous = p
    return out


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

        # 리포트용 기준 지도. 궤적을 그리고 화면을 맞춘 직후의 모습을 한 번 찍어 둔다.
        # 사용자가 이후 확대·축소해도 리포트에는 이 그림이 들어간다(검토 의견: 축소된
        # 상태로 리포트를 만들면 경로가 점 하나로 나왔다).
        self._baseline_png: Optional[bytes] = None
        self._baseline_track_id = 0
        self._baseline_poll = QTimer(self)
        self._baseline_poll.setInterval(_BASELINE_POLL_MS)
        self._baseline_poll.timeout.connect(self._poll_baseline)
        self._baseline_waited_ms = 0

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
            self._pending_js = [s for s in (self._last_track_js, self._last_time_js) if s]

    def set_track(self, points: List[TrackPoint],
                   segments: Optional[List[FlaggedSegment]] = None) -> None:
        self._pending_js = []
        headings = compute_headings(points)
        payload = {
            "points": [
                {
                    "t": p.start_time_sec,
                    # 이상치는 좌표를 지도에 주지 않는다(궤적이 튀는 원인). 끊김과는 구분해 o=1.
                    "lat": p.latitude if p.has_fix else None,
                    "lon": p.longitude if p.has_fix else None,
                    "v": p.speed_kmh,
                    "d": 1 if p.is_dropout else 0,
                    "o": 1 if p.is_outlier else 0,
                    "h": headings[i],
                }
                for i, p in enumerate(points)
            ],
            "flagged": [[s.start_index, s.end_index, getattr(s, "kind", "accel")]
                        for s in (segments or [])],
        }
        self._last_track_js = f"renderTrack({json.dumps(payload, ensure_ascii=False)});"
        self._last_time_js = None
        self._run_js(self._last_track_js)
        self._baseline_png = None
        self._baseline_track_id += 1
        self._baseline_waited_ms = 0
        self._baseline_poll.start()

    def baseline_png(self) -> Optional[bytes]:
        """분석 직후(전체 경로가 화면에 맞춰진 상태)의 지도 그림. 아직 못 찍었으면 None."""
        return self._baseline_png

    def _poll_baseline(self) -> None:
        if self._baseline_png is not None:
            self._baseline_poll.stop()
            return
        if not self._loaded or not self._view.isVisible():
            return  # 탭이 보일 때까지 기다린다(안 보이는 웹뷰는 빈 그림이 찍힌다)
        self._baseline_waited_ms += _BASELINE_POLL_MS
        self._view.page().runJavaScript(
            "(window.__mapReady && window.__trackDrawn) ? (window.__trackIdle ? 'idle' : 'wait') : 'no'",
            0, self._on_baseline_state)

    def _on_baseline_state(self, state) -> None:
        if self._baseline_png is not None or not self._baseline_poll.isActive():
            return
        if state == "idle" or (state == "wait" and self._baseline_waited_ms >= _BASELINE_IDLE_WAIT_LIMIT_MS):
            self._baseline_poll.stop()
            track_id = self._baseline_track_id
            self._view.page().runJavaScript("setCaptureMode(true);")
            QTimer.singleShot(250, lambda: self._capture_baseline(track_id))

    def _capture_baseline(self, track_id: int) -> None:
        try:
            if track_id == self._baseline_track_id and self._loaded and self._view.isVisible():
                self._baseline_png = self.grab_png()
        finally:
            if self._loaded:
                self._view.page().runJavaScript("setCaptureMode(false);")
        if self._baseline_png is None and track_id == self._baseline_track_id:
            self._baseline_poll.start()  # 못 찍었으면 다시 기다린다

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
