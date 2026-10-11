"""2026-10-11 코드 리뷰 반영 - UI·설정(담당 E) 회귀 테스트. 헤드리스(QT_QPA_PLATFORM=offscreen), 영상·네트워크 없음.

항목: #21 #46 #47 #48 #51 #55 #56 #70 #71 #72 #73 #78 #79 #97 #101 #109 #110 #111 #114 #116 #119 #130 #132
"""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QMessageBox, QWidget

from test_review_regressions import qapp, point  # noqa: F401 - 픽스처
from engine.engine_adapter import TrackPoint

ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------- 공통 스텁

class MapStub(QWidget):
    def set_track(self, *a, **k):
        pass

    def set_playback_time(self, t):
        self.seconds = t


class FakePlayer:
    """QMediaPlayer 중 TrackerTab의 탐색 경로가 쓰는 부분만 흉내 낸다(영상 파일 없이)."""

    def __init__(self, position=0, duration=60000, error=QMediaPlayer.NoError, seekable=True):
        self._pos = position
        self._dur = duration
        self._err = error
        self._seekable = seekable
        self._state = QMediaPlayer.PausedState
        self.calls = []
        self._status = QMediaPlayer.LoadedMedia

    class _Src:
        def __init__(self, empty):
            self._empty = empty

        def isEmpty(self):  # noqa: N802
            return self._empty

    def source(self):
        return self._Src(False)

    def isSeekable(self):  # noqa: N802
        return self._seekable

    def error(self):
        return self._err

    def duration(self):
        return self._dur

    def position(self):
        return self._pos

    def setPosition(self, p):  # noqa: N802
        self._pos = p
        self.calls.append(("seek", p))

    def playbackState(self):  # noqa: N802
        return self._state

    def play(self):
        self._state = QMediaPlayer.PlayingState
        self.calls.append(("play",))

    def pause(self):
        self._state = QMediaPlayer.PausedState
        self.calls.append(("pause",))

    def stop(self):
        self._state = QMediaPlayer.StoppedState

    def mediaStatus(self):  # noqa: N802
        return self._status

    def setSource(self, url):  # noqa: N802
        self.calls.append(("source", url))

    def metaData(self):  # noqa: N802
        raise RuntimeError("no metadata")


@pytest.fixture
def tracker(qapp, monkeypatch):
    from ui import tracker_tab
    monkeypatch.setattr(tracker_tab, "MapView", MapStub)
    monkeypatch.setattr(tracker_tab.geocode, "is_available", lambda: False)
    tab = tracker_tab.TrackerTab()
    yield tab
    tab.release_media()
    tab.deleteLater()
    qapp.processEvents()


def _two_segment_tracker(tracker, seg=1, local_ms=1010, **player_kwargs):
    """구간 2개(각 60초) 재생 목록. 재생기는 가짜로 바꿔 둔다."""
    from ui.tracker_tab import PlaylistItem
    tracker._playlist = [PlaylistItem(path="a.avi", duration_sec=60.0), PlaylistItem(path="b.avi", duration_sec=60.0)]
    tracker._durations_ms = [60000, 60000]
    tracker._recompute_offsets()
    tracker._seg = seg
    tracker._player = FakePlayer(position=local_ms, **player_kwargs)
    tracker._seek_slider.setRange(0, 120000)
    return tracker._player


# ---------------------------------------------------------------- #46 #97 설정

@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    from core import appconfig
    monkeypatch.setattr(appconfig, "_app_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(appconfig, "_config_cache", None)
    monkeypatch.setattr(appconfig, "_keys_cache", None)
    yield tmp_path


def test_reset_map_mode_removes_key_from_file(config_dir):
    from core import appconfig
    assert appconfig.save_config({"map_mode": "online", "kakao_js_key": "k"})
    assert appconfig.reset_map_mode()
    data = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    assert "map_mode" not in data and data["kakao_js_key"] == "k"   # 다른 키는 그대로
    assert appconfig.get_map_mode() is None
    # 병합 때문에 되살아나지 않는다: 지운 뒤 다른 값을 저장해도 map_mode는 없다.
    assert appconfig.save_config({"other": 1})
    assert "map_mode" not in json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))


def test_save_config_aborts_when_existing_file_unreadable(config_dir):
    from core import appconfig
    assert appconfig.save_config({"map_mode": "offline", "kakao_js_key": "keep-me"})
    path = config_dir / "settings.json"
    if os.geteuid() == 0:
        pytest.skip("root는 권한 검사를 거치지 않는다")
    original = path.read_bytes()
    os.chmod(path, 0)
    try:
        assert appconfig.save_config({"map_mode": "online"}) is False
        assert appconfig.reset_map_mode() is False
    finally:
        os.chmod(path, 0o644)
    assert path.read_bytes() == original        # 파일을 덮지 않았다(키 덮어쓰기 값 보존)
    assert appconfig.get_map_mode() == "offline"   # 캐시도 바꾸지 않았다


def test_cp949_key_file_is_accepted(tmp_path, monkeypatch):
    from core import appconfig
    monkeypatch.setattr(appconfig, "keys_dir", lambda: str(tmp_path))
    fake_js = "0" * 31 + "a"   # 형식만 맞춘 가짜 값(실제 키 아님)
    text = '{"kakao_js_key": "%s", "kakao_rest_key": "", "메모": "카카오 키 파일 - 한글 주석"}' % fake_js
    (tmp_path / "online_keys.json").write_bytes(text.encode("cp949"))
    infos = appconfig.scan_key_files()
    assert len(infos) == 1 and infos[0].usable and infos[0].js == fake_js
    # 완전히 깨진 바이트도 버리지 않고 키 값을 찾는다.
    (tmp_path / "online_keys.json").write_bytes(b'\xff\xfe\xfd{"kakao_js_key": "' + fake_js.encode() + b'"}')
    infos = appconfig.scan_key_files()
    assert len(infos) == 1 and infos[0].js == fake_js


# ---------------------------------------------------------------- #47 저장 실패 → 다음 실행에서 다시 묻기

def _mode_host(monkeypatch, pending: bool):
    from ui import main_window
    recorded = []
    monkeypatch.setattr(main_window.MainWindow, "_mode_save_pending", classmethod(lambda cls: pending))
    monkeypatch.setattr(main_window.MainWindow, "_record_mode_saved", classmethod(lambda cls, ok: recorded.append(ok)))
    host = SimpleNamespace(_show_map_resource_notice=lambda mode: None, _online_notified=set(),
                           _analysis_view=SimpleNamespace(reload_maps=lambda: None),
                           _mode_save_pending=main_window.MainWindow._mode_save_pending,
                           _record_mode_saved=main_window.MainWindow._record_mode_saved)
    return main_window, host, recorded


def test_first_run_asks_again_when_previous_save_failed(qapp, monkeypatch):
    main_window, host, recorded = _mode_host(monkeypatch, pending=True)
    asked = []
    monkeypatch.setattr(main_window, "get_map_mode", lambda: "online")
    monkeypatch.setattr(main_window, "ask_map_mode", lambda parent, current=None: asked.append(current) or "offline")
    monkeypatch.setattr(main_window, "set_map_mode", lambda mode: True)
    main_window.MainWindow._first_run_setup(host)
    assert asked == ["online"] and recorded == [True]


