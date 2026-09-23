"""Offline regressions for the September review; no private videos or online keys."""
import csv
import json
import math
import os
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from engine.engine_adapter import (TrackPoint, ExtractionResult, load_timeline,
    load_coordinates_as_points, run_full_extraction, _classify_outcome)
from core import pipeline, hashing, outliers
from core.acceleration import _distinct_fix_indices, compute_point_accelerations
from core.format_sniffer import RoutingResult
from core.video_pairs import rear_candidates
from storage.history_store import HistoryStore


def point(t, speed=30, lat=37, **kwargs):
    return TrackPoint(start_time_sec=t, latitude=lat, longitude=127,
                      speed_kmh=speed, gps_date='2026-09-23', gps_utc_time=f'00:00:{int(t):02}', **kwargs)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -float('inf'), 91])
def test_invalid_coordinates(bad):
    p = point(0, lat=bad)
    assert not p.has_fix
    outliers.mark_outliers([p])
    assert p.is_outlier


def test_zero_coordinates_valid():
    p = TrackPoint(latitude=0, longitude=0)
    outliers.mark_outliers([p])
    assert p.has_fix


def test_stats_and_chart_share_trusted_distinct_records():
    points = [point(0), point(.1), point(1, 999, is_outlier=True),
              point(2, 100, gps_checksum_ok=False), point(3, math.inf),
              point(4, 120, gps_trusted=False), point(5, 40)]
    assert _distinct_fix_indices(points) == [0, 6]
    assert all(v is None or math.isfinite(v) for v in compute_point_accelerations(points))


def test_csv_preserves_trust_and_sanitizes_nan(tmp_path):
    path = tmp_path/'timeline.csv'
    path.write_text('start_time_sec,latitude,longitude,speed_kmh,gps_checksum_ok,gps_trusted\n0,37,127,nan,True,False\n')
    p = load_timeline(str(path))[0]
    assert p.speed_kmh is None and not p.has_fix and p.gps_trusted is False


def test_failed_trust_not_no_gps():
    assert _classify_outcome([], '', [point(0, gps_checksum_ok=False)])[0] == 'gps_untrusted'


def test_output_reuse_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr('engine.engine_adapter.sniff', lambda _: RoutingResult('mp4', True, ''))
    (tmp_path/'old.csv').write_text('old')
    with pytest.raises(ValueError, match='비어'):
        run_full_extraction('unused', str(tmp_path))


def test_numbered_rear_names():
    assert 'REC_20240916_172436_R_1.avi' in rear_candidates('REC_20240916_172436_F_1.avi')
    assert 'video_R.mp4' in rear_candidates('video_F.mp4')


@pytest.fixture
def case_env(tmp_path, monkeypatch):
    src = tmp_path/'input.mp4'; src.write_bytes(b'example evidence')
    store = HistoryStore(str(tmp_path/'history.db'))
    monkeypatch.setattr(pipeline.format_sniffer, 'sniff', lambda _: RoutingResult('mp4', True, ''))
    monkeypatch.setattr(pipeline.duration_mod, 'get_duration_sec', lambda *a, **k: 5)
    extract = Mock(return_value=ExtractionResult(RoutingResult('mp4', True, ''), [], [], str(src), status='no_gps'))
    monkeypatch.setattr(pipeline, 'run_full_extraction', extract)
    def run(**kwargs):
        return pipeline.run_analysis_pipeline(str(src), 'case', 'examiner', '', {},
                  str(tmp_path/'cases'), store, **kwargs)
    yield src, store, run, extract
    store.close()


def test_copy_mismatch_rolls_back_before_engine(case_env):
    src, store, run, extract = case_env
    with pytest.raises(ValueError, match='SHA-256'):
        run(precomputed_sha256='0'*64)
    extract.assert_not_called()
    assert store.list_cases() == []
    assert not list((src.parent/'cases').glob('*'))


def test_late_failure_rolls_back(case_env, monkeypatch):
    src, store, run, _ = case_env
    monkeypatch.setattr(pipeline, '_write_case_json', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError): run()
    assert store.list_cases() == []
    assert not list((src.parent/'cases').glob('*'))


def test_reopen_preserves_no_gps(case_env):
    src, store, run, _ = case_env
    result = run()
    saved = json.loads((Path(result.case_folder)/'case.json').read_text())
    assert saved['copy_sha256_verified'] is True
    assert saved['source_video_sha256'] == hashing.sha256_file(str(src))
    reopened = pipeline.reopen_case(store.list_cases()[0])
    assert reopened.extraction.status == 'no_gps'


@pytest.fixture(scope='session')
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def tracker(qapp, monkeypatch):
    from PySide6.QtWidgets import QWidget
    from ui import tracker_tab
    class MapStub(QWidget):
        def set_track(self, *a): pass
        def set_playback_time(self, t): self.seconds = t
    monkeypatch.setattr(tracker_tab, 'MapView', MapStub)
    monkeypatch.setattr(tracker_tab.geocode, 'is_available', lambda: False)
    tab = tracker_tab.TrackerTab()
    yield tab
    tab.release_media(); tab.deleteLater(); qapp.processEvents()


