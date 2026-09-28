"""사건 사본 영상의 지정 시점 PNG 프레임 추출.

시스템 ffmpeg가 있으면 사용하고, 없으면 프로젝트의 QtMultimedia를 이용한다.
프레임 추출 실패로 보고서 생성을 중단하지 않고 명시적 원인을 반환한다.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
from typing import Optional, Tuple

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def choose_capture_source(result) -> Tuple[str, int]:
    """기본은 주 영상, 후방만 보기면 별도 후방 영상 또는 내장 두 번째 트랙."""
    if getattr(result, "track_mode", "") == "rear":
        rear = getattr(result, "rear_copy_path", "") or ""
        if rear:
            return rear, 0
        return result.source_copy_path, 1
    return result.source_copy_path, 0


def _ffmpeg_capture(executable: str, path: str, seconds: float, track_index: int
                    ) -> Tuple[Optional[bytes], str]:
    cmd = [executable, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", f"{seconds:.6f}", "-i", path, "-map", f"0:v:{track_index}",
           "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"ffmpeg 실행 실패: {type(exc).__name__}"
    if result.returncode != 0 or not result.stdout.startswith(PNG_HEADER):
        message = result.stderr.decode("utf-8", "replace").strip()[:160]
        return None, f"ffmpeg 프레임 추출 실패: {message or '유효한 PNG가 반환되지 않음'}"
    return result.stdout, ""


def _qt_capture(path: str, seconds: float, track_index: int
                ) -> Tuple[Optional[bytes], str]:
    """Windows EXE에서 ffmpeg가 없을 때 사용하는 QtMultimedia 대체 경로."""
    try:
        from PySide6.QtCore import QBuffer, QCoreApplication, QEventLoop, QIODevice, QTimer, QUrl
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    except ImportError:
        return None, "QtMultimedia를 사용할 수 없습니다."
    if QCoreApplication.instance() is None:
        return None, "Qt 이벤트 루프가 없습니다."

    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoSink(sink)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    output: Optional[bytes] = None
    error = "요청 시점의 프레임을 얻지 못했습니다."
    state = {"started": False, "finished": False}
    target_us = int(seconds * 1_000_000)

    def finish(png=None, message=""):
        nonlocal output, error
        if state["finished"]:
            return
        state["finished"] = True
        output, error = png, message
        loop.quit()

    def ready(status):
        if state["started"] or status not in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            return
        tracks = player.videoTracks()
        if track_index >= len(tracks):
            finish(message="선택한 영상 트랙이 없습니다.")
            return
        player.setActiveVideoTrack(track_index)
        if seconds > 0 and not player.isSeekable():
            finish(message="영상 탐색이 지원되지 않습니다.")
            return
        state["started"] = True
        player.setPosition(round(seconds * 1000))
        player.play()

    def frame_received(frame):
        if not state["started"] or not frame.isValid():
            return
        ft = frame.startTime()
        if ft < 0 or ft < target_us - 200_000:
            return
        im = frame.toImage()
        if im.isNull():
            return
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if im.save(buffer, "PNG"):
            png = bytes(buffer.data())
            if png.startswith(PNG_HEADER):
                finish(png, "")

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
        player.setSource(QUrl())
    return output, error


def capture_frame_png(path: str, seconds: float, track_index: int = 0
                      ) -> Tuple[Optional[bytes], str]:
    if not path or not os.path.isfile(path):
        return None, "사건 영상 사본이 없습니다."
    if not math.isfinite(seconds) or seconds < 0 or track_index < 0:
        return None, "올바르지 않은 캡처 시각 또는 영상 트랙입니다."
    executable = shutil.which("ffmpeg")
    if executable:
        png, error = _ffmpeg_capture(executable, path, seconds, track_index)
        if png is not None:
            return png, ""
        qt_png, qt_error = _qt_capture(path, seconds, track_index)
        return qt_png, "" if qt_png is not None else f"{error}; Qt: {qt_error}"
    return _qt_capture(path, seconds, track_index)
