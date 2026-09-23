"""Real Qt decoder checks using a locally generated clip; map/network are isolated."""
import os
import shutil
import subprocess
import time
import pytest
from test_review_regressions import qapp, tracker, point
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtMultimedia import QMediaPlayer


def wait_until(qapp, condition, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition(): return True
        QTest.qWait(20)
    return False


@pytest.fixture(scope='module')
def video(tmp_path_factory):
    if not shutil.which('ffmpeg'): pytest.skip('ffmpeg required to create synthetic video')
    path = tmp_path_factory.mktemp('media')/'test.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=320x180:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
        '-t', '6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-y', str(path)], check=True)
    return str(path)


def test_reopen_play_seek_frames(tracker, qapp, video):
    frames = []
    tracker._video_widget.videoSink().videoFrameChanged.connect(
        lambda frame: frames.append(frame.startTime()) if frame.isValid() else None)
    tracker.load_track([point(0), point(1), point(4)])
    for _ in range(3):
        frames.clear()
        tracker.load_video(video)
        assert wait_until(qapp, lambda: tracker._player.isSeekable() and not tracker._prime_pending and not tracker._priming)
        tracker._toggle_play()
        assert wait_until(qapp, lambda: tracker._player.position() > 350)
        assert tracker._player.playbackState() == QMediaPlayer.PlayingState
        tracker._toggle_play()
        assert len(frames) > 5
        tracker._seek_slider.setValue(2000)
        assert wait_until(qapp, lambda: abs(tracker._player.position() - 2000) < 100)
        frames.clear()
        QTest.keyClick(tracker._seek_slider, Qt.Key_Right)
        assert wait_until(qapp, lambda: abs(tracker._player.position() - 3000) < 100)
        assert wait_until(qapp, lambda: any(2900000 <= t <= 3200000 for t in frames))


def test_seek_during_priming_not_reset(tracker, qapp, video):
    tracker.load_video(video)
    assert wait_until(qapp, lambda: tracker._player.isSeekable() and tracker._priming)
    tracker._on_slider_moved(3500)
    QTest.qWait(300)
    assert abs(tracker._player.position() - 3500) < 100


def test_real_engine_no_gps_roundtrip(video, tmp_path):
    from core.pipeline import run_analysis_pipeline, reopen_case
    from storage.history_store import HistoryStore
    with HistoryStore(str(tmp_path/'history.db')) as store:
        result = run_analysis_pipeline(video, 'SYNTHETIC', 'test', '', {}, str(tmp_path/'cases'), store)
        assert result.extraction.status == 'no_gps'
        assert result.extraction.engine_runs and all(r.exit_code == 0 for r in result.extraction.engine_runs)
        assert reopen_case(store.list_cases()[0]).extraction.status == 'no_gps'


def test_short_rear_recovers_after_reverse_seek(tracker, qapp, video, tmp_path):
    rear = str(tmp_path/'short.mp4')
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', video,
                    '-t', '2', '-c', 'copy', rear], check=True)
    tracker.load_video(video, rear)
    assert wait_until(qapp, lambda: tracker._player.isSeekable() and tracker._rear_player.isSeekable()
                      and not tracker._priming and not tracker._rear_prime_timer.isActive())
    tracker._on_slider_moved(4000)
    QTest.qWait(100)
    assert '후방 영상 없음' in tracker._media_label.text()
    assert tracker._rear_player.playbackState() != QMediaPlayer.PlayingState
    tracker._on_slider_moved(1000)
    assert wait_until(qapp, lambda: abs(tracker._rear_player.position() - 1000) < 100)
    assert '후방 영상 없음' not in tracker._media_label.text()


def test_play_during_priming_keeps_playing(tracker, qapp, video):
    tracker.load_video(video)
    assert wait_until(qapp, lambda: tracker._player.isSeekable() and tracker._priming)
    tracker._toggle_play()
    QTest.qWait(400)
    assert tracker._player.playbackState() == QMediaPlayer.PlayingState
    assert tracker._player.position() > 250


def test_case_without_video_releases_previous_source(qapp, monkeypatch, video):
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QWidget
    from ui import tracker_tab, location_tab
    from ui.analysis_view import AnalysisView
    from core.pipeline import PipelineResult
    from core.format_sniffer import RoutingResult
    from engine.engine_adapter import ExtractionResult
    class MapStub(QWidget):
        online_map_failed = Signal(str, str)
        def set_track(self, *args): pass
        def set_playback_time(self, *args): pass
        def ensure_loaded(self): pass
    monkeypatch.setattr(tracker_tab, 'MapView', MapStub)
    monkeypatch.setattr(location_tab, 'MapView', MapStub)
    view = AnalysisView()
    extraction = ExtractionResult(RoutingResult('mp4', True, ''), [], [], '')
    old = PipelineResult(1, '', video, extraction, 6, [], '', 3)
    view.load_result(old, 'A', {})
    assert wait_until(qapp, lambda: view._tracker_tab._player.isSeekable())
    new = PipelineResult(2, '', '', extraction, None, [], '', 3)
    view.load_result(new, 'B', {})
    assert view._tracker_tab._player.source().isEmpty()
    assert view._tracker_tab._rear_player.source().isEmpty()
    assert view._tracker_tab._duration_hint_ms == 0
    assert not view._tracker_tab._play_btn.isEnabled()
    assert 'AVI 복구 없음' in view._analysis_status.text()
    view.deleteLater(); qapp.processEvents()
