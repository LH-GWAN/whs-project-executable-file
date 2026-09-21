from __future__ import annotations

import os

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QMessageBox

from core.appinfo import SETTINGS_APP, SETTINGS_ORG
from core.basemap import BASEMAP_DOWNLOAD_URL, assets_dir, basemap_path

_SETTINGS_ORG = SETTINGS_ORG
_SETTINGS_APP = SETTINGS_APP
_SUPPRESS_KEY = "basemap_notice/suppressed"


def should_show_notice() -> bool:
    if basemap_path() is not None:
        return False
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    return not settings.value(_SUPPRESS_KEY, False, type=bool)


def show_basemap_notice(parent=None) -> None:
    target_dir = assets_dir()
    # 지도 없이 빌드하면 이 폴더가 아예 안 생긴다. 경로만 알려주고 폴더가 없으면
    # 사용자가 헤매므로, 안내할 때 미리 만들어 두고 바로 열 수 있게 한다.
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError:
        pass

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle("배경지도 없음")
    box.setText("배경지도 파일이 없어 지도에 도로와 건물이 표시되지 않습니다.")
    box.setInformativeText(
        "GPS 궤적은 정상적으로 표시되며 분석 기능도 모두 동작합니다.\n"
        "도로 위에 궤적을 보시려면 아래대로 하시면 됩니다.\n"
        "\n"
        "1. [지도 내려받기]를 눌러 파일을 받습니다\n"
        "\n"
        "2. [폴더 열기]를 눌러 열리는 폴더에 받은 파일을 그대로 넣습니다\n"
        f"   {target_dir}\n"
        "\n"
        "3. 프로그램을 껐다 켭니다\n"
        "\n"
        "다시 빌드할 필요 없습니다. .zip으로 받으셨어도 풀지 않고 그대로\n"
        "넣으시면 프로그램이 알아서 풉니다.\n"
        "\n"
        "지도 파일은 용량이 커서 GitHub에 올릴 수 없어(웹 업로드 25MB 제한)\n"
        "외부 공유 링크로 배포합니다. git pull로는 받아지지 않습니다.\n"
        "직접 만드는 방법은 assets/README.md에 있습니다."
    )
    download_btn = box.addButton("지도 내려받기", QMessageBox.ActionRole)
    folder_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
    box.addButton(QMessageBox.Ok)
    box.setDefaultButton(download_btn)

    suppress = QCheckBox("다시 표시하지 않음")
    box.setCheckBox(suppress)
    box.exec()

    clicked = box.clickedButton()
    if clicked is download_btn:
        # 버튼을 누르면 창이 닫혀서 [폴더 열기]를 따로 누를 수 없다. 받는 동안
        # 넣을 폴더도 같이 열어두면 다운로드가 끝나는 대로 끌어다 놓으면 된다.
        QDesktopServices.openUrl(QUrl(BASEMAP_DOWNLOAD_URL))
        QDesktopServices.openUrl(QUrl.fromLocalFile(target_dir))
    elif clicked is folder_btn:
        QDesktopServices.openUrl(QUrl.fromLocalFile(target_dir))

    if suppress.isChecked():
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(_SUPPRESS_KEY, True)