def test_first_run_does_not_ask_when_saved(qapp, monkeypatch):
    main_window, host, recorded = _mode_host(monkeypatch, pending=False)
    monkeypatch.setattr(main_window, "get_map_mode", lambda: "offline")
    monkeypatch.setattr(main_window, "ask_map_mode", lambda *a, **k: pytest.fail("묻지 않아야 한다"))
    main_window.MainWindow._first_run_setup(host)
    assert recorded == []


def test_apply_map_mode_records_failure_and_tells_truth(qapp, monkeypatch):
    main_window, host, recorded = _mode_host(monkeypatch, pending=False)
    warnings = []
    monkeypatch.setattr(main_window, "set_map_mode", lambda mode: False)
    monkeypatch.setattr(main_window.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    monkeypatch.setattr(main_window.geocode, "reset_block", lambda: None)
    monkeypatch.setattr(main_window.AddressResolver, "instance", classmethod(lambda cls: SimpleNamespace(reset=lambda: None)))
    main_window.MainWindow._apply_map_mode(host, "offline")
    assert recorded == [False]
    assert len(warnings) == 1 and "다시 묻습니다" in warnings[0]


# ---------------------------------------------------------------- #48 진행 창이 있는 작업 취소

def test_task_cancel_keeps_gui_responsive(qapp):
    from ui.main_window import MainWindow
    host = QWidget()
    host._home = QWidget()
    ticks = []

    def slow(cancel, _progress):
        # 취소 신호가 와도 바로 안 끝나는 작업(큰 파일 판별처럼). 0.3초 뒤에야 취소를 확인한다.
        while not cancel.wait(0.05):
            pass
        time.sleep(0.3)
        return "late"

    # GUI 스레드가 살아 있으면 취소 뒤에도 타이머가 돈다.
    heartbeat = QTimer()
    heartbeat.setInterval(30)
    heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
    heartbeat.start()

    cancelled_at = {}
    outer = {"result": None}

    def run():
        outer["result"] = MainWindow._run_task_with_progress(host, slow, "검사 중")

    def press_cancel():
        from PySide6.QtWidgets import QProgressDialog, QPushButton
        dlg = host.findChild(QProgressDialog)   # QProgressDialog는 host의 자식
        assert dlg is not None
        cancelled_at["t"] = time.monotonic()
        dlg.findChild(QPushButton).click()      # 사용자가 [취소]를 누른 것과 같다(cancel()은 canceled를 내지 않는다)

    QTimer.singleShot(80, press_cancel)
    run()
    heartbeat.stop()
    result = outer["result"]
    assert result["cancelled"] is True
    # 취소를 누른 뒤 작업이 끝날 때까지(0.3초) 이벤트 루프가 계속 돌았다.
    after = [t for t in ticks if t > cancelled_at["t"] + 0.05]
    assert len(after) >= 3
    assert host._home.isEnabled()
    host.deleteLater()


def test_task_result_and_failure(qapp):
    from ui.main_window import MainWindow
    host = QWidget()
    host._home = QWidget()
    ok = MainWindow._run_task_with_progress(host, lambda c, p: 42, "x")
    assert ok["value"] == 42 and not ok["cancelled"] and not ok["error"]

    def boom(c, p):
        raise ValueError("bad file")

    failed = MainWindow._run_task_with_progress(host, boom, "x")
    assert failed["value"] is None and "bad file" in failed["error"] and not failed["cancelled"]
    host.deleteLater()


def test_reference_search_and_validation_run_in_worker(qapp, monkeypatch):
    """후보 탐색·검증은 GUI 스레드가 아닌 워커에서 돈다."""
    from ui.main_window import MainWindow
    from core import damage_repair
    threads = []
    main = threading.get_ident()
    monkeypatch.setattr(damage_repair, "find_reference_candidates",
                        lambda path, report: threads.append(threading.get_ident()) or ["r1.avi"])
    monkeypatch.setattr(damage_repair, "validate_reference",
                        lambda report, ref: threads.append(threading.get_ident()) or (True, ""))
    host = QWidget()
    host._home = QWidget()
    host._run_task_with_progress = lambda fn, text: MainWindow._run_task_with_progress(host, fn, text)
    host._call_with_cancel = MainWindow._call_with_cancel
    assert MainWindow._find_reference_candidates_with_progress(host, "x.avi", None) == ["r1.avi"]
    assert MainWindow._validate_reference_with_progress(host, None, "r1.avi") == (True, "")
    assert threads and all(t != main for t in threads)
    host.deleteLater()


def test_call_with_cancel_passes_event_only_when_accepted():
    from ui.main_window import MainWindow
    seen = {}

    def with_event(a, cancel_event=None):
        seen["ev"] = cancel_event
        return a

    def without(a):
        return a * 2

    ev = threading.Event()
    assert MainWindow._call_with_cancel(with_event, 1, cancel_event=ev) == 1 and seen["ev"] is ev
    assert MainWindow._call_with_cancel(without, 2, cancel_event=ev) == 4


# ---------------------------------------------------------------- #51 참조 검증 실패 뒤 남은 후보 유지

def _damage_host(monkeypatch, candidates, validations):
    """_check_damage를 돌릴 호스트. validations: 참조 경로 → (ok, 사유)."""
    from ui import damage_dialog
    report = SimpleNamespace(damaged=True, needs_reference=True, container="avi", fragmented=False, path="x.avi")
    host = SimpleNamespace(
        _scan_damage_with_progress=lambda path, label="": report,
        _find_reference_candidates_with_progress=lambda path, rep: list(candidates),
        _validate_reference_with_progress=lambda rep, ref: validations.get(ref, (True, "")),
    )
    monkeypatch.setattr(damage_dialog, "ask_recovery_mode", lambda parent, rep, label="": "none")
    return host, report


def test_failed_candidate_removed_and_rest_offered(qapp, monkeypatch):
    from ui import damage_dialog
    from ui.main_window import MainWindow
    host, _ = _damage_host(monkeypatch, ["R.avi", "F1.avi", "F2.avi"], {"R.avi": (False, "채널이 다름")})
    offered = []

    def ask_reference(parent, rep, cands):
        offered.append(list(cands))
        return cands[0], False   # 첫 후보 사용

    monkeypatch.setattr(damage_dialog, "ask_reference", ask_reference)
    monkeypatch.setattr(damage_dialog, "confirm_reference_problem", lambda parent, reason: False)   # 취소
    result = MainWindow._check_damage(host, "x.avi")
    assert result is not None and result[2] == "F1.avi"
    assert offered == [["R.avi", "F1.avi", "F2.avi"], ["F1.avi", "F2.avi"]]


def test_picker_dismissed_returns_to_reference_prompt(qapp, monkeypatch):
    from ui import damage_dialog
    from ui.main_window import MainWindow
    host, _ = _damage_host(monkeypatch, ["F1.avi"], {})
    answers = iter([(None, False), ("F1.avi", False)])   # 파일 선택 창만 닫음 → 다시 묻는다
    asked = []
    monkeypatch.setattr(damage_dialog, "ask_reference", lambda parent, rep, cands: asked.append(1) or next(answers))
    result = MainWindow._check_damage(host, "x.avi")
    assert result is not None and result[2] == "F1.avi" and len(asked) == 2


def test_validation_cancel_aborts(qapp, monkeypatch):
    from ui import damage_dialog
    from ui.main_window import MainWindow
    host, _ = _damage_host(monkeypatch, ["F1.avi"], {})
    host._validate_reference_with_progress = lambda rep, ref: None   # 검증 중 취소
    monkeypatch.setattr(damage_dialog, "ask_reference", lambda parent, rep, cands: (cands[0], False))
    assert MainWindow._check_damage(host, "x.avi") is None


# ---------------------------------------------------------------- #55 지도 선·범례

NODE_DOM = r'''
const vm = require('vm'); const fs = require('fs');
function el() { const e = { style: {}, children: [], textContent: '', innerHTML: '', title: '',
  classList: { toggle() {}, add() {}, remove() {} },
  appendChild(c) { this.children.push(c); return c; }, addEventListener() {}, setProperty() {},
  getContext() { return { clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, fill() {}, stroke() {},
    getImageData() { return {}; } }; } };
  e.style.setProperty = function () {}; return e; }
const byId = {};
const document = { getElementById(id) { return byId[id] || (byId[id] = el()); }, createElement() { return el(); },
  createTextNode(t) { return { text: t }; }, body: { classList: { toggle() {} } } };
const sources = {};
class Map { constructor() {} on() {} once() {} addControl() {} loaded() { return false; } isStyleLoaded() { return true; }
  addSource(id, s) { sources[id] = s.data; } getSource(id) { return { setData(d) { sources[id] = d; } }; }
  hasImage() { return false; } addImage() {} addLayer() {} jumpTo() {} fitBounds() {} getCanvas() { return { clientWidth: 100, clientHeight: 100 }; }
  project() { return { x: 50, y: 50 }; } easeTo() {} }
const context = { window: {}, document, console, setInterval() { return 0; }, clearInterval() {},
  URL: class { constructor() { this.href = ''; } }, fetch: () => Promise.reject(new Error('offline')),
  maplibregl: { Map, NavigationControl: class {}, ScaleControl: class {}, Marker: class { setLngLat() { return this; } addTo() { return this; } remove() {} },
    addProtocol() {} }, pmtiles: {}, sources, byId };
context.window.sources = sources;
vm.runInNewContext(fs.readFileSync(0, 'utf8'), context);
'''


def _run_map_html(extra_js: str) -> dict:
    if not shutil.which("node"):
        pytest.skip("Node.js required")
    html = (ROOT / "ui/web/map.html").read_text(encoding="utf-8")
    script = html.split("<script>")[1].split("</script>")[0]
    harness = NODE_DOM + extra_js
    out = subprocess.run(["node", "-e", harness], input=script, text=True, capture_output=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _points_payload():
    # 실측 0~2초, 추정 3~5초, 실측 6~8초, 손상 빈 행 9~10초, 실측 11~12초
    pts = []
    for t in range(13):
        r = "interpolated" if 3 <= t <= 5 else ("damaged" if 9 <= t <= 10 else "")
        has = r != "damaged"
        pts.append({"t": t, "lat": 37.5 + t * 1e-4 if has else None, "lon": 127.0 if has else None,
                    "v": 30, "d": 0, "o": 0, "u": 0, "h": None, "e": None, "s": 0, "r": r})
    return pts


def test_map_html_groups_synthetic_segments_and_styles_legend():
    result = _run_map_html(r'''
const pts = ''' + json.dumps(_points_payload()) + r''';
const f = context.buildFeatures(pts);
context.renderLegend([["임의 복구 구간(추정 좌표)", "#9c36b5", "dotted"], ["손상 구간(복구 안 함)", "#212529", "dashed"], ["급가속", "#e03131"]]);
const rows = byId["legend-events"].children.map((r) => [r.children[0].style.borderColor, r.children[0].style.borderTopStyle]);
console.log(JSON.stringify({
  interp: f.interp.features.map((x) => x.geometry.coordinates.length),
  damaged: f.damaged.features.map((x) => x.geometry.coordinates.length),
  solid: f.solid.features.map((x) => x.geometry.coordinates.length),
  legend: rows }));
''')
    # 2→3→4→5→6초: 추정 선분 4개가 하나의 LineString(좌표 5개)으로 묶인다.
    assert result["interp"] == [5]
    # 8→11초: 손상 빈 행을 건너 잇는 선 하나.
    assert result["damaged"] == [2]
    assert result["solid"] == [3, 3, 2]
    assert result["legend"] == [["#9c36b5", "dotted"], ["#212529", "dashed"], ["#e03131", "solid"]]


def test_map_html_status_counts_estimated_separately_and_marks_marker():
    result = _run_map_html(r'''
const pts = ''' + json.dumps(_points_payload()) + r''';
context.window.__mapReady = true;
context.renderTrack({ points: pts, legend: [], events: [], segEnds: [] });
const status = byId["status"].textContent;
context.setPlaybackTime(4.0);
const est = sources["trk-pos"].features[0].properties.est;
context.setPlaybackTime(7.0);
const real = sources["trk-pos"].features[0].properties.est;
console.log(JSON.stringify({ status, est, real, flag: context.window.__posEstimated }));
''')
    assert result["status"].startswith("좌표 8개 · 추정 좌표 3개(임의 복구, 측정값 아님) · 손상 구간 2개 지점")
    assert result["est"] == 1 and result["real"] == 0 and result["flag"] is False


def test_damage_legend_has_line_style_and_distinct_color():
    from core.driving_events import EVENT_COLORS
    from ui import map_view
    pts = [TrackPoint(start_time_sec=0, recovery_state="interpolated", latitude=37, longitude=127),
           TrackPoint(start_time_sec=1, recovery_state="damaged")]
    legend = map_view._damage_legend(pts)
    assert [row[2] for row in legend] == ["dotted", "dashed"]
    assert map_view.DAMAGED_COLOR not in EVENT_COLORS.values()
    assert map_view.INTERPOLATED_COLOR not in EVENT_COLORS.values()
    # 지도 페이지의 선 색과 같다.
    for name in ("map.html", "map_kakao.html"):
        html = (ROOT / "ui/web" / name).read_text(encoding="utf-8")
        assert map_view.DAMAGED_COLOR in html and map_view.INTERPOLATED_COLOR in html
        assert "[label, color, style]" in html


def test_kakao_page_status_counts_estimated_separately():
    if not shutil.which("node"):
        pytest.skip("Node.js required")
    html = (ROOT / "ui/web/map_kakao.html").read_text(encoding="utf-8")
    script = html.split("<script>")[1].split("</script>")[0]
    harness = NODE_DOM + r'''
const pts = ''' + json.dumps(_points_payload()) + r''';
const fixes = pts.filter((p) => p.lat !== null);
console.log(JSON.stringify({ status: context.trackStatus(pts, fixes) }));
'''
    out = subprocess.run(["node", "-e", harness], input=script, text=True, capture_output=True, check=True)
    status = json.loads(out.stdout.strip().splitlines()[-1])["status"]
    assert status.startswith("좌표 8개 · 추정 좌표 3개")


# ---------------------------------------------------------------- #56 온라인 지도 실패 안내·캡처

def test_online_failure_text_does_not_promise_track(qapp, monkeypatch):
    from ui import main_window
    shown = {}

    class Box:
        Warning = QMessageBox.Warning
        ActionRole = QMessageBox.ActionRole
        Ok = QMessageBox.Ok

        def __init__(self, icon, title, text, parent=None):
            shown["text"] = text

        def addButton(self, *a):
            return object()

        def setDefaultButton(self, b):
            pass

        def exec(self):
            pass

        def clickedButton(self):
            return None

    monkeypatch.setattr(main_window, "QMessageBox", Box)
    monkeypatch.setattr(main_window.MapServer, "instance", classmethod(lambda cls: SimpleNamespace(base_url="http://127.0.0.1:1")))
    for kind in ("quota", "network"):
        host = SimpleNamespace(_online_notified=set())
        main_window.MainWindow._on_online_map_failed(host, kind, "")
        assert "배경지도 없이 표시" not in shown["text"]
        assert "궤적도 표시되지 않" in shown["text"]


def test_grab_png_requires_map_ready():
    from ui.map_view import MapView
    calls = []
    host = SimpleNamespace(_loaded=True, is_map_ready=lambda: False,
                           _view=SimpleNamespace(grab=lambda: calls.append("grab")))
    assert MapView.grab_png(host) is None and calls == []
    host = SimpleNamespace(_loaded=False, is_map_ready=lambda: pytest.fail("로드 전엔 묻지 않는다"),
                           _view=SimpleNamespace(grab=lambda: calls.append("grab")))
    assert MapView.grab_png(host) is None


def test_is_map_ready_uses_track_drawn_signal():
    from ui.map_view import MapView
    host = SimpleNamespace(_eval_js=lambda script: "yes" if "__trackDrawn" in script and "__mapReady" in script else "no")
    assert MapView.is_map_ready(host) is True
    host = SimpleNamespace(_eval_js=lambda script: None)   # 응답 없음(페이지 멈춤)
    assert MapView.is_map_ready(host) is False


# ---------------------------------------------------------------- #70 #71 #72 #78 #79 #111 Tracker 재생

def test_enlarged_pane_survives_segment_switch(tracker):
    from ui.tracker_tab import REAR
    tracker._set_rear_active(True)
    tracker._enlarged = REAR
    tracker._set_rear_active(False, keep_enlarged=True)   # 다음 영상을 열며 잠깐 끈다
    assert tracker.enlarged_pane() == REAR
    tracker._set_rear_active(True, keep_enlarged=True)
    assert tracker.enlarged_pane() == REAR
    tracker._set_rear_active(False)                        # 새 사건: 초기화
    assert tracker.enlarged_pane() is None


def test_seek_within_last_segment_clears_ended(tracker):
    player = _two_segment_tracker(tracker, seg=1, local_ms=59000)
    tracker._ended = True
    tracker._on_slider_moved(60000 + 2500)
    assert tracker._ended is False and ("seek", 2500) in player.calls
    tracker._toggle_play()
    assert tracker._seg == 1 and ("play",) in player.calls   # 1번 영상으로 돌아가지 않는다


def test_frame_step_uses_local_position_and_rounds_up(tracker, monkeypatch):
    player = _two_segment_tracker(tracker, seg=1, local_ms=1010)
    monkeypatch.setattr(tracker, "_frame_ms", lambda: 40.0)   # 25fps
    tracker.step_frame(-1)
    assert player.calls[-1] == ("seek", 960)      # 이전 프레임(24번)의 경계
    player._pos = 960
    tracker.step_frame(1)
    assert player.calls[-1] == ("seek", 1000)     # 다음 프레임(25번)
    # 30fps(33.33ms): 1000ms(30번 프레임)에서 앞으로 → 1034(31번 pts 1033.3 이상), 뒤로 → 967(29번 pts 966.7 이상)
    monkeypatch.setattr(tracker, "_frame_ms", lambda: 1000.0 / 30.0)
    player._pos = 1000
    tracker.step_frame(1)
    assert player.calls[-1] == ("seek", 1034)
    player._pos = 1000
    tracker.step_frame(-1)
    assert player.calls[-1] == ("seek", 967)


def test_pause_rear_skips_end_of_media(tracker):
    rear = FakePlayer()
    tracker._rear_player = rear
    rear._status = QMediaPlayer.EndOfMedia
    tracker._pause_rear()
    assert ("pause",) not in rear.calls
    rear._status = QMediaPlayer.LoadedMedia
    tracker._pause_rear()
    assert ("pause",) in rear.calls
    # 재생/정지, 프레임 이동, 오류 처리가 모두 헬퍼를 거친다.
    src = (ROOT / "ui/tracker_tab.py").read_text(encoding="utf-8")
    assert src.count("self._rear_player.pause()") == 1   # _pause_rear 안에서만


def test_seek_while_loading_keeps_prime_and_position(tracker):
    player = _two_segment_tracker(tracker, seg=1, local_ms=0)
    tracker._prime_pending = True
    tracker._prime_target_ms = 5000
    tracker._on_slider_moved(60000 + 7000)   # 같은 구간 안에서 막대를 옮김
    assert tracker._prime_pending is True and tracker._prime_target_ms == 7000
    assert tracker.global_position_ms() == 67000
    assert not any(c[0] == "seek" for c in player.calls)
    assert "탐색할 수 없습니다" not in tracker._media_label.text()
    # 로딩 중 → : 7초 기준으로 8초(같은 영상)
    tracker._skip(1000)
    assert tracker._seg == 1 and tracker._prime_target_ms == 8000


def test_unplayable_segment_keeps_error_and_skips_from_slider(tracker):
    _two_segment_tracker(tracker, seg=0, local_ms=0, error=QMediaPlayer.ResourceError, seekable=False)
    tracker._on_media_error("코덱 없음")
    text = tracker._media_label.text()
    tracker._on_slider_moved(2000)
    assert tracker._media_label.text() == text and tracker._seek_slider.value() == 2000
    tracker._skip(3000)
    assert tracker._seek_slider.value() == 5000 and tracker._media_label.text() == text


# ---------------------------------------------------------------- #73 그래프 캡처 뒤 시각 이름

def test_chart_time_label_restored_after_report_capture(qapp):
    from ui.dataset_view import DatasetView
    from ui.speed_tab import SpeedTab
    tab = SpeedTab()
    tab.load_views([DatasetView("Composed", [point(0), point(1, 40)], []),
                    DatasetView("슬랙", [point(0), point(1, 40)], [], is_slack=True)])
    tab._view_bar.setCurrentIndex(1)
    assert tab._chart._time_label == "경과"
    tab.grab_chart_png()
    assert tab._chart._time_label == "경과"
    tab.deleteLater()


# ---------------------------------------------------------------- #101 후방 손상 검사

def test_rear_damage_is_checked_and_recorded(qapp, monkeypatch):
    from ui.main_window import MainWindow
    report = SimpleNamespace(damaged=True, summary_lines=lambda: ["mdat 손상 1곳"], to_dict=lambda: {"x": 1})
    host = QWidget()
    host._scan_damage_with_progress = lambda path, label="": report
    clicked = {}

    def exec_click_go(box):
        go = [b for b in box.buttons() if b.text().startswith("계속")]
        assert len(go) == 1
        clicked["label"] = box.text()
        QTimer.singleShot(0, go[0].click)
        return original(box)

    original = QMessageBox.exec
    monkeypatch.setattr(QMessageBox, "exec", exec_click_go)
    rec = MainWindow._check_rear_damage(host, "/tmp/rear.avi")
    assert rec["damaged"] and rec["mode"] == "none" and rec["summary"] == ["mdat 손상 1곳"]
    assert "후방" in clicked["label"]
    host._scan_damage_with_progress = lambda path, label="": SimpleNamespace(damaged=False)
    assert MainWindow._check_rear_damage(host, "/tmp/rear.avi") == {}
    host.deleteLater()


def test_video_selected_passes_rear_damage_to_settings(qapp, monkeypatch):
    from ui import main_window
    started = {}
    host = SimpleNamespace(
        _compute_hash_with_progress=lambda path, label="": ("ab" * 32, ""),
        _history_db_path=":memory:",
        _check_damage=lambda path, label="": (SimpleNamespace(damaged=False), "none", ""),
        _check_rear_damage=lambda path: {"damaged": True, "mode": "none", "summary": ["x"]},
        _ask_slack=lambda targets: False,
        _start_analysis=lambda **kw: started.update(kw),
        _home=SimpleNamespace(take_pair_check=lambda: {}),
    )

    class Store:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def find_cases_by_sha256(self, sha):
            return []

    monkeypatch.setattr(main_window, "HistoryStore", Store)
    main_window.MainWindow._on_video_selected(host, "/tmp/front.avi", "/tmp/rear.avi", "")
    assert started["extra_settings"]["rear_damage"]["damaged"] is True
    # 전방만이면 후방 검사를 부르지 않는다.
    host._check_rear_damage = lambda path: pytest.fail("후방 없음")
    main_window.MainWindow._on_video_selected(host, "/tmp/front.avi", "", "")
    assert started["extra_settings"] == {}


def test_analysis_status_shows_rear_damage(qapp):
    from ui.analysis_view import AnalysisView
    text = ["분석 상태: ok"]
    host = SimpleNamespace(_analysis_status=SimpleNamespace(text=lambda: text[0], setText=lambda t: text.__setitem__(0, t)))
    src = (ROOT / "ui/analysis_view.py").read_text(encoding="utf-8")
    assert 'settings.get("rear_damage")' in src and "후방 영상 손상" in src
    del AnalysisView, host


# ---------------------------------------------------------------- #109 #130 지도 재시도·차단 이동

def test_reload_attempts_reset_by_crash_spacing(monkeypatch):
    from ui import map_view
    clock = {"t": 1000.0}
    monkeypatch.setattr(map_view.time, "monotonic", lambda: clock["t"])
    reloads = []
    monkeypatch.setattr(map_view.QTimer, "singleShot", staticmethod(lambda ms, fn: reloads.append(ms)))
    host = SimpleNamespace(_loaded=True, _reload_attempts=0, _last_crash_at=-1e9, _last_track_js="js", _pending_js=[],
                           MAX_RELOAD_ATTEMPTS=3, _load_page=lambda: None)
    for _ in range(3):
        map_view.MapView._on_render_process_gone(host, None, 1)
        clock["t"] += 1.0
    assert len(reloads) == 3
    map_view.MapView._on_render_process_gone(host, None, 1)   # 4번째 연속 크래시: 포기
    assert len(reloads) == 3 and host._last_track_js is None
    clock["t"] += 120.0                                        # 2분 조용했다 → 새 사고
    host._last_track_js = "js"
    map_view.MapView._on_render_process_gone(host, None, 1)
    assert len(reloads) == 4 and host._reload_attempts == 1 and host._last_track_js == "js"


def test_blocked_navigation_keeps_loaded():
    from ui.map_view import MapView
    url = "http://127.0.0.1:48213/map.html"

    class View:
        def url(self):
            return SimpleNamespace(toString=lambda: url + "?x=1")

    host = SimpleNamespace(_loaded=True, _view=View(), _page=SimpleNamespace(allowed_url=url))
    MapView._on_load_finished(host, False)
    assert host._loaded is True
    host._page.allowed_url = "http://127.0.0.1:48213/map_kakao.html"   # 우리 페이지가 아니면 내린다
    MapView._on_load_finished(host, False)
    assert host._loaded is False


# ---------------------------------------------------------------- #110 HEAD Host 검사

def test_head_request_checks_host():
    from ui.map_server import _Handler
    log = []
    host = SimpleNamespace(headers={"Host": "evil.example"}, server=SimpleNamespace(server_address=("127.0.0.1", 48213)),
                           send_error=lambda code: log.append(("error", code)),
                           _serve=lambda head_only=False: log.append(("serve", head_only)))
    host._is_own_request = lambda: _Handler._is_own_request(host)
    _Handler.do_HEAD(host)
    assert log == [("error", 403)]
    host.headers = {"Host": "127.0.0.1:48213"}
    _Handler.do_HEAD(host)
    assert log[-1] == ("serve", True)


# ---------------------------------------------------------------- #114 case.json 갱신 실패

def test_case_edit_reverts_db_when_case_json_fails(qapp, monkeypatch, tmp_path):
    from ui import main_window
    case_dir = tmp_path / "case1"
    (case_dir / "output").mkdir(parents=True)
    (case_dir / "case.json").write_text("{}", encoding="utf-8")
    case = SimpleNamespace(id=7, case_number="OLD", examiner="e", memo="m", output_folder=str(case_dir / "output"))
    updates = []

    class Store:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get_case(self, cid):
            return case

        def update_case_info(self, cid, number, examiner, memo):
            updates.append((number, examiner, memo))

    class Dialog:
        Accepted = 1
        result_values = ("NEW", "e2", "m2")

        def __init__(self, *a):
            pass

        def exec(self):
            return 1

    warnings = []
    monkeypatch.setattr(main_window, "HistoryStore", Store)
    monkeypatch.setattr(main_window, "CaseEditDialog", Dialog)
    monkeypatch.setattr(main_window, "update_case_json", lambda *a: False)
    monkeypatch.setattr(main_window.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    host = SimpleNamespace(_history_db_path="x", _refresh_history=lambda: None, _current_case_id=None)
    main_window.MainWindow._on_history_edit_requested(host, 7)
    assert updates == [("NEW", "e2", "m2"), ("OLD", "e", "m")]
    assert len(warnings) == 1 and "case.json" in warnings[0]
    # case.json이 없는 옛 사건은 DB만 고친다(되돌리지 않는다).
    (case_dir / "case.json").unlink()
    updates.clear()
    host._analysis_view = SimpleNamespace(set_case_number=lambda n: None)
    main_window.MainWindow._on_history_edit_requested(host, 7)
    assert updates == [("NEW", "e2", "m2")] and len(warnings) == 1


# ---------------------------------------------------------------- #116 #21 Tracker 정보 줄

def test_tracker_dropout_before_untrusted(tracker):
    rows = [point(0), TrackPoint(start_time_sec=1.0, gps_status="V", gps_trusted=False, gps_utc_time="00:00:01"),
            TrackPoint(start_time_sec=2.0, latitude=37, longitude=127, gps_checksum_ok=False, gps_utc_time="00:00:02")]
    tracker.load_track(rows, [])
    tracker._update_info(1.0)
    assert tracker._status_label.text() == "(GPS 끊김)"
    tracker._update_info(2.0)
    assert tracker._status_label.text() == "(검증 실패)"


def _damaged_track(mode):
    from core import damage_marks as dm
    pts = []
    for t in list(range(0, 10)) + list(range(20, 30)):
        pts.append(TrackPoint(start_time_sec=float(t), end_time_sec=t + 1.0, latitude=37.5 + t * 1e-4, longitude=127.0,
                              speed_kmh=36.0, gps_date="2025-09-01", gps_utc_time=f"12:00:{t:02}",
                              gps_checksum_ok=True, gps_trusted=True))
    return dm.apply_damage_marks(pts, [dm.DamageInterval(9.5, 20.0, "gap")], mode)


def test_tracker_marks_estimated_rows_and_skips_address(tracker, monkeypatch):
    from core import damage_marks as dm
    from ui import tracker_tab
    requests = []
    monkeypatch.setattr(tracker_tab.geocode, "is_available", lambda: True)
    monkeypatch.setattr(tracker_tab.geocode, "cached_address", lambda lat, lon: None)
    tracker._resolver = SimpleNamespace(request=lambda lat, lon, owner=None: requests.append((lat, lon)))
    pts = _damaged_track(dm.MODE_INTERPOLATE)
    assert any(p.recovery_state == "interpolated" for p in pts)
    tracker.load_track(pts, [])
    tracker._update_info(5.0)
    real_requests = len(requests)
    assert tracker._status_label.text() == "" and real_requests >= 1
    tracker._update_info(15.0)
    assert tracker._status_label.text() == "(추정 좌표)"
    assert "측정값 아님" in tracker._coord_label.toolTip() and "추정" in tracker._status_label.toolTip()
    assert tracker._speed_label.text() == "속도 -"
    assert tracker._addr_label.text() == "" and len(requests) == real_requests   # 추정 좌표로 주소 조회 없음
    assert tracker._clock_label.text() == "날짜·시간 - (추정 구간)"
    tracker._update_info(25.0)
    assert tracker._status_label.text() == "" and len(requests) == real_requests + 1


def test_tracker_marks_damaged_rows(tracker):
    from core import damage_marks as dm
    pts = _damaged_track(dm.MODE_NONE)
    tracker.load_track(pts, [])
    tracker._update_info(15.0)
    assert tracker._status_label.text() == "(손상 구간)"
    assert tracker._coord_label.text().startswith("위치 37.500900")   # 마지막 정상값(9초)을 그대로
    assert tracker._clock_label.text() == "날짜·시간 - (손상 구간)"
    assert tracker.gps_interval() == 1.0


def test_is_synthetic_helper_matches_contract():
    from ui.map_view import is_synthetic_point
    assert is_synthetic_point(TrackPoint(recovery_state="damaged"))
    assert is_synthetic_point(TrackPoint(recovery_state="interpolated"))
    assert not is_synthetic_point(TrackPoint(recovery_state="gap_carved"))
    assert not is_synthetic_point(TrackPoint())
    assert is_synthetic_point(SimpleNamespace(is_synthetic=True, recovery_state=""))


# ---------------------------------------------------------------- #119 #21 Location 표

@pytest.fixture
def location(qapp, monkeypatch):
    from ui import location_tab
    monkeypatch.setattr(location_tab, "MapView", MapStub)
    monkeypatch.setattr(location_tab.geocode, "is_available", lambda: False)
    tab = location_tab.LocationTab()
    yield tab
    tab.deleteLater()
    qapp.processEvents()


def test_segment_view_rows_use_global_numbers(location):
    from ui.dataset_view import DatasetView
    v1 = [point(t, segment_index=0) for t in range(120)]
    v2 = [point(t, segment_index=1) for t in range(10)]
    composed = DatasetView("Composed", v1 + v2, [], segment_labels=["video1", "video2"])
    location.load_views([composed, DatasetView("video1", v1, [], row_offset=0), DatasetView("video2", v2, [], row_offset=120)])
    location._view_bar.setCurrentIndex(2)
    assert location._table.verticalHeaderItem(4).text() == "125"
    location._show_selected(4, v2[4])
    assert location._selected_label.text().startswith("#125:")


def test_dataset_views_assign_row_offsets():
    from ui.analysis_view import dataset_views
    segs = [SimpleNamespace(label="video1", points=[point(0)] * 3, driving_events=[], offset_sec=0.0),
            SimpleNamespace(label="video2", points=[point(0)] * 5, driving_events=[], offset_sec=3.0),
            SimpleNamespace(label="video3", points=[point(0)], driving_events=[], offset_sec=8.0)]
    result = SimpleNamespace(is_sequence=True, segments=segs, points=[point(0)] * 9, driving_events=[], damage=None)
    views = dataset_views(result, [])
    assert [v.row_offset for v in views] == [0, 0, 3, 8]


def test_location_estimated_row_has_no_address_or_link(location, monkeypatch):
    from ui import location_tab
    from core.location_table import COL_LINK
    requests = []
    monkeypatch.setattr(location_tab.geocode, "is_available", lambda: True)
    monkeypatch.setattr(location_tab.geocode, "cached_address", lambda lat, lon: None)
    location._resolver = SimpleNamespace(request=lambda lat, lon, owner=None: requests.append((lat, lon)))
    opened = []
    monkeypatch.setattr(location_tab.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url)))
    est = TrackPoint(start_time_sec=1.0, latitude=37.1, longitude=127.1, recovery_state="interpolated")
    dmg = TrackPoint(start_time_sec=2.0, recovery_state="damaged")
    location.load([point(0), est, dmg], [])
    location._show_selected(1, est)
    assert "추정" in location._selected_label.text() and requests == []
    location._show_selected(2, dmg)
    assert "손상 구간" in location._selected_label.text()
    location._on_cell_double_clicked(1, COL_LINK)
    assert opened == []
    location._on_cell_double_clicked(0, COL_LINK)
    assert len(opened) == 1
    assert location._table.item(1, COL_LINK).foreground().color() != location._table.item(0, COL_LINK).foreground().color()


# ---------------------------------------------------------------- #132 주소 조회 스레드

def test_address_resolver_is_daemon_and_stop_does_not_block(qapp, monkeypatch):
    from PySide6.QtCore import QThread
    from ui import address_resolver
    resolver = address_resolver.AddressResolver()
    assert not isinstance(resolver, QThread)
    started = threading.Event()

    def slow(lat, lon):
        started.set()
        time.sleep(1.5)   # 느린 DNS처럼
        return SimpleNamespace(status=address_resolver.geocode.STATUS_OK, address="x", message="")

    monkeypatch.setattr(address_resolver.geocode, "describe_location", slow)
    resolver.request(37.0, 127.0, owner="t")
    assert started.wait(2.0)
    assert resolver._thread.daemon
    t0 = time.monotonic()
    resolver.stop()
    assert time.monotonic() - t0 < 0.5          # 기다리거나 terminate()하지 않는다
    resolver.request(37.0, 127.0, owner="t")    # 종료 뒤 요청은 무시
    assert resolver._pending == {}


# ---------------------------------------------------------------- F 요청: #57 #58 #49 #131

def test_db_recovery_notice_refreshes_history_and_shows_migration(qapp, monkeypatch):
    from ui import main_window
    from core import appinfo
    warnings = []
    monkeypatch.setattr(main_window.QMessageBox, "warning", lambda *a: warnings.append((a[1], a[2])))

    class Store:
        recovered_from = ""

        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def list_cases(self):
            Store.recovered_from = "/x/history.db.corrupt"   # 조회 중 손상 → 복구
            return []

    monkeypatch.setattr(main_window, "HistoryStore", Store)
    monkeypatch.setattr(appinfo, "MIGRATION_NOTICE", "예전 폴더를 옮기지 못했습니다")
    host = SimpleNamespace(_history_db_path="x", _home=SimpleNamespace(set_history=lambda cases: None))
    host._refresh_history = lambda: main_window.MainWindow._refresh_history(host)
    host._notify_recovered_db = lambda: main_window.MainWindow._notify_recovered_db(host)
    host._notify_migration = lambda: main_window.MainWindow._notify_migration(host)
    Store.recovered_from = "/x/history.db.bad"
    main_window.MainWindow._notify_db_recovery(host)   # 예전엔 없는 _refresh_home을 불러 AttributeError
    titles = [t for t, _ in warnings]
    assert titles.count("사건 이력 복구") == 2          # 시작 때 1번 + 조회 중 복구 1번
    assert "데이터 폴더 이전" in titles and appinfo.MIGRATION_NOTICE == ""
    assert Store.recovered_from == ""
    main_window.MainWindow._notify_db_recovery(host)   # 안내는 한 번만(비워진 뒤 다시 뜨지 않는다)
    assert [t for t, _ in warnings].count("데이터 폴더 이전") == 1


def test_check_pair_probes_passes_cancel_event(monkeypatch):
    from core import video_sequence
    seen = {}

    def fake_compare(front, rear, cancel_event=None):
        seen["ev"] = cancel_event
        check = video_sequence.PairCheck(front_path=front, rear_path=rear)
        check.cancelled = cancel_event is not None and cancel_event.is_set()
        return check

    monkeypatch.setattr(video_sequence, "compare_pair", fake_compare)
    monkeypatch.setattr(video_sequence, "compare_gps", lambda *a: pytest.fail("취소됐으면 GPS 대조를 하지 않는다"))
    ev = threading.Event()
    ev.set()
    front = SimpleNamespace(path="f.mp4", points=[])
    rear = SimpleNamespace(path="r.mp4", points=[])
    check = video_sequence.check_pair_probes(front, rear, cancel_event=ev)
    assert seen["ev"] is ev and check.cancelled


def test_sequence_pair_fill_runs_check_in_worker(qapp, monkeypatch):
    from ui import home_view
    main = threading.get_ident()
    threads = []

    def fake_check(front, rear, cancel_event=None):
        threads.append(threading.get_ident())
        return home_view.PairCheck(front_path=front.path, rear_path=rear.path)   # ok

    monkeypatch.setattr(home_view, "check_pair_probes", fake_check)
    monkeypatch.setattr(home_view, "probe_single", lambda p, cancel=None: SimpleNamespace(path=p, points=[], name=os.path.basename(p)))
    monkeypatch.setattr(home_view.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("/tmp/rear_fill.mp4", "")))
    home = home_view.HomeView()
    src = (ROOT / "ui/home_view.py").read_text(encoding="utf-8")
    assert "check_pair_probes(f, r, cancel_event=cancel)" in src
    # 워커 경로를 직접 확인: _run_task로 돌린 대조가 GUI 스레드 밖에서 돈다.
    check, error = home._run_task("x", "y", lambda cancel, _p: home_view.check_pair_probes(
        SimpleNamespace(path="a"), SimpleNamespace(path="b"), cancel_event=cancel))
    assert check.ok and not error and threads and threads[0] != main
    home.close()


