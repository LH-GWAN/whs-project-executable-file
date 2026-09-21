from __future__ import annotations

import bisect
import statistics
from typing import List, Optional, Tuple

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core import geocode, gpstime
from core.video_tracks import TRACK_MODE_BOTH, TRACK_MODE_FRONT, TRACK_MODE_REAR
from core.acceleration import FlaggedSegment
from engine.engine_adapter import TrackPoint
from ui.address_resolver import AddressResolver
from ui.map_view import MapView

SKIP_MS = 5000
PLAYBACK_RATES = ((0.5, "0.5×"), (0.75, "0.75×"), (1.0, "1×"), (1.5, "1.5×"), (2.0, "2×"))
_SYNC_TOLERANCE_MS = 400
# 전방/후방 중 하나를 눌러 키웠을 때의 폭 비율(누른 쪽 : 다른 쪽)
ENLARGED_RATIO = (3, 1)
FRONT, REAR = 0, 1

# GPS 기록 주기를 데이터에서 못 읽을 때(기록이 하나뿐 등) 쓰는 값. 블랙박스 GPS는 거의 1 Hz다.
DEFAULT_GPS_INTERVAL_SEC = 1.0


def _gps_key(p: TrackPoint):
    return (p.gps_date or "", p.gps_utc_time or "")


def gps_record_rows(points: List[TrackPoint]) -> List[Tuple[float, TrackPoint]]:
    """(시각, 행) - GPS 기록(시각 필드)이 있는 행만, 시각순. G센서 전용 행은 빠진다."""
    rows = [(p.start_time_sec, p) for p in points
            if p.start_time_sec is not None and p.has_gps_record]
    rows.sort(key=lambda r: r[0])
    return rows


def estimate_gps_interval(rows: List[Tuple[float, TrackPoint]]) -> float:
    """GPS 기록 주기(초). 같은 기록이 여러 행에 반복된 기기(VUGERA는 초당 31행)를 감안해
    기록 값이 바뀌는 시점 사이의 간격 중앙값을 쓴다."""
    change_times: List[float] = []
    prev_key = None
    for t, p in rows:
        key = _gps_key(p)
        if key != prev_key:
            change_times.append(t)
            prev_key = key
    gaps = [b - a for a, b in zip(change_times, change_times[1:]) if b - a > 0]
    if not gaps:
        return DEFAULT_GPS_INTERVAL_SEC
    return min(10.0, max(0.1, statistics.median(gaps)))


