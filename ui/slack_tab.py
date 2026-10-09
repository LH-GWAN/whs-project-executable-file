"""슬랙 데이터 탭 - 옛 녹화 잔재에서 카빙한 GPS를 지도로 본다. 영상은 없다.

슬랙은 컨테이너가 참조하지 않는 영역이라 재생할 영상이 없다(사용자 결정: 영상 칸에는 '영상 없음'만
적는다). 지도는 Tracker와 같은 MapView를 쓰되 재생 위치는 없다. 표·그래프는 Speed/Location 탭의
"… 슬랙" 묶음에서 본다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget

from core.slack import SlackSet
from ui.map_view import MapView


class SlackTab(QWidget):
    def __init__(self, slack: SlackSet, parent=None):
        super().__init__(parent)
        self.slack = slack
        self._map = MapView()

        title = QLabel("영상 없음")
        font = title.font()
        font.setPointSize(font.pointSize() + 6)
        font.setBold(True)
        title.setFont(font)
        title.setAlignment(Qt.AlignCenter)
        note = QLabel(
            "슬랙 데이터는 영상 파일이 참조하지 않는 영역(옛 녹화의 잔재)에서 카빙한 GPS 기록입니다.\n"
            "현재 영상과 다른 시점의 기록이라 재생할 영상이 없고, 영상 시간축과도 맞지 않습니다.\n"
            "시각은 슬랙 첫 기록을 0초로 둔 경과 초이며, 지도·표·그래프 모두 그 기준입니다.")
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignCenter)
        note.setStyleSheet("color: #666;")
        summary = QLabel(slack.summary)
        summary.setAlignment(Qt.AlignCenter)
        summary.setWordWrap(True)

        panel = QWidget()
        panel.setObjectName("SlackPanel")
        panel.setStyleSheet("#SlackPanel { background: #111; border-radius: 4px; }")
        panel_layout = QVBoxLayout(panel)
        panel_layout.addStretch(1)
        panel_layout.addWidget(title)
        panel_layout.addWidget(note)
        panel_layout.addSpacing(8)
        panel_layout.addWidget(summary)
        panel_layout.addStretch(1)
        for w in (title, note, summary):
            w.setStyleSheet(w.styleSheet() + " color: #ddd;")

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(panel)
        splitter.addWidget(self._map)
        splitter.setSizes([420, 620])
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self._map.set_track(slack.map_points, [])

    def map_view(self) -> MapView:
        return self._map

    def ensure_map_loaded(self) -> None:
        self._map.ensure_loaded()

    def grab_map_png(self):
        return self._map.grab_png()
