"""온라인 지도(카카오맵) 키가 없을 때 발급·등록·저장 절차를 안내하는 창.

배경지도(pmtiles) 안내(ui/basemap_notice.py)와 같은 역할이다. 키 파일은 git으로
오지 않으므로(공개 저장소라 .gitignore), 받는 사람이 직접 발급받아 넣어야 한다.
"""
from __future__ import annotations

import json
import os

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QMessageBox

from core.appconfig import (
    MAP_MODE_ONLINE,
    MAP_SERVER_PREFERRED_PORTS,
    ONLINE_KEYS_DOWNLOAD_URL,
    get_map_mode,
    is_online_map_ready,
    key_status_report,
    keys_dir,
    keys_file_path,
    online_keys,
)

_SETTINGS_ORG = "GPSTracer"
_SETTINGS_APP = "GPSTracer"
_SUPPRESS_KEY = "online_keys_notice/suppressed"

KAKAO_CONSOLE_URL = "https://developers.kakao.com/console/app"


def download_available() -> bool:
    """팀 공용 키 파일의 공유 링크가 설정돼 있는가. 없으면 직접 발급 절차만 안내한다."""
    return bool(ONLINE_KEYS_DOWNLOAD_URL)

# 키 파일 틀. 값이 비어 있으면 is_online_map_ready()가 False라 외부 요청이 나가지 않는다.
# 자리표시 문자열을 넣어 두면 카카오가 "키 없음"으로 거절해 엉뚱한 안내가 뜨므로 빈 값으로 둔다.
KEYS_TEMPLATE = {
    "_안내": "카카오 디벨로퍼스 > 내 애플리케이션 > [앱] > [플랫폼 키]에서 두 키를 복사해 따옴표 안에 넣고 프로그램을 다시 시작하세요. 이 파일은 git에 올라가지 않습니다.",
    "provider": "kakao",
    "kakao_js_key": "",
    "kakao_rest_key": "",
}


def should_show_notice() -> bool:
    """온라인을 골랐는데 지도용 키가 없고, 사용자가 안내를 끄지 않았을 때."""
    if get_map_mode() != MAP_MODE_ONLINE:
        return False
    online_keys(force=True)  # 방금 넣은 파일도 바로 반영
    if is_online_map_ready():
        return False
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    return not settings.value(_SUPPRESS_KEY, False, type=bool)


