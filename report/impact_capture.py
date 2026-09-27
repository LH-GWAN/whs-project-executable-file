"""Asynchronous, timestamp-checked frame extraction using the existing Qt backend."""
from __future__ import annotations

import os
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

from core.impact import ImpactEvent, select_report_impacts
from core.video_tracks import TRACK_MODE_REAR


@dataclass
class ImpactCapture:
    event: ImpactEvent
    camera: str
    filename: str
    png: bytes = b""
    frame_time_sec: float | None = None
    error: str = ""


class ImpactCaptureJob(QObject):
    finished = Signal(object)
    progress = Signal(int, int)
    TIMEOUT_MS = 8000

    def __init__(self, result, events, parent=None):
        super().__init__(parent)
        source = result.source_copy_path
        mode = result.track_mode
        # One image per report, including dual-camera cases. Rear-only keeps
        # the selected rear track; otherwise use the primary video's first track.
        track, label = (1, "후방") if mode == TRACK_MODE_REAR else (0, "주 영상")
        self._jobs = [(e, source, track, label) for e in select_report_impacts(events)]
        self._captures = []
        self._index = 0
        self._player = None
        self._sink = None
        self._active = False
        self._cancelled = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._complete(error="영상 로드/탐색 시간 초과"))

    def start(self):
        QTimer.singleShot(0, self._next)

    def cancel(self):
        self._cancelled = True
        self._release()

    def _release(self):
        self._active = False
        self._timer.stop()
        if self._player is not None:
            self._player.stop()
            self._player.setVideoSink(None)
            self._player.deleteLater()
            self._player = None
        if self._sink is not None:
            self._sink.deleteLater()
            self._sink = None

    def _next(self):
        if self._cancelled:
            return
        if self._index >= len(self._jobs):
            self.finished.emit(self._captures)
            return
        event, path, track, label = self._jobs[self._index]
        self._capture = ImpactCapture(event, label, os.path.basename(path))
        self.progress.emit(self._index, len(self._jobs))
        self._active = True
        self._started = False
        if not os.path.isfile(path):
            self._complete(error="영상 파일을 찾을 수 없습니다")
            return
        self._player = QMediaPlayer(self)
        self._sink = QVideoSink(self)
        player = self._player
        self._player.setVideoSink(self._sink)
        self._player.mediaStatusChanged.connect(lambda status: self._loaded(player, status, track))
        self._player.errorOccurred.connect(lambda *_: self._error(player))
        self._sink.videoFrameChanged.connect(lambda frame: self._frame(player, frame))
        self._timer.start(self.TIMEOUT_MS)
        self._player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))

    def _error(self, player):
        if self._active and player is self._player:
            self._complete(error=player.errorString() or "영상 디코딩 실패")

    def _loaded(self, player, status, track):
        if not self._active or player is not self._player or self._started:
            return
        if status not in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            return
        self._started = True
        if len(player.videoTracks()) <= track:
            self._complete(error="요청한 비디오 트랙이 없습니다")
            return
        target = self._capture.event.time_sec
        if player.duration() > 0 and target * 1000 >= player.duration():
            self._complete(error="충격 시각이 영상 재생 범위를 벗어났습니다")
            return
        player.setActiveVideoTrack(track)
        player.setPosition(round(target * 1000))
        player.play()

    def _frame(self, player, frame):
        if not self._active or player is not self._player or not self._started or not frame.isValid():
            return
        start, end = frame.startTime(), frame.endTime()
        if start < 0:
            return  # Position alone cannot prove that this is the requested frame.
        target = self._capture.event.time_sec
        actual = start / 1_000_000
        contains = end > start and actual <= target < end / 1_000_000
        if not contains and not (0 <= actual - target <= 0.100):
            if actual > target + 0.100:
                self._complete(error="요청 시각과 일치하는 프레임을 얻지 못했습니다")
            return
        image = frame.toImage()
        if image.isNull():
            self._complete(error="프레임 이미지 변환 실패")
            return
        image = image.scaled(960, 540, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        ok = image.save(buffer, "PNG")
        buffer.close()
        if not ok:
            self._complete(error="PNG 인코딩 실패")
            return
        self._capture.png = bytes(data)
        self._capture.frame_time_sec = actual
        self._complete()

    def _complete(self, error=""):
        if not self._active:
            return
        self._capture.error = error
        self._captures.append(self._capture)
        self._release()
        self._index += 1
        QTimer.singleShot(0, self._next)
