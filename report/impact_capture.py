"""Verified video-frame evidence from the incident's preserved video copy.

A frame is not labelled as an exact impact frame unless the decoder supplies a
presentation timestamp. For FFmpeg, decode from the beginning using video-track
relative PTS, select the first frame at/after the requested video-relative time,
and check the PTS reported by showinfo. QVideoFrame timestamps are checked too.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Tuple

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
MAX_FRAME_OFFSET_SEC = 0.120
FFMPEG_TIMEOUT_SEC = 45
_SHOWINFO_PTS = re.compile(r"\bpts_time:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)")


@dataclass(frozen=True)
class FrameEvidence:
    png: Optional[bytes]
    requested_time_sec: float
    frame_time_sec: Optional[float] = None
    method: str = ""
    error: str = ""

    @property
    def offset_sec(self) -> Optional[float]:
        return None if self.frame_time_sec is None else self.frame_time_sec - self.requested_time_sec


def choose_capture_source(result) -> Tuple[str, int]:
    """Rear-only uses a separate rear copy, or the second track in a dual-track file."""
    if getattr(result, "track_mode", "") == "rear":
        rear = getattr(result, "rear_copy_path", "") or ""
        if rear:
            return rear, 0
        return result.source_copy_path, 1
    return result.source_copy_path, 0


def capture_source_for_event(result, event) -> Tuple[str, int, float]:
    """Composed event time -> preserved segment file and local decoder time."""
    segments = getattr(result, "segments", None) or []
    if not segments:
        path, track = choose_capture_source(result)
        return path, track, event.time_sec
    segment = next((s for s in segments if s.index == event.segment_index), None)
    if segment is None:
        raise ValueError("감지 지점의 영상 구간이 없습니다.")
    local_time = event.time_sec - segment.offset_sec
    if local_time < 0 or (segment.duration_sec is not None and local_time >= segment.duration_sec):
        raise ValueError("감지 시각이 영상 구간 범위를 벗어납니다.")
    if segment.track_mode == "rear":
        if segment.rear_copy_path:
            return segment.rear_copy_path, 0, local_time
        return segment.primary_copy_path, 1, local_time
    return segment.primary_copy_path, 0, local_time


def _verified(png: Optional[bytes], requested: float, actual: Optional[float],
              method: str, error: str = "", allow_preceding: bool = False) -> FrameEvidence:
    if png is None:
        return FrameEvidence(None, requested, None, method, error or "프레임을 추출하지 못했습니다.")
    if not png.startswith(PNG_HEADER):
        return FrameEvidence(None, requested, None, method, "PNG 헤더가 유효하지 않습니다.")
    if actual is None or not math.isfinite(actual):
        return FrameEvidence(None, requested, None, method, "실제 프레임 시각을 확인하지 못했습니다.")
    delta = actual - requested
    if delta < (-MAX_FRAME_OFFSET_SEC if allow_preceding else -0.002) or delta > MAX_FRAME_OFFSET_SEC:
        return FrameEvidence(None, requested, actual, method,
                             f"프레임 시각 불일치: 요청 {requested:.3f}초 / 실제 {actual:.3f}초")
    return FrameEvidence(png, requested, actual, method)


def _ffmpeg_capture(executable: str, path: str, seconds: float, track_index: int) -> FrameEvidence:
    # Avoid fast input seeking: it can return a keyframe near the requested time.
    # setpts establishes a clear track-relative timeline. showinfo is deliberately
    # AFTER the select filter so it reports the very frame that is encoded.
    video_filter = f"setpts=PTS-STARTPTS,select='gte(t,{seconds:.6f})',showinfo"
    cmd = [executable, "-nostdin", "-hide_banner", "-loglevel", "info", "-nostats",
           "-i", path, "-map", f"0:v:{track_index}", "-an", "-sn", "-dn",
           "-vf", video_filter, "-fps_mode", "vfr", "-frames:v", "1",
           "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SEC, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return FrameEvidence(None, seconds, method="ffmpeg", error=f"ffmpeg 실행 실패: {type(exc).__name__}")
    stderr = run.stderr.decode("utf-8", "replace")
    if run.returncode != 0 or not run.stdout.startswith(PNG_HEADER):
        last = [ln.strip() for ln in stderr.splitlines() if "Error" in ln or "error" in ln or "matches no" in ln]
        message = (last[-1] if last else stderr.strip().splitlines()[-1] if stderr.strip() else "PNG 결과 없음")
        return FrameEvidence(None, seconds, method="ffmpeg", error=f"ffmpeg 프레임 추출 실패: {message[:180]}")
    # FFmpeg 6.x may log additional showinfo frames when encoder buffers drain
    # despite -frames:v 1. The sole output PNG is the FIRST selected frame.
    # Match n:0 instead of incorrectly requiring only one showinfo line.
    # Missing or ambiguous first-frame PTS fails closed (no fabricated time).
    first_pts = []
    for line in stderr.splitlines():
        if "showinfo" not in line or "pts_time:" not in line:
            continue
        if not re.search(r"\bn:\s*0\b", line):
            continue
        match = _SHOWINFO_PTS.search(line)
        if match:
            first_pts.append(float(match.group(1)))
    actual = first_pts[0] if len(first_pts) == 1 else None
    return _verified(run.stdout, seconds, actual, "ffmpeg")


def _qt_capture(path: str, seconds: float, track_index: int) -> FrameEvidence:
    """Qt decoder fallback. Never accept a stale pre-seek or un-timestamped frame."""
    try:
        from PySide6.QtCore import QBuffer, QCoreApplication, QEventLoop, QIODevice, QTimer, QUrl
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    except ImportError:
        return FrameEvidence(None, seconds, method="Qt", error="QtMultimedia를 사용할 수 없습니다.")
    if QCoreApplication.instance() is None:
        return FrameEvidence(None, seconds, method="Qt", error="Qt 이벤트 루프가 없습니다.")

    player, sink, loop, timer = QMediaPlayer(), QVideoSink(), QEventLoop(), QTimer()
    player.setVideoSink(sink)
    timer.setSingleShot(True)
    result = FrameEvidence(None, seconds, method="Qt", error="요청 시점의 프레임을 얻지 못했습니다.")
    state = {"started": False, "finished": False}
    target_us = round(seconds * 1_000_000)

    def finish(png=None, stamp=None, message="", allow_preceding=False):
        nonlocal result
        if state["finished"]:
            return
        state["finished"] = True
        result = _verified(png, seconds, stamp, "Qt", message, allow_preceding)
        loop.quit()

    def ready(status):
        if state["started"] or state["finished"] or status not in (
                QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            return
        if track_index >= len(player.videoTracks()):
            finish(message="선택한 영상 트랙이 없습니다.")
            return
        if seconds > 0 and not player.isSeekable():
            finish(message="영상 탐색이 지원되지 않습니다.")
            return
        player.setActiveVideoTrack(track_index)
        state["started"] = True
        player.setPosition(round(seconds * 1000))
        player.play()

    def frame_received(frame):
        if not state["started"] or state["finished"] or not frame.isValid():
            return
        start_us, end_us = frame.startTime(), frame.endTime()
        if start_us < 0:
            return  # Unknown time must not be passed off as an exact event frame.
        if start_us < target_us:
            # This frame represents the event only if it contains the requested instant.
            if end_us <= target_us:
                return  # stale frame emitted around seek
            # If it contains the event, its PTS may precede the event by one frame.
            if target_us - start_us > MAX_FRAME_OFFSET_SEC * 1_000_000:
                finish(message="프레임 간격이 커서 감지 시각에 근접한 화면을 확인할 수 없습니다.")
                return
        else:
            if start_us - target_us > MAX_FRAME_OFFSET_SEC * 1_000_000:
                finish(message="탐지 시각 이후 프레임이 허용 오차를 초과합니다.")
                return
        im = frame.toImage()
        if im.isNull():
            finish(message="프레임 이미지 변환 실패")
            return
        data = QBuffer()
        if not data.open(QIODevice.WriteOnly) or not im.save(data, "PNG"):
            finish(message="PNG 인코딩 실패")
            return
        # A frame that spans the target can precede it; retain/disclose its PTS.
        finish(bytes(data.data()), start_us / 1_000_000,
               allow_preceding=(start_us < target_us))

    player.mediaStatusChanged.connect(ready)
    sink.videoFrameChanged.connect(frame_received)
    player.errorOccurred.connect(lambda _code, message: finish(message=message or "미디어 오류"))
    timer.timeout.connect(lambda: finish(message="Qt 영상 프레임 대기 시간 초과"))
    try:
        timer.start(10000)
        player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
        if not state["finished"]:
            loop.exec()
    finally:
        timer.stop()
        player.stop()
        player.setSource(QUrl())
        player.setVideoSink(None)
    return result


def capture_frame_evidence(path: str, seconds: float, track_index: int = 0) -> FrameEvidence:
    if not path or not os.path.isfile(path):
        return FrameEvidence(None, seconds, error="사건 영상 사본이 없습니다.")
    if (not isinstance(seconds, (float, int)) or not math.isfinite(seconds)
            or seconds < 0 or not isinstance(track_index, int) or track_index < 0):
        return FrameEvidence(None, seconds, error="올바르지 않은 캡처 시각 또는 영상 트랙입니다.")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        result = _ffmpeg_capture(ffmpeg, path, seconds, track_index)
        if result.png is not None:
            return result
        qt = _qt_capture(path, seconds, track_index)
        if qt.png is not None:
            return qt
        return FrameEvidence(None, seconds, frame_time_sec=result.frame_time_sec,
                             method="ffmpeg/Qt", error=f"{result.error}; Qt: {qt.error}")
    return _qt_capture(path, seconds, track_index)


def capture_frame_png(path: str, seconds: float, track_index: int = 0) -> Tuple[Optional[bytes], str]:
    """Backward-compatible wrapper for existing callers and their tests."""
    result = capture_frame_evidence(path, seconds, track_index)
    return result.png, result.error