def ensure_keys_template() -> str:
    """키 파일이 없으면 빈 틀을 만들어 두고 경로를 돌려준다. 있으면 건드리지 않는다."""
    path = keys_file_path()
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(KEYS_TEMPLATE, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return path


def notice_text() -> str:
    domains = "\n".join(f"      {'http://127.0.0.1:' + str(p)}" for p in MAP_SERVER_PREFERRED_PORTS)
    keys = online_keys(force=True)
    state = []
    if not keys["js"]:
        state.append("JavaScript 키 없음")
    if not keys["rest"]:
        state.append("REST API 키 없음")
    # 어디를 봤고 어떤 파일을 왜 못 썼는지. "넣었는데 없다고 한다"를 여기서 풀어 준다.
    report = "\n".join("   " + line for line in key_status_report().splitlines()[1:])
    where_block = (
        f"키 파일을 찾는 폴더 (exe로 쓸 때는 _internal\\assets 가 이 폴더입니다):\n   {keys_dir()}\n"
        + (report + "\n" if report else "")
        + "   ※ 이름은 online_keys.json 이 아니어도 되지만 .json 또는 .txt 여야 하고, 값은\n"
        "      영문 소문자·숫자 32자여야 합니다. 파일을 넣은 뒤 [다시 확인]을 누르면 프로그램을\n"
        "      다시 켜거나 빌드하지 않아도 바로 인식합니다.\n"
    )
    shared_block = ""
    if download_available():
        shared_block = (
            "\n"
            "▶ 팀 공용 키 파일이 준비돼 있어 직접 발급받지 않아도 됩니다.\n"
            "   [키 파일 내려받기]를 누르면 브라우저에서 online_keys.json 을 받을 수 있고,\n"
            "   넣을 폴더도 같이 열립니다. 받은 파일을 그 폴더에 그대로 넣고 프로그램을\n"
            "   껐다 켜면 됩니다. 아래 1~5번은 직접 발급받을 때의 절차입니다.\n"
        )
    step4 = (
        "4. [키 파일 만들기]를 누르면 위 위치에 빈 키 파일이 생기고 열립니다.\n"
        "   같은 화면의 JavaScript 키(3번에서 도메인을 등록한 그 키)와 REST API 키를\n"
        "   따옴표 안에 붙여 넣고 저장합니다. 네이티브 앱 키는 쓰지 않습니다.\n"
    )
    return (
        f"현재 상태: {', '.join(state) if state else '키 설정됨'}\n"
        f"{where_block}"
        f"{shared_block}"
        "\n"
        "1. [카카오 디벨로퍼스 열기]를 눌러 로그인하고 [애플리케이션 추가]로 앱을 만듭니다\n"
        "   (이미 있으면 그 앱을 씁니다)\n"
        "\n"
        "2. 왼쪽 메뉴 [카카오맵] > 사용 설정을 ON 으로 켭니다\n"
        "   (개발자 계정에서 처음 켠 앱에만 무료 한도가 붙습니다)\n"
        "\n"
        "3. [앱] > [플랫폼 키]에서 JavaScript 키 카드를 열고 [JavaScript SDK 도메인]에\n"
        "   아래 3개를 등록합니다 (제품 링크 관리의 '웹 도메인'이 아닙니다)\n"
        f"{domains}\n"
        "\n"
        f"{step4}"
        "\n"
        "5. 프로그램을 껐다 켭니다\n"
        "\n"
        "키 파일은 git에 올라가지 않습니다. 다른 PC에서 빌드하거나 쓰려면 이 파일을\n"
        "USB 등으로 옮겨 같은 위치에 넣으세요. 자세한 절차는 assets/README.md 참고."
    )


def show_online_keys_notice(parent=None, allow_suppress: bool = True) -> bool:
    """안내 창을 띄운다. 창을 닫을 때 키가 인식된 상태면 True(호출한 쪽이 지도를 다시 띄운다)."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle("온라인 지도 키 설정")
    ready_now = is_online_map_ready()
    box.setText("온라인 지도 키가 인식됐습니다." if ready_now
                else "온라인 지도(카카오맵)를 쓰려면 카카오 API 키가 필요한데, 아직 찾지 못했습니다.\n"
                     "키를 넣기 전까지는 오프라인 지도(배경지도 파일)로 표시됩니다.")
    box.setInformativeText(notice_text())
    recheck_btn = box.addButton("다시 확인", QMessageBox.ActionRole)
    download_btn = None
    if download_available():
        download_btn = box.addButton("키 파일 내려받기", QMessageBox.ActionRole)
    console_btn = box.addButton("카카오 디벨로퍼스 열기", QMessageBox.ActionRole)
    file_btn = box.addButton("키 파일 만들기", QMessageBox.ActionRole)
    folder_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
    box.addButton(QMessageBox.Ok)
    box.setDefaultButton(download_btn if download_btn is not None else console_btn)
    box.setTextInteractionFlags(Qt.TextSelectableByMouse)

    suppress = None
    if allow_suppress:
        suppress = QCheckBox("다시 표시하지 않음")
        box.setCheckBox(suppress)
    box.exec()

    folder = keys_dir()
    clicked = box.clickedButton()
    if clicked is recheck_btn:
        keys = online_keys(force=True)
        if keys["js"]:
            QMessageBox.information(
                parent, "온라인 지도 키",
                f"키를 찾았습니다: {keys['source']}\n온라인 지도를 켭니다."
                + ("" if keys["rest"] else "\n(REST API 키가 없어 주소는 표시되지 않습니다)"))
            return True
        # 아직 못 찾았으면 이유가 갱신된 안내 창을 다시 띄운다.
        return show_online_keys_notice(parent, allow_suppress)
    if download_btn is not None and clicked is download_btn:
        # 배경지도 안내와 같다 - 버튼을 누르면 창이 닫혀 [폴더 열기]를 따로 누를 수 없으니
        # 받는 동안 넣을 폴더도 같이 열어 둔다.
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl(ONLINE_KEYS_DOWNLOAD_URL))
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
    elif clicked is console_btn:
        QDesktopServices.openUrl(QUrl(KAKAO_CONSOLE_URL))
    elif clicked is file_btn:
        try:
            path = ensure_keys_template()
        except OSError as e:
            QMessageBox.warning(parent, "키 파일", f"키 파일을 만들지 못했습니다:\n{e}")
            return
        # 파일을 기본 편집기로 열고, 못 열면(연결 프로그램 없음) 폴더라도 연다.
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
    elif clicked is folder_btn:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    if suppress is not None and suppress.isChecked():
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(_SUPPRESS_KEY, True)
    return bool(online_keys(force=True)["js"]) and not ready_now