def test_damaged_pair_requires_confirmation_and_is_recorded(qapp, monkeypatch):
    from ui import home_view
    home = home_view.HomeView()
    check = home_view.PairCheck(front_path="/f.mp4", rear_path="/r.mp4")
    check.code = "damaged"
    check.damaged_files = {"후방": ["mdat 손상 1곳"]}
    check.problems = ["후방 영상이 손상돼 GPS를 읽지 못했습니다"]
    monkeypatch.setattr(home_view.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("/r.mp4", "")))
    monkeypatch.setattr(home, "_run_pair_check", lambda f, r: check)
    monkeypatch.setattr(home, "_notice", lambda *a, **k: None)
    asked = []

    def click(label):
        def exec_(box):
            btn = [b for b in box.buttons() if b.text() == label]
            assert len(btn) == 1, [b.text() for b in box.buttons()]
            asked.append(box.windowTitle() or box.text())
            QTimer.singleShot(0, btn[0].click)
            return original(box)
        return exec_

    original = QMessageBox.exec
    # 예: 대조 없이 받고 기록이 남는다.
    monkeypatch.setattr(QMessageBox, "exec", click("대조 없이 후방으로 받기"))
    assert home._pick_rear("/f.mp4") == "/r.mp4"
    record = home.take_pair_check()
    assert record["status"] == "damaged" and record["damaged_files"] == {"후방": ["mdat 손상 1곳"]}
    assert home.take_pair_check() == {}   # 한 번 가져가면 비운다
    # 받지 않음 → 다시 고르기 창 → 전방만 분석: 확인 없이 통과시키지 않는다.
    answers = iter(["받지 않음", "전방만 분석"])
    monkeypatch.setattr(QMessageBox, "exec", lambda box: click(next(answers))(box))
    assert home._pick_rear("/f.mp4") == "" and home.take_pair_check() == {}
    home.close()


