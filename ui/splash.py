"""시작 화면과 앱 아이콘.

도구를 실행하면 assets/idas.png(아이콘 256px)를 어두운 배경 위에 약 2초(SPLASH_SECONDS) 보여 준 뒤 메인 창을
연다. 메인 창을 만드는 시간은 그 2초 안에 포함되고, 그보다 오래 걸리면 준비되는 대로 바로 연다.
아이콘 파일이 없으면(소스 트리 밖·번들 누락) 아무것도 띄우지 않고 바로 시작한다.
창·작업 표시줄·exe 아이콘은 assets/idas.ico(Windows), 창 아이콘은 PNG도 함께 쓴다.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from core.paths import resource_root

SPLASH_SECONDS = 2.0
SPLASH_BG = "#0f1215"
SPLASH_SIZE = 360          # 시작 화면 한 변(px)
ICON_SIZE = 200            # 그 안의 아이콘 한 변(px)


def icon_path(name: str = "idas.ico") -> str:
    return os.path.join(resource_root(), "assets", name)


def app_icon() -> Optional[QIcon]:
    """창·작업 표시줄 아이콘. .ico(여러 크기)를 우선하고 없으면 .png."""
    icon = QIcon()
    for name in ("idas.ico", "idas.png"):
        path = icon_path(name)
        if os.path.isfile(path):
            icon.addFile(path)
    return None if icon.isNull() else icon


def splash_pixmap() -> Optional[QPixmap]:
    """어두운 바탕 가운데에 아이콘을 둔 시작 화면 그림."""
    src = QPixmap(icon_path("idas.png"))
    if src.isNull():
        src = QPixmap(icon_path("idas.ico"))
    if src.isNull():
        return None
    canvas = QPixmap(SPLASH_SIZE, SPLASH_SIZE)
    canvas.fill(QColor(SPLASH_BG))
    scaled = src.scaled(ICON_SIZE, ICON_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    painter = QPainter(canvas)
    painter.drawPixmap((SPLASH_SIZE - scaled.width()) // 2, (SPLASH_SIZE - scaled.height()) // 2 - 10, scaled)
    painter.setPen(QColor("#9aa4ae"))
    font = painter.font()
    font.setPointSize(11)
    painter.setFont(font)
    painter.drawText(0, SPLASH_SIZE - 44, SPLASH_SIZE, 20, Qt.AlignHCenter | Qt.AlignVCenter, "IDAS")
    painter.end()
    return canvas


class Splash(QSplashScreen):
    def __init__(self, pixmap: QPixmap):
        super().__init__(pixmap, Qt.WindowStaysOnTopHint)
        self._shown_at = time.monotonic()

    def wait_until(self, seconds: float) -> None:
        """처음 보인 뒤 seconds가 지날 때까지 이벤트를 돌리며 기다린다(이미 지났으면 바로 돌아옴)."""
        app = QApplication.instance()
        while app is not None and time.monotonic() - self._shown_at < seconds:
            app.processEvents()
            time.sleep(0.02)


def show_splash(app: QApplication) -> Optional[Splash]:
    pixmap = splash_pixmap()
    if pixmap is None:
        return None
    splash = Splash(pixmap)
    splash.show()
    app.processEvents()
    return splash
