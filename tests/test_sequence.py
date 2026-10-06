"""연속 영상 이어보기: 이어짐 검사, 구간 합치기, CSV, Tracker 조작 - 합성 데이터, 영상·키 불필요."""
import csv
import datetime as dt
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest

from core import pipeline
from core.driving_events import detect_driving_events
from core.format_sniffer import RoutingResult
from core.location_table import write_csv
from core.video_sequence import BASIS_FILENAME, BASIS_GPS, ClipProbe, SequenceSlot, check_chain
from engine.engine_adapter import ExtractionResult, TrackPoint

T0 = dt.datetime(2025, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def clip(name, start_sec, duration=60.0, first=(37.5000, 127.0000), last=(37.5050, 127.0000),
         speed=40.0, gps=True, sig=None):
    return ClipProbe(
        path=f"/x/{name}", container="mp4", signature=sig or {"brand": "mp42", "model": "Dashcam V1 CH:1"},
        duration=duration, gps_start=T0 + dt.timedelta(seconds=start_sec) if gps else None,
        name_start=(T0 + dt.timedelta(seconds=start_sec)).replace(tzinfo=None),
        first_fix=(0.0, *first), last_fix=(duration - 1, *last), max_speed_kmh=speed)


def test_consecutive_clips_are_sorted_and_accepted():
    a = clip("a.mp4", 0, last=(37.5050, 127.0))
    b = clip("b.mp4", 60, first=(37.5051, 127.0), last=(37.5100, 127.0))
    ordered, basis, problems, _notes = check_chain([SequenceSlot(front=b), SequenceSlot(front=a)])
    assert problems == [] and basis == BASIS_GPS
    assert [s.front.name for s in ordered] == ["a.mp4", "b.mp4"]


@pytest.mark.parametrize("start, first, expect", [
    (74 * 60, (37.5051, 127.0), "뒤에 다음 영상이 시작"),      # 74분 공백
    (30, (37.5051, 127.0), "겹칩니다"),                         # 앞 영상과 30초 겹침
    (60, (37.6000, 127.0), "떨어져 있습니다"),                   # 1초 사이에 10 km 이동
])
def test_broken_chain_is_blocked(start, first, expect):
    a = clip("a.mp4", 0)
    b = clip("b.mp4", start, first=first)
    _o, _b, problems, _n = check_chain([SequenceSlot(front=a), SequenceSlot(front=b)])
    assert any(expect in p for p in problems)


def test_no_time_basis_is_blocked_and_filename_basis_is_used():
    a, b = clip("a.mp4", 0, gps=False), clip("b.mp4", 60, gps=False, first=(37.5051, 127.0))
    _o, basis, problems, notes = check_chain([SequenceSlot(front=a), SequenceSlot(front=b)])
    assert problems == [] and basis == BASIS_FILENAME and notes
    a.name_start = None
    _o, _b, problems, _n = check_chain([SequenceSlot(front=a), SequenceSlot(front=b)])
    assert any("검증할 근거가 없습니다" in p for p in problems)


def test_different_device_is_blocked():
    a = clip("a.mp4", 0)
    b = clip("b.mp4", 60, first=(37.5051, 127.0), sig={"brand": "iso4", "model": "AMBA"})
    _o, _b, problems, _n = check_chain([SequenceSlot(front=a), SequenceSlot(front=b)])
    assert any("기기" in p for p in problems)


def _segment(index, offset, speeds):
    pts = [TrackPoint(start_time_sec=float(i), latitude=37.5 + i * 1e-4, longitude=127.0, speed_kmh=v,
                      gps_date="2025-09-01", gps_utc_time=f"12:{index:02}:{i:02}")
           for i, v in enumerate(speeds)]
    ex = ExtractionResult(RoutingResult("mp4", True, ""), pts, [], f"/c/{index}.mp4")
    return pipeline.SegmentResult(index, f"/c/{index}.mp4", "", "a" * 64, "", "", float(len(speeds)),
                                  offset, ex, detect_driving_events(pts))


def test_combine_segments_shifts_time_and_indices():
    s1 = _segment(0, 0.0, [30, 30, 40, 40])
    s2 = _segment(1, 4.0, [30, 30, 40, 40])
    combined, events = pipeline.combine_segments([s1, s2])
    assert [p.start_time_sec for p in combined.points] == [0, 1, 2, 3, 4, 5, 6, 7]
    assert [p.segment_index for p in combined.points] == [0] * 4 + [1] * 4
    assert [(e.start_index, e.start_time_sec) for e in events] == [(1, 1.0), (5, 5.0)]
    # 경계를 넘는 판정은 없다(3→4 사이 40→30은 두 파일 사이라 보지 않는다)
    assert all(e.end_index < 4 or e.start_index >= 4 for e in events)


def test_location_csv_matches_table(tmp_path):
    s1, s2 = _segment(0, 0.0, [30, 30, 40]), _segment(1, 3.0, [40, 40, 40])
    combined, events = pipeline.combine_segments([s1, s2])
    path = tmp_path / "loc.csv"
    assert write_csv(str(path), combined.points, events, ["video1", "video2"]) == 6
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    assert rows[0] == ["영상", "시각(초)", "위도", "경도", "속도(km/h)", "위험운전", "충격(g)", "GPS 검증"]
    assert rows[1][0] == "video1" and rows[-1][0] == "video2" and rows[-1][1] == "5.00"
    assert rows[2][5] == "급가속"


@pytest.fixture
def tracker(monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget
    from ui import tracker_tab
    app = QApplication.instance() or QApplication([])

    class MapStub(QWidget):
        def set_track(self, *a): pass
        def set_playback_time(self, t): self.t = t
    monkeypatch.setattr(tracker_tab, 'MapView', MapStub)
    tab = tracker_tab.TrackerTab()
    yield tab
    tab.release_media(); tab.deleteLater(); app.processEvents()


def test_playlist_global_time(tracker):
    from ui.tracker_tab import PlaylistItem
    tracker.load_playlist([PlaylistItem("/missing/a.mp4", duration_sec=60, label="video1"),
                           PlaylistItem("/missing/b.mp4", duration_sec=60, label="video2")])
    assert tracker.total_duration_ms() == 120000
    assert tracker._locate(75000) == (1, 15000)
    assert tracker._locate(59999) == (0, 59999)
    assert tracker._segment_label.text().startswith("영상 1/2")


def test_keys_and_skip_buttons(tracker):
    from PySide6.QtCore import Qt
    from ui.tracker_tab import SKIP_MS
    assert SKIP_MS == 3000 and tracker._back_btn.text() == "-3s"
    assert tracker._play_btn.focusPolicy() == Qt.NoFocus
    moved = []
    tracker._skip = lambda delta: moved.append(delta)
    tracker.step_frame = lambda d: moved.append(("frame", d))

    class Key:
        def __init__(self, k): self._k = k
        def key(self): return self._k
    for k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Comma, Qt.Key_Greater):
        assert tracker._on_key(Key(k))
    assert moved == [-1000, 1000, ("frame", -1), ("frame", 1)]
    assert not tracker._on_key(Key(Qt.Key_A))


def test_seek_slider_click_jumps(tracker):
    from PySide6.QtCore import QPointF, Qt, QEvent
    from PySide6.QtGui import QMouseEvent
    slider = tracker._seek_slider
    slider.resize(400, 20)
    slider.setRange(0, 100000)
    got = []
    slider.valueChanged.connect(got.append)
    point = QPointF(300, 10)
    slider.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, point, point, Qt.LeftButton,
                                       Qt.LeftButton, Qt.NoModifier))
    assert got and 65000 < got[0] < 85000


