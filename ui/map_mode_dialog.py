from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from core.appconfig import MAP_MODE_OFFLINE, MAP_MODE_ONLINE, set_map_mode


class MapModeDialog(QDialog):
    """첫 실행 때 지도를 오프라인/온라인 중 무엇으로 쓸지 고르게 한다.

    수사 자료를 다루는 도구라 "외부로 나가는가"는 사용자가 알고 골라야 하는
    사항이지 조용히 정해줄 것이 아니다. 고른 값은 설정에 남고, 나중에
    파일 > 지도 설정에서 바꿀 수 있다.
    """

    def __init__(self, parent=None, current: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("지도 사용 방식 선택")
        self.setModal(True)
        self.setMinimumWidth(560)

        title = QLabel("지도를 어떤 방식으로 사용하시겠습니까?")
        title.setProperty("role", "title")

        self._offline = QRadioButton("오프라인 지도 (권장)")
        offline_desc = QLabel(
            "  · 인터넷 없이 동작합니다. 망분리 PC에서도 사용할 수 있습니다.\n"
            "  · 사건 GPS 좌표가 외부로 나가지 않습니다.\n"
            "  · 지도 파일(수십~수백 MB)을 한 번 내려받아 넣어야 합니다."
        )
        offline_desc.setStyleSheet("color: #555;")

        self._online = QRadioButton("온라인 지도")
        online_desc = QLabel(
            "  · 별도 지도 파일 없이 바로 도로·건물이 표시됩니다.\n"
            "  · 지도를 볼 때마다 외부 지도 서비스에 접속합니다.\n"
            "  ※ 아직 준비 중입니다. 지금 선택하면 궤적만 표시됩니다."
        )
        online_desc.setStyleSheet("color: #555;")

        warn = QLabel(
            "온라인 지도는 화면에 보이는 위치 범위가 외부 서비스로 전송됩니다.\n"
            "사건 자료의 기밀이 중요하다면 오프라인을 사용하세요."
        )
        warn.setStyleSheet("color: #b34; padding-top: 6px;")
        warn.setWordWrap(True)

        if current == MAP_MODE_ONLINE:
            self._online.setChecked(True)
        else:
            self._offline.setChecked(True)

        ok = QPushButton("확인")
        ok.setProperty("role", "primary")
        ok.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(ok)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addSpacing(6)
        layout.addWidget(self._offline)
        layout.addWidget(offline_desc)
        layout.addSpacing(10)
        layout.addWidget(self._online)
        layout.addWidget(online_desc)
        layout.addWidget(warn)
        layout.addLayout(buttons)

    def selected_mode(self) -> str:
        return MAP_MODE_ONLINE if self._online.isChecked() else MAP_MODE_OFFLINE


def ask_map_mode(parent=None, current: str | None = None) -> str:
    dialog = MapModeDialog(parent, current)
    dialog.exec()
    mode = dialog.selected_mode()
    set_map_mode(mode)
    return mode
