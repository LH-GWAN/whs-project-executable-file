"""Frame PTS verification and offline report regression tests.

Use: python -m pytest -q tests
Real FFmpeg checks skip if the system binary is unavailable; native Qt PDF may
be tested separately on a Windows/PySide6 environment.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.impact import find_first_impact
from report.impact_capture import (
    MAX_FRAME_OFFSET_SEC, PNG_HEADER, _verified, capture_frame_evidence,
    choose_capture_source,
)
from report.report_html_snapshot import ReportHtmlSnapshot


def test_timestamp_required_and_fail_closed():
    png = PNG_HEADER + b"test"
    assert _verified(png, 1, None, "test").png is None
    assert _verified(png, 1, 1.5, "test").png is None
    assert _verified(png, 1, 1.04, "test").png == png
    assert _verified(png, 1, .98, "Qt", allow_preceding=True).png == png
    assert _verified(png, 1, .98, "ffmpeg").png is None
    assert _verified(b"invalid", 1, 1.04, "test").png is None


def test_g_sensor_threshold_and_first_event():
    class P:
        def __init__(self, t, g):
            self.start_time_sec, self.g_magnitude = t, g
    points = [P(0, 1.0), P(.1, 1.31), P(.2, 2.7), P(.3, 9)]
    assert find_first_impact(points).index == 2
    assert find_first_impact(points, threshold=1.3).index == 1
    assert find_first_impact([P(0, 1), P(1, math.nan), P(2, 20)]) is None


def test_camera_selection():
    result = SimpleNamespace(source_copy_path="F.mp4", rear_copy_path="", track_mode="rear")
    assert choose_capture_source(result) == ("F.mp4", 1)
    result.rear_copy_path = "R.mp4"
    assert choose_capture_source(result) == ("R.mp4", 0)
    result.track_mode = "both"
    assert choose_capture_source(result) == ("F.mp4", 0)


def test_private_large_report_html_roundtrip_and_cleanup():
    html = "<html><body>" + "X" * 2_600_000 + "</body></html>"
    snapshot = ReportHtmlSnapshot(html)
    path = Path(snapshot.path)
    try:
        assert path.read_text(encoding="utf-8") == html
        if os.name == "posix":
            assert path.stat().st_mode & 0o077 == 0
            assert path.parent.stat().st_mode & 0o077 == 0
    finally:
        snapshot.close()
    assert not path.exists()
    snapshot.close()


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe required")
    base = tmp_path_factory.mktemp("impact-frames")
    path = base / "25fps.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
                    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-y", str(path)], check=True)
    return path


def _frame_times(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "frame=best_effort_timestamp_time",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return [float(line.split(",")[0]) for line in result.stdout.splitlines() if line.strip()]


@pytest.mark.parametrize("requested", [.01, .731, 1.0, 1.137, 2.151])
def test_ffmpeg_image_pts_matches_probe(video, requested):
    expected = next(t for t in _frame_times(video) if t >= requested - 1e-7)
    evidence = capture_frame_evidence(str(video), requested)
    if expected - requested > MAX_FRAME_OFFSET_SEC:
        assert evidence.png is None and evidence.error
    else:
        assert evidence.png and evidence.png.startswith(PNG_HEADER), evidence.error
        assert evidence.method == "ffmpeg"
        assert evidence.frame_time_sec == pytest.approx(expected, abs=.0001)
        assert evidence.offset_sec == pytest.approx(expected-requested, abs=.0001)


def test_no_unknown_or_wrong_track_can_claim_success(video):
    missing_track = capture_frame_evidence(str(video), .731, 2)
    assert missing_track.png is None
    beyond_length = capture_frame_evidence(str(video), 100)
    assert beyond_length.png is None


def test_report_renders_requested_and_actual_pts(video):
    pytest.importorskip("PySide6")
    from core.pipeline import PipelineResult
    from engine.engine_adapter import ExtractionResult, TrackPoint
    from core.format_sniffer import RoutingResult
    from report.report_builder import render_report_html

    actual = capture_frame_evidence(str(video), .731)
    assert actual.png, actual.error
    pts = [TrackPoint(start_time_sec=0, x_g=1, y_g=0, z_g=0),
           TrackPoint(start_time_sec=.731, x_g=2.1, y_g=0, z_g=0)]
    event = find_first_impact(pts)
    extraction = ExtractionResult(RoutingResult("mp4", True, ""), pts, [], str(video))
    result = PipelineResult(1, "", str(video), extraction, 3, [], "sha256", 3)
    html = render_report_html(
        result, "CASE", "EXAMINER", "", impact_event=event,
        impact_png=actual.png, impact_frame_time_sec=actual.frame_time_sec,
        impact_capture_method=actual.method)
    assert "캡처 영상 프레임 실제 PTS 0.760초" in html
    assert "감지 시각과 차이 +0.029초" in html


def test_ffmpeg6_multiple_showinfo_lines_use_encoded_first_frame(video, monkeypatch):
    """FFmpeg 6.x may log n:1 after encoding one PNG; n:0 is the PNG's PTS.

    This is a regression for the six failures in the first v3 GitHub Actions run.
    Preserve a real FFmpeg PNG but simulate extra informational log lines.
    """
    import report.impact_capture as capture
    from types import SimpleNamespace
    real_run = capture.subprocess.run

    def multi_showinfo(cmd, **kwargs):
        result = real_run(cmd, **kwargs)
        # Confirm the successful real capture first, then emulate FFmpeg 6.x's
        # additional showinfo output rather than mocking PNG data.
        assert result.returncode == 0 and result.stdout.startswith(PNG_HEADER)
        stderr = result.stderr + (
            b"\n[Parsed_showinfo_2 @ 0x1] n:   1 pts: 10240 "
            b"pts_time:0.8 fmt:yuv420p\n"
        )
        return SimpleNamespace(returncode=0, stdout=result.stdout, stderr=stderr)

    monkeypatch.setattr(capture.subprocess, "run", multi_showinfo)
    evidence = capture._ffmpeg_capture(shutil.which("ffmpeg"), str(video), .731, 0)
    assert evidence.png and evidence.method == "ffmpeg", evidence.error
    assert evidence.frame_time_sec == pytest.approx(.760)


def test_ffmpeg_missing_first_pts_fails_closed(video, monkeypatch):
    """An n:1-only log is insufficient to identify the PNG presentation time."""
    import report.impact_capture as capture
    from types import SimpleNamespace
    real_run = capture.subprocess.run

    def missing_first(cmd, **kwargs):
        result = real_run(cmd, **kwargs)
        return SimpleNamespace(
            returncode=result.returncode, stdout=result.stdout,
            stderr=b"[Parsed_showinfo_2 @ 0x1] n: 1 pts_time:0.8\n"
        )

    monkeypatch.setattr(capture.subprocess, "run", missing_first)
    evidence = capture._ffmpeg_capture(shutil.which("ffmpeg"), str(video), .731, 0)
    assert evidence.png is None and "시각" in evidence.error


def test_ffmpeg_ambiguous_duplicate_first_pts_fails_closed(video, monkeypatch):
    import report.impact_capture as capture
    from types import SimpleNamespace
    real_run = capture.subprocess.run

    def duplicate_first(cmd, **kwargs):
        result = real_run(cmd, **kwargs)
        first = next(line for line in result.stderr.splitlines()
                     if b"showinfo" in line and b"pts_time:" in line and b"n:" in line)
        return SimpleNamespace(returncode=result.returncode, stdout=result.stdout,
                               stderr=result.stderr + b"\n" + first + b"\n")

    monkeypatch.setattr(capture.subprocess, "run", duplicate_first)
    evidence = capture._ffmpeg_capture(shutil.which("ffmpeg"), str(video), .731, 0)
    assert evidence.png is None and "시각" in evidence.error