def test_one_second_rows_and_frame_detail():
    from core.location_table import gps_slot_rows, has_frame_detail, row_texts
    # INAVI형: GPS 1초 + 사이 G센서 행 9개
    pts = []
    for sec in range(3):
        pts.append(TrackPoint(start_time_sec=float(sec), latitude=37.5, longitude=127.0, speed_kmh=10,
                              gps_date="2025-09-01", gps_utc_time=f"12:00:{sec:02}"))
        pts += [TrackPoint(start_time_sec=sec + 0.1 * k, x_g=0.1, y_g=0.1, z_g=1.0) for k in range(1, 10)]
    assert gps_slot_rows(pts) == [0, 10, 20] and has_frame_detail(pts)
    assert row_texts(pts[1], [])[1:3] == ["-", "-"]          # GPS 없는 행은 "-"
    plain = [p for i, p in enumerate(pts) if i in (0, 10, 20)]
    assert not has_frame_detail(plain)


def test_slack_track_orders_by_gps_time():
    from core.slack import build_slack_set
    raw = [TrackPoint(latitude=37.5, longitude=127.0, speed_kmh=5, gps_date="2023-05-26", gps_utc_time="01:00:05"),
           TrackPoint(latitude=37.5, longitude=127.0, speed_kmh=5, gps_date="2023-05-26", gps_utc_time="01:00:00"),
           TrackPoint(latitude=37.6, longitude=127.0, speed_kmh=5)]
    slack = build_slack_set(raw, "video2")
    assert slack.label == "video2 슬랙" and slack.dates == ["2023-05-26"]
    assert [p.start_time_sec for p in slack.points] == [0.0, 5.0, None]
    assert build_slack_set([], "x") is None
