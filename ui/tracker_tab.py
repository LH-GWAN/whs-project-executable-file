from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core import geocode
from core.acceleration import FlaggedSegment
from engine.engine_adapter import TrackPoint
from ui.address_resolver import AddressResolver
from ui.map_view import MapView

SKIP_MS = 5000
PLAYBACK_RATES = ((0.5, "0.5×"), (0.75, "0.75×"), (1.0, "1×"), (1.5, "1.5×"), (2.0, "2×"))
_SYNC_TOLERANCE_MS = 400


def _fmt_ms(ms: int) -> str:
    total = max(0, int(ms // 1000))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class TrackerTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 전방 재생기가 기준이다. 후방은 전방의 재생/정지/위치/배속을 따라간다.
        self._player = QMediaPlayer(self)
        self._video_widget = QVideoWidget(self)
        self._player.setVideoOutput(self._video_widget)
        self._rear_player = QMediaPlayer(self)
        self._rear_widget = QVideoWidget(self)
        self._rear_player.setVideoOutput(self._rear_widget)
        self._rear_widget.hide()
        self._rear_active = False

        self._play_btn = QPushButton("▶")
        self._play_btn.setFixedWidth(40)
        self._play_btn.setToolTip("재생 / 일시정지")
        self._play_btn.clicked.connect(self._toggle_play)
        self._back_btn = QPushButton("-5s")
        self._back_btn.setFixedWidth(48)
        self._back_btn.setToolTip("5초 뒤로")
        self._back_btn.clicked.connect(lambda: self._skip(-SKIP_MS))
        self._fwd_btn = QPushButton("+5s")
        self._fwd_btn.setFixedWidth(48)
        self._fwd_btn.setToolTip("5초 앞으로")
        self._fwd_btn.clicked.connect(lambda: self._skip(SKIP_MS))

        self._seek_slider = QSlider(Qt.Horizontal)
        self._seek_slider.sliderMoved.connect(self._on_slider_moved)
        self._time_label = QLabel("00:00 / 00:00")

        # 배속. 지도 마커는 재생기의 positionChanged를 따라가므로 느리게 틀면 같이 느려진다.
        self._rate_combo = QComboBox()
        for rate, label in PLAYBACK_RATES:
            self._rate_combo.addItem(label, rate)
        self._rate_combo.setCurrentIndex(2)
        self._rate_combo.setToolTip("재생 속도 (지도 위치도 같은 속도로 움직입니다)")
        self._rate_combo.currentIndexChanged.connect(self._on_rate_changed)

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._rear_player.mediaStatusChanged.connect(self._on_rear_media_status)
        # 영상을 올린 직후 검은 화면 대신 첫 장면이 보이게, 로드되면 잠깐 재생했다가
        # 바로 멈춰 0초로 되돌린다(재생기는 한 번 재생을 시작해야 프레임을 그린다).
        self._prime_pending = False
        self._priming = False
        self._rear_prime_pending = False
        # 파이프라인이 계산한 영상 길이. 재생기가 길이를 0으로 주는 경우(가끔 AVI에서
        # 첫 로드에 못 읽음)의 대비책이자, 두 값이 다르면 파이프라인 값을 믿는다.
        self._duration_hint_ms = 0

        controls = QHBoxLayout()
        controls.addWidget(self._play_btn)
        controls.addWidget(self._back_btn)
        controls.addWidget(self._fwd_btn)
        controls.addWidget(self._seek_slider, 1)
        controls.addWidget(self._time_label)
        controls.addWidget(self._rate_combo)

        # 재생 중인 지점의 속도/좌표를 영상 바로 아래에 보여준다. 위경도만으로는
        # 어디인지 바로 읽기 어려워서 주소도 함께 둔다. 주소는 외부 조회가 필요해
        # 온라인 모드에서만 채워지고, 조회는 워커(AddressResolver)가 맡는다.
        # GPS가 없는 순간(미기록·끊김·이상치)에는 값을 지우지 않고 마지막 정상값을 그대로
        # 두며, 오른쪽 상태 표시로만 알린다 - 1초마다 "미기록"으로 바뀌면 읽을 수가 없다.
        self._speed_label = QLabel("속도 -")
        self._speed_label.setStyleSheet("font-weight: 600;")
        self._coord_label = QLabel("위치 -")
        self._addr_label = QLabel("")
        self._addr_label.setStyleSheet("color: #666;")
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #b36b00; font-weight: 600;")

        info_row = QHBoxLayout()
        info_row.setContentsMargins(4, 2, 4, 2)
        info_row.addWidget(self._speed_label)
        info_row.addSpacing(14)
        info_row.addWidget(self._coord_label)
        info_row.addSpacing(10)
        info_row.addWidget(self._addr_label, 1)
        info_row.addWidget(self._status_label)

        # 전방/후방 영상: 후방이 있으면 영상 칸을 반으로 나눠 왼쪽 전방, 오른쪽 후방.
        self._video_split = QSplitter(Qt.Horizontal)
        self._video_split.addWidget(self._video_widget)
        self._video_split.addWidget(self._rear_widget)
        self._video_split.setSizes([1, 1])

        video_panel = QVBoxLayout()
        video_panel.setContentsMargins(0, 0, 0, 0)
        video_panel.addWidget(self._video_split, 1)
        video_panel.addLayout(controls)
        video_panel.addLayout(info_row)
        video_container = QWidget()
        video_container.setLayout(video_panel)

        self._points: List[TrackPoint] = []
        self._map = MapView()

        self._resolver = AddressResolver.instance()
        self._resolver.resolved.connect(self._on_address_resolved)
        self._addr_key = None  # 지금 화면에 보이는(또는 조회 중인) 지점의 캐시 키

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(video_container)
        splitter.addWidget(self._map)
        splitter.setSizes([520, 520])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(splitter)

    def map_view(self) -> MapView:
        return self._map

    # ---------- 영상 로드 ----------
    def load_video(self, path: str, rear_path: str = "") -> None:
        self._prime_pending = True
        self._rear_prime_pending = False
        self._set_rear_active(False)
        self._rear_player.setSource(QUrl())
        self._player.setSource(QUrl.fromLocalFile(path))
        if rear_path:
            self._rear_prime_pending = True
            self._rear_player.setSource(QUrl.fromLocalFile(rear_path))
            self._set_rear_active(True)

    def set_duration_hint(self, duration_sec: Optional[float]) -> None:
        self._duration_hint_ms = int(duration_sec * 1000) if duration_sec else 0
        if self._player.duration() <= 0 and self._duration_hint_ms > 0:
            self._on_duration_changed(self._duration_hint_ms)

    def has_rear_video(self) -> bool:
        return self._rear_active

    def _set_rear_active(self, active: bool) -> None:
        self._rear_active = active
        self._rear_widget.setVisible(active)
        if active:
            self._video_split.setSizes([1, 1])

    def _on_media_status(self, status) -> None:
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            # 재생기가 길이를 늦게/0으로 주는 경우 대비 - 로드된 시점에 한 번 더 맞춘다.
            duration = self._player.duration()
            if duration <= 0 and self._duration_hint_ms > 0:
                duration = self._duration_hint_ms
            if duration > 0:
                self._on_duration_changed(duration)
            # 파일 하나에 전방·후방 트랙이 같이 든 경우: 두 번째 비디오 트랙을 후방으로 띄운다.
            if not self._rear_active and self._rear_player.source().isEmpty():
                try:
                    tracks = self._player.videoTracks()
                except Exception:  # noqa: BLE001
                    tracks = []
                if len(tracks) >= 2:
                    self._rear_prime_pending = True
                    self._rear_player.setSource(self._player.source())
                    self._rear_player.setActiveVideoTrack(1)
                    self._set_rear_active(True)
        if not self._prime_pending:
            return
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._prime_pending = False
            self._priming = True
            self._player.play()
            QTimer.singleShot(150, self._finish_prime)

    def _on_rear_media_status(self, status) -> None:
        if not self._rear_prime_pending:
            return
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._rear_prime_pending = False
            self._rear_player.play()
            QTimer.singleShot(150, self._finish_rear_prime)

    def _finish_prime(self) -> None:
        if not self._priming:
            return
        self._player.pause()
        self._player.setPosition(0)
        self._priming = False
        self._play_btn.setText("▶")
        self._update_info(0.0)

    def _finish_rear_prime(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlayingState and not self._priming:
            return  # 전방이 이미 재생 중이면 그대로 따라가게 둔다
        self._rear_player.pause()
        self._rear_player.setPosition(self._player.position())

    # ---------- 궤적/정보 ----------
    def load_track(self, points: List[TrackPoint],
                    segments: Optional[List[FlaggedSegment]] = None) -> None:
        self._points = points
        self._map.set_track(points, segments)
        self._addr_key = None
        self._last_valid: Optional[TrackPoint] = None
        self._update_info(0.0)

    def _point_at(self, seconds: float) -> Optional[TrackPoint]:
        found = None
        for p in self._points:
            if p.start_time_sec is None:
                continue
            if p.start_time_sec <= seconds:
                found = p
            else:
                break
        return found

    def _update_info(self, seconds: float) -> None:
        point = self._point_at(seconds)
        if point is None:
            self._speed_label.setText("속도 -")
            self._coord_label.setText("위치 -")
            self._addr_label.setText("")
            self._status_label.setText("")
            self._addr_key = None
            return

        if point.is_outlier:
            status = "(이상치)"
        elif point.has_fix:
            status = ""
        elif point.is_dropout:
            status = "(GPS 끊김)"
        else:
            status = "(GPS 미기록)"
        self._status_label.setText(status)
        self._status_label.setToolTip(
            "이 시점의 GPS 기록이 없거나 이상치라 마지막 정상 측정값을 그대로 표시합니다." if status else "")

        # 정상 측정이면 값을 갱신하고, 아니면 마지막 정상값을 그대로 둔다.
        shown = point if point.has_fix else getattr(self, "_last_valid", None)
        if point.has_fix:
            self._last_valid = point
        if shown is None:
            if point.speed_kmh is not None and not point.is_outlier:
                self._speed_label.setText(f"속도 {point.speed_kmh:.1f} km/h")
            else:
                self._speed_label.setText("속도 -")
            self._coord_label.setText("위치 -")
            self._addr_label.setText("")
            self._addr_key = None
            return

        speed = shown.speed_kmh if shown.speed_kmh is not None else point.speed_kmh
        self._speed_label.setText(f"속도 {speed:.1f} km/h" if speed is not None else "속도 -")
        self._coord_label.setText(f"위치 {shown.latitude:.6f}, {shown.longitude:.6f}")
        self._show_address(shown.latitude, shown.longitude)

    def _show_address(self, lat: float, lon: float) -> None:
        if not geocode.is_available():
            self._addr_label.setText("")
            self._addr_key = None
            return
        key = geocode.cache_key(lat, lon)
        if key == self._addr_key:
            return
        self._addr_key = key
        cached = geocode.cached_address(lat, lon)
        if cached is not None:
            self._addr_label.setText(f"({cached})" if cached else "")
            return
        # 조회가 끝날 때까지 직전 주소를 그대로 둔다. 매초 비웠다 채우면 깜빡인다.
        self._resolver.request(lat, lon)

    def _on_address_resolved(self, lat: float, lon: float, address: str) -> None:
        if geocode.cache_key(lat, lon) == self._addr_key:
            self._addr_label.setText(f"({address})" if address else "")

    # ---------- 재생 조작 ----------
    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
            if self._rear_active:
                self._rear_player.pause()
        else:
            self._player.play()
            if self._rear_active:
                self._rear_player.setPosition(self._player.position())
                self._rear_player.play()

    def _skip(self, delta_ms: int) -> None:
        duration = self._player.duration() or self._duration_hint_ms
        target = self._player.position() + delta_ms
        target = max(0, min(target, duration) if duration > 0 else max(0, target))
        self._player.setPosition(target)
        if self._rear_active:
            self._rear_player.setPosition(target)
        self._map.set_playback_time(target / 1000.0)
        self._update_info(target / 1000.0)

    def _on_rate_changed(self, _index: int) -> None:
        rate = float(self._rate_combo.currentData())
        self._player.setPlaybackRate(rate)
        self._rear_player.setPlaybackRate(rate)

    def _on_state_changed(self, state) -> None:
        if self._priming:
            return  # 첫 장면을 띄우려는 내부 재생은 버튼에 반영하지 않는다
        self._play_btn.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")
        if self._rear_active and not self._rear_prime_pending:
            if state == QMediaPlayer.PlayingState:
                if self._rear_player.playbackState() != QMediaPlayer.PlayingState:
                    self._rear_player.setPosition(self._player.position())
                    self._rear_player.play()
            elif self._rear_player.playbackState() == QMediaPlayer.PlayingState:
                self._rear_player.pause()

    def _on_slider_moved(self, position: int) -> None:
        self._player.setPosition(position)
        if self._rear_active:
            self._rear_player.setPosition(position)
        self._map.set_playback_time(position / 1000.0)
        self._update_info(position / 1000.0)

    def _on_duration_changed(self, duration: int) -> None:
        if duration <= 0 and self._duration_hint_ms > 0:
            duration = self._duration_hint_ms
        self._seek_slider.setRange(0, max(0, duration))
        self._time_label.setText(f"{_fmt_ms(self._player.position())} / {_fmt_ms(duration)}")

    def _on_position_changed(self, position: int) -> None:
        if not self._seek_slider.isSliderDown():
            self._seek_slider.setValue(position)
        duration = self._player.duration() or self._duration_hint_ms
        self._time_label.setText(f"{_fmt_ms(position)} / {_fmt_ms(duration)}")
        self._map.set_playback_time(position / 1000.0)
        self._update_info(position / 1000.0)
        # 후방 영상이 전방과 어긋나면 맞춘다(디코더 차이로 조금씩 밀린다).
        if self._rear_active and not self._priming and not self._rear_prime_pending:
            if abs(self._rear_player.position() - position) > _SYNC_TOLERANCE_MS:
                self._rear_player.setPosition(position)

    def grab_map_png(self):
        return self._map.grab_png()

    def ensure_map_loaded(self) -> None:
        self._map.ensure_loaded()

    def stop(self) -> None:
        self._prime_pending = False
        self._priming = False
        self._rear_prime_pending = False
        self._player.stop()
        self._rear_player.stop()

    def release_media(self) -> None:
        """영상 파일 잠금을 푼다. 사건 폴더를 지우기 전에 부른다 - Windows는 재생기가
        열어 둔 파일이 있으면 폴더 삭제가 실패한다."""
        self.stop()
        self._player.setSource(QUrl())
        self._rear_player.setSource(QUrl())
        self._set_rear_active(False)