class _ElideLabel(QLabel):
    """칸보다 긴 글은 끝을 …로 줄여 그린다. 주소처럼 길이가 들쭉날쭉한 글이 레이아웃 폭을
    밀지 못하게 하려고 쓴다(QLabel은 기본으로 줄이지 않고 그냥 잘린다)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        self._refresh()

    def fullText(self) -> str:  # noqa: N802
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        width = max(0, self.width() - 4)
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, width))


class _VideoPane(QWidget):
    """영상 하나 + 위쪽의 작은 이름표("전방"/"후방"). 어디를 눌러도 clicked를 낸다.

    Qt6의 QVideoWidget은 안에 영상 창을 두되 입력을 투과시키므로(WindowTransparentForInput)
    마우스 이벤트는 QVideoWidget 자체로 온다 - 그걸 걸러서 클릭으로 쓴다."""

    clicked = Signal()

    def __init__(self, video_widget: QVideoWidget, parent=None):
        super().__init__(parent)
        self._caption = QLabel("")
        self._caption.setAlignment(Qt.AlignCenter)
        self._caption.setStyleSheet("color: #8a8a8a; font-size: 11px;")
        self._caption.setFixedHeight(16)
        self._caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._caption.hide()
        self._video = video_widget
        self._video.setParent(self)
        self._video.installEventFilter(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        layout.addWidget(self._caption)
        layout.addWidget(self._video, 1)
        self.setCursor(Qt.PointingHandCursor)

    def set_caption(self, text: str, visible: bool) -> None:
        self._caption.setText(text)
        self._caption.setVisible(visible)
        self._caption.setToolTip("클릭하면 이 영상을 크게 봅니다. 다시 누르면 원래대로." if visible else "")

    def caption_text(self) -> str:
        return self._caption.text()

    def caption_visible(self) -> bool:
        return not self._caption.isHidden()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._video and event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self.clicked.emit()
            return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


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
        self._rear_active = False
        # 전방/후방 중 눌러서 키운 쪽(FRONT/REAR). None이면 반반.
        self._enlarged: Optional[int] = None
        self._front_pane = _VideoPane(self._video_widget)
        self._rear_pane = _VideoPane(self._rear_widget)
        self._rear_pane.hide()
        self._front_pane.clicked.connect(lambda: self._on_pane_clicked(FRONT))
        self._rear_pane.clicked.connect(lambda: self._on_pane_clicked(REAR))

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
        # 후방 재생기가 틀 비디오 트랙(0=기본). 파일 하나에 두 트랙이 든 경우 1.
        self._rear_track_index = 0
        self._track_mode = TRACK_MODE_BOTH
        # 마지막으로 load_video가 지정한 소스. setSource()는 이전 미디어를 멈추면서 그 미디어의
        # 상태 변화(LoadedMedia 등)를 소스가 바뀌기 전에 동기적으로 내보낸다(실측). 그 낡은
        # 이벤트를 새 파일 것으로 알고 처리하면 첫 장면 띄우기가 건너뛰어지고, 2트랙 파일 다음에
        # 다른 파일을 열면 이전 파일의 트랙 수(2)를 보고 후방 칸을 다시 켰다. 그래서 상태 처리는
        # 재생기의 현재 소스가 지정한 소스와 같을 때만 한다.
        self._front_url = QUrl()
        self._rear_url = QUrl()
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

        # 영상 위: 재생 지점의 GPS 기록 시각. GPS는 UTC로 찍히므로 한국 시간으로 옮기고
        # "(UTC+9)"를 붙인다. GPS가 없는 순간에는 마지막 시각을 그대로 둔다.
        self._clock_label = QLabel("날짜·시간 -")
        clock_font = self._clock_label.font()
        clock_font.setBold(True)
        self._clock_label.setFont(clock_font)
        self._clock_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._clock_label.setFixedHeight(self._clock_label.fontMetrics().height() + 6)
        self._clock_label.setContentsMargins(4, 0, 4, 0)
        self._clock_label.setToolTip("현재 재생 지점의 GPS 기록 시각. 한국 시간(UTC+9)으로 표시합니다.")
        self._last_clock = ""

        # 재생 중인 지점의 속도/좌표를 영상 바로 아래에 보여준다. 위경도만으로는
        # 어디인지 바로 읽기 어려워서 주소도 함께 둔다. 주소는 외부 조회가 필요해
        # 온라인 모드에서만 채워지고, 조회는 워커(AddressResolver)가 맡는다.
        # GPS가 없는 순간(미기록·끊김·이상치)에는 값을 지우지 않고 마지막 정상값을 그대로
        # 두며, 오른쪽 상태 표시로만 알린다 - 1초마다 "미기록"으로 바뀌면 읽을 수가 없다.
        #
        # 이 줄의 크기는 내용과 무관하게 고정한다. 처음엔 "(GPS 미기록)"이 나타날 때마다
        # 줄이 넓어져 영상 칸이 커졌다 작아졌다 했고, 그 바람에 옆의 지도 컨테이너 크기가
        # 바뀌어 지도 확대까지 풀렸다(검토 제보). 각 칸은 가장 긴 문구 폭으로 잡아 두고,
        # 줄 전체는 폭을 레이아웃에 요구하지 않는다(Ignored).
        self._speed_label = QLabel("속도 -")
        speed_font = self._speed_label.font()
        speed_font.setBold(True)
        self._speed_label.setFont(speed_font)
        self._coord_label = QLabel("위치 -")
        self._addr_label = _ElideLabel()
        self._addr_label.setStyleSheet("color: #666;")
        self._addr_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._status_label = QLabel("")
        status_font = self._status_label.font()
        status_font.setBold(True)
        self._status_label.setFont(status_font)
        self._status_label.setStyleSheet("color: #b36b00;")
        self._status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._speed_label.setFixedWidth(self._speed_label.fontMetrics().horizontalAdvance("속도 000.0 km/h") + 8)
        self._coord_label.setFixedWidth(self._coord_label.fontMetrics().horizontalAdvance("위치 -00.000000, -000.000000") + 8)
        self._status_label.setFixedWidth(self._status_label.fontMetrics().horizontalAdvance("(GPS 미기록)") + 12)

        info_row = QHBoxLayout()
        info_row.setContentsMargins(4, 2, 4, 2)
        info_row.addWidget(self._speed_label)
        info_row.addSpacing(14)
        info_row.addWidget(self._coord_label)
        info_row.addSpacing(10)
        info_row.addWidget(self._addr_label, 1)
        info_row.addWidget(self._status_label)
        self._info_bar = QWidget()
        self._info_bar.setLayout(info_row)
        self._info_bar.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._info_bar.setFixedHeight(self._speed_label.fontMetrics().height() + 10)

        # 전방/후방 영상: 후방이 있으면 영상 칸을 반으로 나눠 왼쪽 전방, 오른쪽 후방.
        # 한쪽을 누르면 그쪽이 3:1로 커지고, 다시 누르면 반반으로 돌아온다.
        self._video_split = QSplitter(Qt.Horizontal)
        self._video_split.addWidget(self._front_pane)
        self._video_split.addWidget(self._rear_pane)
        self._video_split.setChildrenCollapsible(False)
        self._video_split.setSizes([1, 1])

        video_panel = QVBoxLayout()
        video_panel.setContentsMargins(0, 0, 0, 0)
        video_panel.setSpacing(2)
        video_panel.addWidget(self._clock_label)
        video_panel.addWidget(self._video_split, 1)
        video_panel.addLayout(controls)
        video_panel.addWidget(self._info_bar)
        video_container = QWidget()
        video_container.setLayout(video_panel)
        self._video_container = video_container

        self._points: List[TrackPoint] = []
        self._gps_rows: List[Tuple[float, TrackPoint]] = []
        self._gps_times: List[float] = []
        self._gps_interval = DEFAULT_GPS_INTERVAL_SEC
        self._gps_t0: Optional[float] = None
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
    def load_video(self, path: str, rear_path: str = "", track_mode: str = "") -> None:
        """track_mode는 파일 하나에 전·후방 트랙이 든 영상의 보기 방식(both/front/rear). 빈 문자열이면
        both와 같다(트랙이 둘이면 같이 보여 준다)."""
        self._prime_pending = True
        self._rear_prime_pending = False
        self._rear_track_index = 0
        self._track_mode = track_mode or TRACK_MODE_BOTH
        self._set_rear_active(False)
        self._front_url = QUrl.fromLocalFile(path)
        self._rear_url = QUrl.fromLocalFile(rear_path) if rear_path else QUrl()
        self._rear_player.setSource(QUrl())
        if self._player.source() == self._front_url:
            # 같은 파일을 다시 열면 setSource가 아무것도 안 해서(LoadedMedia가 다시 안 온다) 첫 장면
            # 띄우기와 트랙 보기 방식이 적용되지 않는다. 비웠다가 다시 지정해 새로 로드시킨다.
            self._player.setSource(QUrl())
        self._player.setSource(self._front_url)
        if rear_path:
            self._rear_prime_pending = True
            self._rear_player.setSource(self._rear_url)
            self._set_rear_active(True)

    def set_duration_hint(self, duration_sec: Optional[float]) -> None:
        self._duration_hint_ms = int(duration_sec * 1000) if duration_sec else 0
        if self._player.duration() <= 0 and self._duration_hint_ms > 0:
            self._on_duration_changed(self._duration_hint_ms)

    def has_rear_video(self) -> bool:
        return self._rear_active

    def _set_rear_active(self, active: bool) -> None:
        self._rear_active = active
        self._rear_pane.setVisible(active)
        self._front_pane.set_caption("전방", active)
        self._rear_pane.set_caption("후방", active)
        self._enlarged = None
        self._apply_split_sizes()

    def enlarged_pane(self) -> Optional[int]:
        """눌러서 키운 쪽(FRONT/REAR), 반반이면 None."""
        return self._enlarged

    def _on_pane_clicked(self, which: int) -> None:
        if not self._rear_active:
            return  # 영상이 하나뿐이면 키울 상대가 없다
        self._enlarged = None if self._enlarged == which else which
        self._apply_split_sizes()

    def _apply_split_sizes(self) -> None:
        total = sum(self._video_split.sizes()) or max(self._video_split.width(), 200)
        if self._enlarged is None:
            weights = (1, 1)
        elif self._enlarged == FRONT:
            weights = ENLARGED_RATIO
        else:
            weights = tuple(reversed(ENLARGED_RATIO))
        self._video_split.setStretchFactor(0, weights[0])
        self._video_split.setStretchFactor(1, weights[1])
        if self._rear_active:
            unit = total / float(sum(weights))
            self._video_split.setSizes([int(unit * weights[0]), int(unit * weights[1])])

    def _on_media_status(self, status) -> None:
        if self._front_url.isEmpty() or self._player.source() != self._front_url:
            return  # 이전 미디어의 낡은 상태 이벤트
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            # 재생기가 길이를 늦게/0으로 주는 경우 대비 - 로드된 시점에 한 번 더 맞춘다.
            duration = self._player.duration()
            if duration <= 0 and self._duration_hint_ms > 0:
                duration = self._duration_hint_ms
            if duration > 0:
                self._on_duration_changed(duration)
            # 파일 하나에 전방·후방 트랙이 같이 든 경우(랜드로버 순정 블랙박스 등): 두 번째
            # 비디오 트랙을 후방으로 띄운다. 트랙 전환은 후방 재생기가 파일을 다 읽은 뒤에 한다 -
            # 로드 전에 setActiveVideoTrack을 부르면 무시돼서 후방 칸도 전방 트랙을 틀었다
            # (검토 제보 "둘 다 전방 영상"). _on_rear_media_status가 _rear_track_index를 적용한다.
            if not self._rear_active and self._rear_player.source().isEmpty():
                try:
                    tracks = self._player.videoTracks()
                except Exception:  # noqa: BLE001
                    tracks = []
                if len(tracks) >= 2:
                    mode = getattr(self, "_track_mode", TRACK_MODE_BOTH)
                    if mode == TRACK_MODE_REAR:
                        # 후방만: 전방 재생기 하나로 2번 트랙을 튼다(로드 후라 전환이 먹는다).
                        if self._player.activeVideoTrack() != 1:
                            self._player.setActiveVideoTrack(1)
                        self._front_pane.set_caption("후방", True)
                    elif mode == TRACK_MODE_FRONT:
                        self._front_pane.set_caption("전방", True)
                    else:
                        self._rear_prime_pending = True
                        self._rear_track_index = 1
                        self._rear_url = self._player.source()
                        self._rear_player.setSource(self._rear_url)
                        self._set_rear_active(True)
        if not self._prime_pending:
            return
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._prime_pending = False
            self._priming = True
            self._player.play()
            QTimer.singleShot(150, self._finish_prime)

    def _on_rear_media_status(self, status) -> None:
        if self._rear_url.isEmpty() or self._rear_player.source() != self._rear_url:
            return  # 이전 미디어의 낡은 상태 이벤트
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            if self._rear_track_index and self._rear_player.activeVideoTrack() != self._rear_track_index:
                try:
                    if len(self._rear_player.videoTracks()) > self._rear_track_index:
                        self._rear_player.setActiveVideoTrack(self._rear_track_index)
                except Exception:  # noqa: BLE001
                    pass
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
        self._last_clock = ""
        self._clock_label.setText("날짜·시간 -")
        # GPS 기록 행만 따로 두고 첫 기록 시각과 주기를 잡는다. 정보 줄은 이 주기의 슬롯
        # 단위로 판정한다(아래 _slot_point 설명).
        self._gps_rows = gps_record_rows(points)
        self._gps_times = [t for t, _ in self._gps_rows]
        self._gps_interval = estimate_gps_interval(self._gps_rows)
        self._gps_t0 = self._gps_times[0] if self._gps_times else None
        self._update_info(0.0)

    def gps_interval(self) -> float:
        return getattr(self, "_gps_interval", DEFAULT_GPS_INTERVAL_SEC)

    def _slot_point(self, seconds: float) -> Optional[TrackPoint]:
        """재생 시각이 속한 GPS 슬롯의 기록 행. 없으면 None(= 그 슬롯은 GPS 미기록).

        기기는 GPS(1 Hz)보다 훨씬 자주 행을 쓴다(INAVI는 0.1초마다 G센서 행). 예전엔 재생
        시각 이하의 마지막 행을 그대로 봐서, GPS 행 사이의 G센서 행을 만날 때마다 "미기록"이
        떴다(0.4·1.4·2.4초에만 GPS가 있는 파일에서 문구가 계속 나온다는 제보). 이제 GPS 첫
        기록 시각 t0을 기준으로 t0, t0+주기, t0+2주기 … 슬롯을 두고, 재생 시각이 속한 슬롯
        시각에서 주기의 절반 안에 GPS 기록 행이 있으면 그 행을, 없으면 미기록으로 본다.
        슬롯이 시작되기 전(t0 이전)도 미기록이다.
        """
        if self._gps_t0 is None or seconds < self._gps_t0 - 1e-6:
            return None
        interval = self._gps_interval
        k = int((seconds - self._gps_t0) / interval + 1e-6)
        slot_t = self._gps_t0 + k * interval
        # slot_t에 가장 가까운 GPS 기록 행 (양쪽 이웃만 보면 된다)
        i = bisect.bisect_left(self._gps_times, slot_t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self._gps_times):
                d = abs(self._gps_times[j] - slot_t)
                if d <= interval / 2 + 1e-6 and (best is None or d < best[0]):
                    best = (d, self._gps_rows[j][1])
        return best[1] if best else None

    def _update_info(self, seconds: float) -> None:
        if not self._points:
            self._speed_label.setText("속도 -")
            self._coord_label.setText("위치 -")
            self._addr_label.setText("")
            self._status_label.setText("")
            self._addr_key = None
            return

        point = self._slot_point(seconds)

        if point is None:
            status = "(GPS 미기록)"
        elif point.is_outlier:
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

        # 시각은 좌표가 없는 행(status=V)에도 대개 남아 있어 좌표와 별개로 갱신한다.
        clock = gpstime.format_point(point) if point is not None else ""
        if clock:
            self._last_clock = clock
        self._clock_label.setText(f"날짜·시간 {self._last_clock}" if self._last_clock else "날짜·시간 -")

        # 정상 측정이면 값을 갱신하고, 아니면 마지막 정상값을 그대로 둔다.
        has_fix = point is not None and point.has_fix
        shown = point if has_fix else getattr(self, "_last_valid", None)
        if has_fix:
            self._last_valid = point
        if shown is None:
            if point is not None and point.speed_kmh is not None and not point.is_outlier:
                self._speed_label.setText(f"속도 {point.speed_kmh:.1f} km/h")
            else:
                self._speed_label.setText("속도 -")
            self._coord_label.setText("위치 -")
            self._addr_label.setText("")
            self._addr_key = None
            return

        speed = shown.speed_kmh if shown.speed_kmh is not None else (point.speed_kmh if point else None)
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
        if not self._front_url.isEmpty() and self._player.source() != self._front_url:
            return  # 이전 미디어의 길이
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
        self._rear_track_index = 0
        self._front_url = QUrl()
        self._rear_url = QUrl()
        self._player.setSource(QUrl())
        self._rear_player.setSource(QUrl())
        self._set_rear_active(False)
