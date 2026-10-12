"""디자인(검정+민트) 팔레트·앱 아이콘·시작 화면(2026-10-12)."""
from __future__ import annotations

import os
import time

import pytest
from PySide6.QtWidgets import QApplication

from ui import splash as splash_mod
from ui.styles import APP_QSS, COLORS


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_palette_is_dark_mint():
    assert COLORS["bg"] == "#0f1215" and COLORS["mint"] == "#3ddc97"
    assert "background-color: #0f1215" in APP_QSS and "#3ddc97" in APP_QSS
    assert "#ffffff;\n    color: #111111" not in APP_QSS      # 옛 흰 배경 테마가 남아 있지 않다


def test_icon_files_bundled_and_loadable(qapp):
    assert os.path.isfile(splash_mod.icon_path("idas.ico")) and os.path.isfile(splash_mod.icon_path("idas.png"))
    icon = splash_mod.app_icon()
    assert icon is not None and not icon.isNull()
    pm = splash_mod.splash_pixmap()
    assert pm is not None and pm.width() == splash_mod.SPLASH_SIZE


def test_splash_waits_about_the_requested_time(qapp):
    s = splash_mod.show_splash(qapp)
    assert s is not None
    # 처음 보인 시각부터 잰다(창 준비 시간이 그 안에 포함되는 것이 의도).
    s.wait_until(1.2)
    assert time.monotonic() >= s._shown_at + 1.2 - 0.02
    t0 = time.monotonic()
    s.wait_until(0.1)                      # 이미 지난 시간이면 바로 돌아온다
    assert time.monotonic() - t0 < 0.5
    s.close()


def test_splash_skipped_without_icon(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(splash_mod, "resource_root", lambda: str(tmp_path))
    assert splash_mod.app_icon() is None and splash_mod.splash_pixmap() is None
    assert splash_mod.show_splash(qapp) is None
