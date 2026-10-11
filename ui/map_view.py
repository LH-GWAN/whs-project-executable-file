from __future__ import annotations

import json
import math
import time
from typing import List, Optional

from PySide6.QtCore import QBuffer, QEventLoop, QIODevice, Qt, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from core.driving_events import DISPLAY_ORDER, EVENT_PRIORITY, DrivingEvent
from engine.engine_adapter import TrackPoint
from ui.map_server import ONLINE_PAGE, MapServer

_ONLINE_POLL_MS = 700
_ONLINE_POLL_LIMIT_MS = 40000
_BASELINE_POLL_MS = 500
_BASELINE_IDLE_WAIT_LIMIT_MS = 8000   # 타일이 이만큼 안 와도 일단 찍는다
_BASELINE_NO_LIMIT_MS = 30000         # 지도가 이만큼 준비되지 않으면 기준 그림을 포기한다(리뷰 #113)
_RELOAD_RESET_SEC = 60.0              # 렌더러 크래시 뒤 이만큼 조용하면 재시도 횟수를 되돌린다(리뷰 #81)


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


def _event_line_colors(points: List[TrackPoint], events: List[DrivingEvent]) -> List[Optional[str]]:
    """행마다 '이 행으로 들어오는 선분'의 색. 선분(앞 행→이 행)이 이벤트 구간 안에 있을 때만 그 색이다 -
    구간의 첫 행으로 들어오는 선분은 아직 이벤트가 아니다. 예전엔 행마다 색을 주고 양 끝이 같을 때만
    칠해서 30→42→27의 급감속 선분이 초록, 기준 미달 선분이 빨강이 됐다(리뷰 #79). 겹치면
    EVENT_PRIORITY(급정지·급출발·급가속·급감속) 앞쪽이 이긴다 - 표·그래프와 같은 순서다."""
    rank = {k: n for n, k in enumerate(EVENT_PRIORITY)}
    best: List[Optional[DrivingEvent]] = [None] * len(points)
    for ev in events:
        for i in range(max(1, ev.start_index + 1), min(len(points), ev.end_index + 1)):
            if best[i] is None or rank.get(ev.kind, 99) < rank.get(best[i].kind, 99):
                best[i] = ev
    return [ev.color if ev is not None else None for ev in best]


