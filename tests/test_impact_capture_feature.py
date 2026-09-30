"""G센서 감지 및 캡처 경계 조건. GUI/PDF 실제 실행 검증은 별도로 한다."""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from core.impact import DEFAULT_IMPACT_THRESHOLD_G, find_first_impact
from report.impact_capture import choose_capture_source, capture_frame_png


@dataclass
class Sample:
    start_time_sec: float
    g_magnitude: float


def baseline(g=.2):
    return [Sample(i / 10, g) for i in range(20)]


@pytest.mark.parametrize("g", [0, .15, .4, 1.4])
def test_same_absolute_deviation_across_device_baselines(g):
    ev = find_first_impact(baseline(g) + [Sample(2, g + 3.1), Sample(2.1, 9)])
    assert ev.index == 20 and ev.time_sec == 2
    assert ev.baseline_g == pytest.approx(g)
    assert ev.deviation_g == pytest.approx(3.1)


def test_small_denominator_and_large_ratio_do_not_trigger():
    assert find_first_impact(baseline(.15) + [Sample(2, .45)]) is None
    assert find_first_impact(baseline(1.064) + [Sample(2, 1.391)]) is None


def test_current_excluded_and_inclusive_boundary():
    ev = find_first_impact(baseline(0) + [Sample(2, 3)])
    assert ev.baseline_g == 0 and ev.deviation_g == 3
    assert find_first_impact(baseline(0) + [Sample(2, 2.999)]) is None
    assert find_first_impact(baseline(3.5) + [Sample(2, .5)]).deviation_g == pytest.approx(3.0)


@pytest.mark.parametrize("bad", [None, -1, float("nan"), float("inf"), "1", True])
def test_bad_values_reset_history(bad):
    assert find_first_impact(baseline() + [Sample(2, bad), Sample(2.1, 5)]) is None
    assert find_first_impact(baseline() + [Sample(bad, .2), Sample(2.1, 5)]) is None


@pytest.mark.parametrize("time", [1.9, 1.8, 4])
def test_nonmonotonic_and_gap_reset(time):
    assert find_first_impact(baseline() + [Sample(time, 9)]) is None


def test_warmup_and_window_expiration():
    assert find_first_impact([Sample(0, .2), Sample(.5, 5)]) is None
    points = [Sample(0, .8)] + [Sample(i / 10, .2) for i in range(1, 22)]
    ev = find_first_impact(points + [Sample(2.2, 3.2)])
    assert ev and ev.baseline_g == pytest.approx(.2)


def test_segment_boundary_resets_baseline():
    points = [SimpleNamespace(start_time_sec=i / 10, g_magnitude=.2, segment_index=0)
              for i in range(20)]
    points.append(SimpleNamespace(start_time_sec=2, g_magnitude=5, segment_index=1))
    assert find_first_impact(points) is None


@pytest.mark.parametrize("threshold", [0, -2, float("nan"), float("inf"), None, True])
def test_invalid_thresholds(threshold):
    with pytest.raises(ValueError):
        find_first_impact([], threshold_g=threshold)


def test_explicit_threshold_is_g_not_ratio():
    assert find_first_impact(baseline(.2) + [Sample(2, .6)], threshold_g=.3)


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


@pytest.mark.parametrize("delta, detected", [(1.0, False), (2.99, False), (3.0, True), (3.01, True)])
@pytest.mark.parametrize("g", [0.0, .15, .4, 1.4])
def test_default_three_g_boundary(g, delta, detected):
    # Pin the product default independently of the implementation constant.
    assert DEFAULT_IMPACT_THRESHOLD_G == 3.0
    event = find_first_impact(baseline(g) + [Sample(2, g + delta)])
    assert (event is not None) == detected
    if event:
        assert event.threshold_g == 3.0
        assert event.deviation_g == pytest.approx(delta)
