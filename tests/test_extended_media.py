"""Additional integration regressions for multi-track and failure recovery."""
import random
import subprocess
from pathlib import Path
import pytest
from test_review_regressions import qapp, tracker, point, case_env
from test_media_smoke import video, wait_until
from PySide6.QtTest import QTest
from PySide6.QtMultimedia import QMediaPlayer


def test_invalid_video_then_valid_recovers(tracker, qapp, video, tmp_path):
    bad = tmp_path / 'bad.mp4'
    bad.write_bytes(b'not a video')
    tracker.load_video(str(bad))
    assert wait_until(qapp, lambda: tracker._player.error() != QMediaPlayer.NoError)
    assert not tracker._play_btn.isEnabled()
    tracker.load_video(video)
    assert wait_until(qapp, lambda: tracker._player.isSeekable() and not tracker._priming and not tracker._prime_pending)
    assert tracker._play_btn.isEnabled()
    tracker._toggle_play()
    assert wait_until(qapp, lambda: tracker._player.position() > 300)


def test_dual_track_modes_then_single(tracker, qapp, video, tmp_path):
    dual = str(tmp_path/'dual.mp4')
    subprocess.run(['ffmpeg', '-v', 'error', '-i', video, '-map', '0:v', '-map', '0:v', '-c', 'copy', dual], check=True)
    for mode in ('both', 'rear', 'front', 'both'):
        tracker.load_video(dual, track_mode=mode)
        assert wait_until(qapp, lambda: tracker._player.isSeekable() and not tracker._priming and not tracker._prime_pending)
        assert tracker._player.activeVideoTrack() == (1 if mode == 'rear' else 0)
        assert tracker.has_rear_video() == (mode == 'both')
        if mode == 'both':
            assert wait_until(qapp, lambda: tracker._rear_player.isSeekable() and tracker._rear_player.activeVideoTrack() == 1)
    tracker.load_video(video)
    assert wait_until(qapp, lambda: tracker._player.isSeekable() and not tracker._priming and not tracker._prime_pending)
    assert not tracker.has_rear_video()
    assert tracker._rear_player.source().isEmpty()
    assert tracker._player.activeVideoTrack() == 0


def test_random_seeks_and_rate_mute_controls(tracker, qapp, video):
    tracker.load_track([point(i, speed=i*10) for i in range(1,6)])
    tracker.load_video(video)
    assert wait_until(qapp, lambda: tracker._player.isSeekable() and not tracker._priming and not tracker._prime_pending)
    rng=random.Random(23)
    for _ in range(100):
        target=rng.randrange(0,5900)
        tracker._on_slider_moved(target)
        assert tracker._player.position() == target
        assert tracker._seek_slider.value() == target
        expected = int(target/1000)*10
        assert tracker._speed_label.text() == (f'속도 {expected:.1f} km/h' if target >= 1000 else '속도 -')
    tracker._mute_btn.setChecked(True)
    assert tracker._audio.isMuted()
    tracker._mute_btn.setChecked(False)
    assert not tracker._audio.isMuted()
    tracker._volume.setValue(25)
    assert abs(tracker._audio.volume()-.25) < .001
    for i in range(tracker._rate_combo.count()):
        tracker._rate_combo.setCurrentIndex(i)
        assert tracker._player.playbackRate() == float(tracker._rate_combo.currentData())


def test_rear_copy_mismatch_rolls_back(case_env, monkeypatch):
    from core import pipeline
    src, store, run, extract = case_env
    rear=src.parent/'rear.mp4';rear.write_bytes(b'rear evidence')
    original=pipeline.shutil.copy2
    def copy(a,b):
        original(a,b)
        if str(a)==str(rear):Path(b).write_bytes(b'corrupted')
    monkeypatch.setattr(pipeline.shutil,'copy2',copy)
    with pytest.raises(ValueError,match='후방 사본'):run(rear_video_path=str(rear))
    assert not store.list_cases()
    assert not list((src.parent/'cases').glob('*'))
    extract.assert_not_called()


def test_cancel_after_copy_cleans_case(case_env):
    import threading
    from engine.engine_adapter import CancelledError
    src,store,run,extract=case_env
    event=threading.Event()
    def progress(message):
        if '분석 사본' in message:event.set()
    with pytest.raises(CancelledError):run(cancel_event=event,progress_cb=progress)
    assert not store.list_cases()
    assert not list((src.parent/'cases').glob('*'))
    extract.assert_not_called()


def test_home_stops_hidden_playback(tracker, qapp, video):
    from types import SimpleNamespace
    from ui.main_window import MainWindow
    tracker.load_video(video)
    assert wait_until(qapp, lambda: tracker._player.isSeekable() and not tracker._priming and not tracker._prime_pending)
    tracker._toggle_play()
    assert wait_until(qapp, lambda: tracker._player.position() > 300)
    switched=[]
    window=SimpleNamespace(_analysis_view=SimpleNamespace(_tracker_tab=tracker),
        _refresh_history=lambda:None,_home=object(),
        _stack=SimpleNamespace(setCurrentWidget=lambda widget:switched.append(widget)))
    MainWindow._show_home(window)
    assert tracker._player.playbackState() == QMediaPlayer.StoppedState
    assert tracker._rear_player.playbackState() == QMediaPlayer.StoppedState
    assert not tracker._prime_timer.isActive()
    assert switched == [window._home]