def _event_markers(points: List[TrackPoint], events: List[DrivingEvent]) -> List[dict]:
    """위험운전마다 지도에 붙일 이름표. 구간 안 좌표 중 가운데 지점에 단다."""
    out = []
    for ev in events:
        fixes = [i for i in range(max(0, ev.start_index), min(len(points), ev.end_index + 1))
                 if points[i].has_fix]
        if not fixes:
            continue
        p = points[fixes[len(fixes) // 2]]
        out.append({"label": ev.label, "color": ev.color, "lat": p.latitude, "lon": p.longitude,
                    "detail": f"{ev.label} · {ev.start_time_sec:.1f}~{ev.end_time_sec:.1f}초 · {ev.detail}"})
    return out


def _segment_ends(points: List[TrackPoint]) -> List[List[float]]:
    """이어보기에서 영상마다 마지막 좌표(검은 점). 영상이 하나면 빈 목록 - 지도가 알아서 끝점을 찍는다."""
    last = {}
    for p in points:
        if p.has_fix:
            last[p.segment_index] = [p.latitude, p.longitude]
    return [last[k] for k in sorted(last)] if len(last) > 1 else []


# 손상 구간 선 색. 급가속(#e03131)과 같은 빨강이어서 범례에 같은 빨간 선이 두 줄 나왔다(리뷰 #55).
# 지도 페이지(map.html / map_kakao.html)의 선 색과 같아야 한다.
DAMAGED_COLOR = "#212529"
INTERPOLATED_COLOR = "#9c36b5"
_SYNTHETIC_STATES = ("damaged", "interpolated")


def is_synthetic_point(p: TrackPoint) -> bool:
    """손상 처리로 만든 합성 행인가 - 'damaged'(빈 행)·'interpolated'(추정 좌표). 실측이 아니므로 화면에서
    '(손상 구간)'·'(추정)'으로 구분하고, 추정 좌표로는 주소 조회·외부 지도 링크를 하지 않는다(리뷰 #21).
    'gap_carved'는 손상 구간에서 회수한 실측이라 합성이 아니다. TrackPoint.is_synthetic이 있으면 그 값을 쓴다."""
    flag = getattr(p, "is_synthetic", None)
    if isinstance(flag, bool):
        return flag
    try:
        from core.acceleration import is_synthetic   # D2와 같은 판별
        return is_synthetic(p)
    except (ImportError, AttributeError):
        return getattr(p, "recovery_state", "") in _SYNTHETIC_STATES


def _damage_legend(points: List[TrackPoint]) -> List[List[str]]:
    """손상 파일에서만 범례에 올린다. [이름, 색, 선 모양] - 지도 위 선과 같은 점선으로 그린다(리뷰 #55)."""
    states = {p.recovery_state for p in points}
    out = []
    if "interpolated" in states:
        out.append(["임의 복구 구간(추정 좌표)", INTERPOLATED_COLOR, "dotted"])
    if "damaged" in states:
        out.append(["손상 구간(복구 안 함)", DAMAGED_COLOR, "dashed"])
    return out


def _event_legend(events: List[DrivingEvent]) -> List[List[str]]:
    """이 궤적에 나온 위험운전 종류만 범례에 올린다([이름, 색])."""
    present = {ev.kind: ev for ev in events}
    return [[present[k].label, present[k].color] for k in DISPLAY_ORDER if k in present]


class _MapPage(QWebEnginePage):
    """지도 페이지는 우리 로컬 서버의 지도 페이지로만 이동할 수 있다. 뒤로 가기·드롭·링크로 다른
    페이지(특히 온라인에서 오프라인으로 바꾼 뒤 기록에 남은 카카오 페이지)가 열리면 좌표가 외부로
    나갈 수 있다(리뷰 #3). 외부 스크립트(SDK)는 하위 자원이라 이 검사에 걸리지 않는다."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.allowed_url = ""

    def acceptNavigationRequest(self, url, nav_type, is_main_frame) -> bool:  # noqa: N802
        if not is_main_frame:
            return True
        return url.toString().split("?", 1)[0] == self.allowed_url


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
        self._last_crash_at = -1e9
        self._last_track_js: Optional[str] = None
        self._last_time_js: Optional[str] = None

        self._view = QWebEngineView(self)
        self._page = _MapPage(self._view)
        self._view.setPage(self._page)
        self._view.setContextMenuPolicy(Qt.NoContextMenu)   # 우클릭 '뒤로'·'새로고침' 등 차단
        try:
            self._page.settings().setAttribute(QWebEngineSettings.NavigateOnDropEnabled, False)
        except AttributeError:
            pass
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
        url = MapServer.instance().map_url()
        self._page.allowed_url = url
        self._view.load(QUrl(url))

    def is_online_page(self) -> bool:
        return self._view.url().path().endswith("/" + ONLINE_PAGE)

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            # acceptNavigationRequest가 막은 이동도 loadFinished(False)로 온다. 그때는 우리 페이지가 그대로
            # 떠 있으므로 _loaded를 지우지 않는다 - 지우면 재생 위치가 지도에 전달되지 않았다(리뷰 #130).
            # 우리 페이지 자체를 못 불러온 경우는 _load_page가 이미 False로 둔 상태다.
            if self._view.url().toString().split("?", 1)[0] != self._page.allowed_url:
                self._loaded = False
            return
        self._loaded = True
        # 지금 허용된 페이지가 아니면(기록·드롭 등으로 다른 페이지가 열렸으면) 궤적을 주지 않고 되돌린다.
        if self._view.url().toString().split("?", 1)[0] != self._page.allowed_url:
            self._loaded = False
            QTimer.singleShot(0, self._load_page)
            return
        self._view.history().clear()   # 뒤로 가기로 이전(온라인) 페이지가 다시 열리지 않게
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
            # 준비도 실패도 안 오면(SDK 콜백 없음) 신호 없이 멈추던 것을 실패로 알린다(리뷰 #83).
            self.online_map_failed.emit("network", "온라인 지도가 응답하지 않습니다(시간 초과)")
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
        now = time.monotonic()
        # 지난 크래시 뒤 이만큼 조용했으면 새 사고로 보고 횟수를 되돌린다. 로드 성공 시점에 되돌리면
        # 크래시→재로드가 늘 60초 안이라 영영 되돌아가지 않았고(리뷰 #109), 로드 성공만으로 되돌리면
        # 궤적이 렌더러를 죽일 때 무한 반복했다(리뷰 #81). 크래시 시점끼리 비교하면 둘 다 피한다.
        if now - self._last_crash_at > _RELOAD_RESET_SEC:
            self._reload_attempts = 0
        self._last_crash_at = now
        if self._reload_attempts >= self.MAX_RELOAD_ATTEMPTS:
            # 같은 궤적에서 계속 죽으면 그 궤적은 다시 넣지 않는다(리뷰 #81).
            self._last_track_js = None
            self._pending_js = []
            return
        self._reload_attempts += 1
        QTimer.singleShot(600, self._load_page)

    def _run_js(self, script: str) -> None:
        if self._loaded:
            self._view.page().runJavaScript(script)
        else:
            self._pending_js = [s for s in (self._last_track_js, self._last_time_js) if s]

    def set_track(self, points: List[TrackPoint],
                   events: Optional[List[DrivingEvent]] = None, baseline: bool = True) -> None:
        """baseline=False면 리포트용 기준 그림을 건드리지 않는다(Location에서 영상별·슬랙 묶음을
        볼 때). 기준 그림은 사건의 본(Composed) 궤적에서만 찍는다 - 예전엔 묶음을 바꿀 때마다 다시
        찍어서 마지막에 본 슬랙 궤적이 '전체 경로'로 리포트에 실렸다(리뷰 #13)."""
        self._pending_js = []
        headings = compute_headings(points)
        events = events or []
        line_colors = _event_line_colors(points, events)
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
                    # 검증 실패(checksum·불신)로 뺀 좌표. 수신 없음과 구분해 안내한다(리뷰 #111).
                    "u": 1 if (p.has_coords and not p.is_outlier
                               and (p.gps_checksum_ok is False or p.gps_trusted is False)) else 0,
                    "h": headings[i],
                    # 이 행으로 들어오는 선분이 위험운전 구간이면 그 종류의 선 색.
                    "e": line_colors[i],
                    # 이어보기의 영상 번호. 영상이 바뀌는 곳은 선을 잇지 않는다.
                    "s": p.segment_index,
                    # 손상 파일 처리 상태: "damaged"(빈 행) / "interpolated"(임의 복구) / "gap_carved" / "".
                    "r": p.recovery_state,
                }
                for i, p in enumerate(points)
            ],
            "segEnds": _segment_ends(points),
            "events": _event_markers(points, events),
            "legend": _event_legend(events) + _damage_legend(points),
        }
        self._last_track_js = f"renderTrack({json.dumps(payload, ensure_ascii=False)});"
        self._last_time_js = None
        self._run_js(self._last_track_js)
        # 지금 그려진 궤적이 바뀌었으니 진행 중이던 캡처는 무효다. 기준 그림은 본 궤적일 때만 새로 찍고,
        # 좌표가 하나도 없으면 찍지 않는다(빈 지도나 이전 사건 지역이 '전체 경로'로 실리던 리뷰 #14).
        self._baseline_track_id += 1
        self._baseline_poll.stop()
        if not baseline:
            return
        self._baseline_png = None
        self._baseline_waited_ms = 0
        if any(p.has_fix for p in points):
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
        if self._baseline_waited_ms >= _BASELINE_NO_LIMIT_MS:
            self._baseline_poll.stop()   # 지도가 끝내 준비되지 않으면(온라인 실패 등) 멈춘다(리뷰 #113)
            return
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

    def _eval_js(self, script: str, timeout_ms: int = 1500):
        """페이지 안의 식을 동기로 평가한다(응답이 없으면 None). 캡처처럼 결과를 바로 써야 하는 곳에서만 쓴다."""
        if not self._loaded:
            return None
        loop = QEventLoop()
        result = {"value": None, "done": False}

        def on_value(value) -> None:
            result["value"] = value
            result["done"] = True
            loop.quit()

        self._view.page().runJavaScript(script, 0, on_value)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        return result["value"] if result["done"] else None

    def is_map_ready(self) -> bool:
        """페이지가 떠 있고 지도 라이브러리까지 준비돼 궤적이 그려진 상태인가. 온라인 지도는 SDK가 실패하면
        페이지만 뜨고 궤적을 그리지 않으므로 페이지 로드 여부만으로는 모른다(리뷰 #56)."""
        return self._eval_js("(window.__mapReady && window.__trackDrawn) ? 'yes' : 'no'") == "yes"

    def grab_png(self) -> Optional[bytes]:
        """현재 지도 화면을 PNG로 캡처한다. 리포트에 넣기 위한 것.

        지도가 아직 안 떴거나 그려지지 않았으면 None을 돌려준다 - 빈 이미지를
        리포트에 넣는 것보다 아예 넣지 않는 편이 낫다. 온라인 지도가 실패해 회색 화면만 있는 경우도
        같다 - 예전엔 페이지 로드만 보고 빈 지도를 '전체 경로'로 리포트에 실었다(리뷰 #56).
        """
        if not self._loaded or not self.is_map_ready():
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