def test_video_selected_records_pair_check(qapp, monkeypatch):
    from ui import main_window
    started = {}
    host = SimpleNamespace(
        _compute_hash_with_progress=lambda path, label="": ("ab" * 32, ""),
        _history_db_path=":memory:",
        _check_damage=lambda path, label="": (SimpleNamespace(damaged=False), "none", ""),
        _check_rear_damage=lambda path: {},
        _ask_slack=lambda targets: False,
        _start_analysis=lambda **kw: started.update(kw),
        _home=SimpleNamespace(take_pair_check=lambda: {"status": "damaged"}),
    )

    class Store:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def find_cases_by_sha256(self, sha):
            return []

    monkeypatch.setattr(main_window, "HistoryStore", Store)
    main_window.MainWindow._on_video_selected(host, "/tmp/front.avi", "/tmp/rear.avi", "")
    assert started["extra_settings"]["pair_check"] == {"status": "damaged"}


# ---------------------------------------------------------------- D1 요청: #45 #85 #74 #77 #42 #82

def test_history_open_runs_in_worker_and_cancels(qapp, monkeypatch):
    from ui import main_window
    main = threading.get_ident()
    seen = {}

    def slow_reopen(case, cancel_event=None):
        seen["thread"] = threading.get_ident()
        return SimpleNamespace(case_id=case.id)

    monkeypatch.setattr(main_window.ReopenWorker.__module__ and __import__("ui.workers", fromlist=["x"]), "reopen_case", slow_reopen)
    case = SimpleNamespace(id=3, case_number="C-3", examiner="e", memo="m", analysis_settings={})

    class Store:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get_case(self, cid):
            return case

        def touch_last_opened(self, cid):
            pass

    monkeypatch.setattr(main_window, "HistoryStore", Store)
    host = QWidget()
    loaded = {}
    host._history_db_path = "x"
    host._reopen_worker = None
    host._reopen_progress = None
    host._analysis_view = SimpleNamespace(load_result=lambda r, n, s: loaded.update(n=n))
    host._stack = SimpleNamespace(setCurrentWidget=lambda w: loaded.update(shown=True))
    main_window.MainWindow._on_history_item_opened(host, 3)
    assert host._reopen_worker is not None
    deadline = time.monotonic() + 5
    while host._reopen_worker is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert loaded == {"n": "C-3", "shown": True} and seen["thread"] != main
    assert host._current_case_id == 3
    host.deleteLater()


