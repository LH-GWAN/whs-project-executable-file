"""G센서 감지 및 캡처 경계 조건. GUI/PDF 실제 실행 검증은 별도로 한다."""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from core.impact import find_first_impact
from report.impact_capture import choose_capture_source, capture_frame_png


@dataclass
class Sample:
    start_time_sec: float
    g_magnitude: float


def test_default_threshold_and_first_only():
    data = [Sample(0, 1.0), Sample(.1, 1.31), Sample(.2, 2.7), Sample(.3, 9.0)]
    ev = find_first_impact(data)
    assert ev.index == 2 and ev.time_sec == pytest.approx(.2)
    assert ev.previous_g == pytest.approx(1.31)


def test_131_demo_only_with_explicit_threshold():
    data = [Sample(45.9, 1.064), Sample(46.0, 1.391)]
    assert find_first_impact(data) is None
    ev = find_first_impact(data, threshold=1.30)
    assert ev.time_sec == 46.0 and ev.ratio == pytest.approx(1.391 / 1.064)


def test_bad_values_break_chain():
    from math import nan, inf
    data = [Sample(0, 0), Sample(.1, 100), Sample(.2, nan), Sample(.3, 1),
            Sample(.4, inf), Sample(.5, 1), Sample(.6, 0), Sample(.7, 100)]
    assert find_first_impact(data) is None
    assert find_first_impact([Sample(1, 1), Sample(1, 10), Sample(2, 1)]) is None
    assert find_first_impact([Sample(2, 1), Sample(1, 9)]) is None


def test_invalid_thresholds():
    for threshold in (1, 0, -2, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            find_first_impact([], threshold=threshold)


def test_selected_source_and_rear_track():
    def make(mode, rear=""):
        return SimpleNamespace(track_mode=mode, rear_copy_path=rear,
                               source_copy_path="front.mp4")
    assert choose_capture_source(make("both", "rear.mp4")) == ("front.mp4", 0)
    assert choose_capture_source(make("front")) == ("front.mp4", 0)
    assert choose_capture_source(make("rear")) == ("front.mp4", 1)
    assert choose_capture_source(make("rear", "rear.mp4")) == ("rear.mp4", 0)


def test_missing_video_has_visible_reason(tmp_path):
    png, reason = capture_frame_png(str(tmp_path / "missing.mp4"), 46.0)
    assert png is None and "없습니다" in reason
