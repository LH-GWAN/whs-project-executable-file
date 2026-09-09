from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from core.appconfig import MAP_MODE_OFFLINE, MAP_MODE_ONLINE, online_keys, set_map_mode


class MapModeDialog(QDialog):
    """지도를 오프라인/온라인 중 무엇으로 쓸지 고르게 한다.

    수사 자료를 다루는 도구라 "외부로 나가는가"는 사용자가 알고 골라야 하는
    사항이지 조용히 정해줄 것이 아니다. 첫 실행 때 한 번 묻고, 고른 값은 설정에
    남는다. 나중에 [설정 > 지도 사용 방식]에서 바꿀 수 있다.
    """

    def __init__(self, parent=None, current: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("지도 사용 방식 선택")
        self.setModal(True)
        self.setMinimumWidth(600)

        title = QLabel("지도를 어떤 방식으로 사용하시겠습니까?")
        title.setProperty("role", "title")

        self._offline = QRadioButton("오프라인 지도 (권장)")
        offline_desc = QLabel(
            "  · 인터넷 없이 동작합니다. 망분리 PC에서도 사용할 수 있습니다.\n"
            "  · 사건 GPS 좌표가 외부로 나가지 않습니다.\n"
            "  · 지도 파일(수십~수백 MB)을 한 번 내려받아 넣어야 합니다."
        )
        offline_desc.setStyleSheet("color: #555;")

        keys = online_keys()
        keys_ok = bool(keys["js"])
        self._online = QRadioButton("온라인 지도 (카카오맵)")
        online_lines = [
            "  · 별도 지도 파일 없이 도로·건물·지명이 바로 표시됩니다 (인터넷 필요).",
            "  · 재생 중인 위치의 주소(구·동)도 함께 표시됩니다."
            + ("" if keys["rest"] else " ※ REST API 키가 없어 주소는 표시되지 않습니다."),
            "  · 무료 사용 한도(일 단위)가 있어 초과하면 그날은 사용할 수 없습니다.",
        ]
        if not keys_ok:
            online_lines.append("  ※ API 키가 설정되지 않아 지금 고르면 오프라인 지도로 표시됩니다.")
        online_desc = QLabel("\n".join(online_lines))
        online_desc.setStyleSheet("color: #555;")

        warn = QLabel(
            "온라인 지도는 화면에 보이는 위치 범위가, 주소 표시는 해당 좌표가 카카오 서버로\n"
            "전송됩니다. 사건 자료의 기밀이 중요하다면 오프라인을 사용하세요."
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