def test_slack_reanalysis_offered_once_after_repair(qapp, monkeypatch):
    from ui.main_window import MainWindow
    started = []
    host = QWidget()
    host._last_analysis_args = {"video_path": "/v.avi", "sha256": "ab", "carve_slack": False,
                                "extra_settings": {}, "damage_report": "rep"}
    host._start_analysis = lambda **kw: started.append(kw)
    answers = iter(["예, 다시 분석", "아니요"])

    def exec_(box):
        label = next(answers)
        btn = [b for b in box.buttons() if b.text() == label][0]
        QTimer.singleShot(0, btn.click)
        return original(box)

    original = QMessageBox.exec
    monkeypatch.setattr(QMessageBox, "exec", exec_)
    result = SimpleNamespace(damage={"slack_available": True, "slack_carved": False})
    MainWindow._offer_slack_reanalysis(host, result)
    assert len(started) == 1 and started[0]["carve_slack"] is True and started[0]["damage_report"] == "rep"
    MainWindow._offer_slack_reanalysis(host, result)   # '아니요'
    assert len(started) == 1
    # 이미 슬랙을 뽑았거나 슬랙이 없으면 묻지 않는다.
    monkeypatch.setattr(QMessageBox, "exec", lambda box: pytest.fail("묻지 않아야 한다"))
    MainWindow._offer_slack_reanalysis(host, SimpleNamespace(damage={"slack_available": True, "slack_carved": True}))
    MainWindow._offer_slack_reanalysis(host, SimpleNamespace(damage=None))
    host.deleteLater()


