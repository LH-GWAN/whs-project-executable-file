from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
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


def _fmt_ms(ms: int) -> str:
    total = max(0, int(ms // 1000))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class TrackerTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._video_widget = QVideoWidget(self)
        self._player.setVideoOutput(self._video_widget)

        self._play_btn = QPushButton("▶")
        self._play_btn.setFixedWidth(40)
        self._play_btn.clicked.connect(self._toggle_play)

        self._seek_slider = QSlider(Qt.Horizontal)
        self._seek_slider.sliderMoved.connect(self._on_slider_moved)
        self._time_label = QLabel("00:00 / 00:00")

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_state_changed)

        controls = QHBoxLayout()
        controls.addWidget(self._play_btn)
        controls.addWidget(self._seek_slider, 1)
        controls.addWidget(self._time_label)

        # 재생 중인 지점의 속도/좌표를 영상 바로 아래에 보여준다. 위경도만으로는
        # 어디인지 바로 읽기 어려워서 주소도 함께 둔다. 주소는 외부 조회가 필요해
        # 온라인 모드에서만 채워지고, 조회는 워커(AddressResolver)가 맡는다.
        self._speed_label = QLabel("속도 -")
        self._speed_label.setStyleSheet("font-weight: 600;")
        self._coord_label = QLabel("위치 -")
        self._addr_label = QLabel("")
        self._addr_label.setStyleSheet("color: #666;")

        info_row = QHBoxLayout()
        info_row.setContentsMargins(4, 2, 4, 2)
        info_row.addWidget(self._speed_label)
        info_row.addSpacing(14)
        info_row.addWidget(self._coord_label)
        info_row.addSpacing(10)
        info_row.addWidget(self._addr_label, 1)

        video_panel = QVBoxLayout()
        video_panel.setContentsMargins(0, 0, 0, 0)
        video_panel.addWidget(self._video_widget, 1)
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

    def load_video(self, path: str) -> None:
        self._player.setSource(QUrl.fromLocalFile(path))

    def load_track(self, points: List[TrackPoint],
                    segments: Optional[List[FlaggedSegment]] = None) -> None:
        self._points = points
        self._map.set_track(points, segments)
        self._addr_key = None
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
            self._addr_key = None
            return

        self._speed_label.setText(
            f"속도 {point.speed_kmh:.1f} km/h" if point.speed_kmh is not None else "속도 -")

        if point.has_fix:
            self._coord_label.setText(f"위치 {point.latitude:.6f}, {point.longitude:.6f}")
            self._show_address(point.latitude, point.longitude)
        else:
            self._coord_label.setText("위치 (GPS 끊김)" if point.is_dropout else "위치 (GPS 미기록)")
            self._addr_label.setText("")
            self._addr_key = None

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

    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_state_changed(self, state) -> None:
        self._play_btn.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _on_slider_moved(self, position: int) -> None:
        self._player.setPosition(position)
        self._map.set_playback_time(position / 1000.0)
        self._update_info(position / 1000.0)

    def _on_duration_changed(self, duration: int) -> None:
        self._seek_slider.setRange(0, max(0, duration))
        self._time_label.setText(f"{_fmt_ms(self._player.position())} / {_fmt_ms(duration)}")

    def _on_position_changed(self, position: int) -> None:
        if not self._seek_slider.isSliderDown():
            self._seek_slider.setValue(position)
        self._time_label.setText(f"{_fmt_ms(position)} / {_fmt_ms(self._player.duration())}")
        self._map.set_playback_time(position / 1000.0)
        self._update_info(position / 1000.0)

    def grab_map_png(self):
        return self._map.grab_png()

    def ensure_map_loaded(self) -> None:
        self._map.ensure_loaded()

    def stop(self) -> None:
        self._player.stop()