def test_reverse_seek_and_before_first_fix(tracker):
    tracker.load_track([point(1, 20), point(2, 20), point(3, 20),
                        TrackPoint(start_time_sec=5), point(40, 80, lat=38)])
    for t, text in [(40.7, '38.000000'), (5, '37.000000'), (0, '위치 -'), (1.5, '37.000000')]:
        tracker._update_info(t)
        assert text in tracker._coord_label.text()
    tracker._update_info(0)
    assert tracker._clock_label.text() == '날짜·시간 -'


def test_slot_never_selects_future_record(tracker):
    tracker.load_track([point(.4), point(1.45), point(2.4)])
    assert tracker._slot_point(1.42).start_time_sec <= 1.42


def test_old_prime_timer_cancelled(tracker, qapp):
    tracker._priming = True
    tracker._prime_timer.start(1)
    tracker.release_media()
    assert not tracker._prime_timer.isActive()
    assert not tracker._priming


def test_missing_media_has_error_and_disabled_controls(tracker):
    tracker.load_video('/does-not-exist.mp4')
    assert '영상 재생 오류' in tracker._media_label.text()
    assert not tracker._play_btn.isEnabled()


def test_audio_and_keyboard_step(tracker):
    assert tracker._player.audioOutput() is tracker._audio
    assert tracker._rear_player.audioOutput() is None
    assert tracker._seek_slider.singleStep() == 1000
    assert tracker._seek_slider.hasTracking() is False


def test_speed_widget_excludes_outlier(qapp):
    from ui.speed_tab import SpeedTab
    tab = SpeedTab()
    tab.load([point(0), point(1, 999, is_outlier=True)], [])
    assert '30.0' in tab._avg_label.text() and '30.0' in tab._max_label.text()
    assert '999' not in tab._max_label.text()
    tab.deleteLater()


def test_stationary_heading_does_not_look_into_future():
    from ui.map_view import compute_headings
    points = [point(0, 10, track_deg=90), point(1, 0), point(2, 0), point(3, 10, lat=37.001, track_deg=0)]
    assert compute_headings(points) == [90, 90, 90, 0]


def test_empty_selection_rejected(qapp, monkeypatch):
    from ui.case_info_dialog import CaseInfoDialog, QMessageBox
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: None)
    dialog = CaseInfoDialog(); dialog._case_number.setText('case')
    for cb in (dialog._tracker_cb, dialog._speed_cb, dialog._location_cb): cb.setChecked(False)
    dialog._on_start()
    assert dialog.result_input is None
    dialog.deleteLater()


def test_report_sampling_and_escaped_warnings():
    from report.report_builder import _select_row_indices, render_report_html
    records = [point(i) for i in range(600)]
    indices = _select_row_indices(records, [])
    assert len(indices) == 200 and indices[0] == 0 and indices[-1] == 599
    extraction = ExtractionResult(RoutingResult('mp4', True, ''), records, [], '', warnings=['<script>bad</script>'])
    result = pipeline.PipelineResult(1, '', '', extraction, 600, [], 'abc', 3)
    html = render_report_html(result, 'case', 'examiner', '')
    assert 'class="gap"' not in html and '&lt;script&gt;' in html
    assert 'GPS 검증' in html and '분석 상태' in html


def test_existing_case_folder_is_never_deleted(case_env):
    src, store, run, extract = case_env
    folder = src.parent/'cases'/'case_1'; folder.mkdir(parents=True)
    marker = folder/'existing-evidence'; marker.write_text('preserve')
    with pytest.raises(FileExistsError): run()
    assert marker.read_text() == 'preserve'
    assert store.list_cases() == []
    extract.assert_not_called()


def test_csv_track_selection_uses_valid_fixes(tmp_path):
    from engine.engine_adapter import pick_primary_csv
    paths = []
    for name, rows in [('bad', ['0,37,127,False']*3), ('good', ['0,37,127,True'])]:
        path = tmp_path/name/'timeline.csv'; path.parent.mkdir()
        path.write_text('start_time_sec,latitude,longitude,gps_checksum_ok\n'+'\n'.join(rows))
        paths.append(str(path))
    assert pick_primary_csv(paths) == paths[1]


def test_rear_failure_does_not_disable_front(tracker):
    tracker._on_media_error('test codec failure', rear=True)
    assert tracker._play_btn.isEnabled()
    assert '후방 영상 오류' in tracker._media_label.text()


def test_legacy_timeline_restores_sidecar_trust(tmp_path):
    (tmp_path/'coordinates.csv').write_text('start_time_sec,latitude,longitude,date,utc_time,trusted\n0,37,127,2026-09-23,00:00:00,False\n')
    timeline = tmp_path/'timeline.csv'
    timeline.write_text('start_time_sec,latitude,longitude,gps_date,gps_utc_time,gps_checksum_ok\n0,37,127,2026-09-23,00:00:00,True\n')
    assert not load_timeline(str(timeline))[0].has_fix


def test_location_verification_labels(qapp, monkeypatch):
    from PySide6.QtWidgets import QWidget
    from ui import location_tab
    class MapStub(QWidget):
        def set_track(self, *args): pass
    monkeypatch.setattr(location_tab, 'MapView', MapStub)
    tab = location_tab.LocationTab()
    tab.load([point(0, gps_checksum_ok=True), point(1, gps_checksum_ok=False), point(2)], [])
    assert [tab._table.item(i, 6).text() for i in range(3)] == ['정상', '실패', '미제공']
    assert tab._table.item(1, 3).text() == '(검증 실패)'
    assert tab._table.item(1, 5).text() == '-'
    tab.deleteLater()