def test_tracker_clock_marks_estimated_time(tracker):
    from core.damage_marks import TIME_SOURCE_ELAPSED_ESTIMATED, TIME_SOURCE_UTC_ESTIMATED
    utc = point(0)
    utc.time_estimated = True
    utc.time_source = TIME_SOURCE_UTC_ESTIMATED
    elapsed = point(1)
    elapsed.time_estimated = True
    elapsed.time_source = TIME_SOURCE_ELAPSED_ESTIMATED
    tracker.load_track([utc, elapsed, point(2)], [])
    tracker._update_info(0.0)
    assert tracker._clock_label.text().endswith("(UTC 추정)") and "추정" in tracker._clock_label.toolTip()
    tracker._update_info(1.0)
    assert tracker._clock_label.text().endswith(" (추정)") and not tracker._clock_label.text().endswith("(UTC 추정)")
    tracker._update_info(2.0)
    assert "추정" not in tracker._clock_label.text()


def test_speed_tab_without_timeline_is_not_judgeable(qapp):
    from ui.dataset_view import DatasetView
    from ui.speed_tab import SpeedTab
    tab = SpeedTab()
    rows = [TrackPoint(latitude=37, longitude=127, speed_kmh=30 + i) for i in range(3)]   # 시각 없음
    tab.load_views([DatasetView("Composed", rows, [])])
    assert "판정 불가(시각 없음)" in tab._flag_label.text()
    tab.load_views([DatasetView("Composed", [point(0, 30), point(1, 40)], [])])
    assert "판정 불가" not in tab._flag_label.text()
    tab.deleteLater()


def test_analysis_view_status_flags_and_repaired_integrity(qapp):
    from ui.analysis_view import AnalysisView
    src = (ROOT / "ui/analysis_view.py").read_text(encoding="utf-8")
    assert "TIME_ESTIMATED_WARNING" in src and 'damage_source", "") == "db"' in src
    result = SimpleNamespace(is_sequence=False, source_copy_path="/c/src.avi", sha256="a" * 64,
                             rear_copy_path="", rear_sha256="", track_mode="", duration_sec=10.0,
                             repaired_path="/c/src_repaired.avi", repaired_sha256="b" * 64,
                             extraction=SimpleNamespace(routing=SimpleNamespace(container="avi")))
    views = AnalysisView._build_file_views(SimpleNamespace(), result)
    assert ("재생 사본(헤더 이식)", "/c/src_repaired.avi", "b" * 64) in views[0]["files"]
    # 이식 사본의 해시가 다르면 표시등이 빨강이다.
    host = SimpleNamespace(_integrity_results={"/c/src.avi": ("ok", ""), "/c/src_repaired.avi": ("bad", "원본 b… / 사본 c…")})
    state, detail = AnalysisView._integrity_of(host, views[0]["files"])
    assert state == "bad" and "재생 사본(헤더 이식)" in detail


def test_report_receives_analysis_settings(qapp, monkeypatch):
    """후방 손상·대조 불가 기록(analysis_settings)이 리포트 렌더러에 전달된다(D2 요청)."""
    from ui import main_window
    received = {}
    monkeypatch.setattr(main_window, "render_report_html", lambda *a, **kw: received.update(kw) or "<html></html>")
    monkeypatch.setattr(main_window, "ReportExporter", lambda html, out, cb, parent=None: SimpleNamespace(deleteLater=lambda: None, close=lambda: None))
    monkeypatch.setattr(main_window.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("/tmp/r.pdf", "")))
    host = SimpleNamespace(
        _ask_report_format=lambda: "pdf", _report_exporter=None, _report_basename=lambda: "case",
        _report_dir=lambda: "/tmp", _current_case_number="C", _current_examiner="e", _current_memo="m",
        _current_settings={"rear_damage": {"damaged": True}, "pair_check": {"status": "damaged"}},
        _current_case_id=1, _history_db_path="x",
        _analysis_view=SimpleNamespace(capture_visuals=lambda: (None, None), set_report_enabled=lambda on: None))
    main_window.MainWindow._on_report_requested(host, SimpleNamespace())
    assert received["analysis_settings"] == host._current_settings
