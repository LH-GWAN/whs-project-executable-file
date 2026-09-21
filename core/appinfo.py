"""앱 이름과 사용자 데이터 폴더 - 이름이 쓰이는 곳은 전부 여기를 본다.

도구 이름이 GPS Tracer에서 IDAS로 바뀌었다. 창 제목·exe 이름·데이터 폴더·레지스트리 키가
전부 이름을 따르므로 한 곳에서 관리한다. 예전 이름으로 만들어진 데이터 폴더
(%LOCALAPPDATA%\\GPSTracer)는 처음 실행할 때 새 이름으로 옮겨서 사건 이력이 사라지지 않게 한다.
"""
from __future__ import annotations

import os

APP_NAME = "IDAS"                 # 창 제목·화면 제목·exe 이름
APP_DATA_DIRNAME = "IDAS"         # %LOCALAPPDATA%\IDAS (Windows), ~/.local/share/IDAS (그 외)
SETTINGS_ORG = "IDAS"             # QSettings (HKCU\Software\IDAS)
SETTINGS_APP = "IDAS"
USER_AGENT = "IDAS"
LEGACY_APP_DATA_DIRNAMES = ("GPSTracer",)

_migrated = False


def _data_base_dir() -> str:
    if os.name == "nt":
        return os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def migrate_legacy_app_data() -> str:
    """예전 이름의 데이터 폴더가 있고 새 폴더가 아직 없으면 이름만 바꿔 넘긴다. 옮긴 폴더
    경로를 돌려주고, 할 일이 없거나 실패하면 ""(그 경우 새 폴더를 새로 쓴다)."""
    global _migrated
    if _migrated:
        return ""
    _migrated = True
    base = _data_base_dir()
    new_dir = os.path.join(base, APP_DATA_DIRNAME)
    if os.path.exists(new_dir):
        return ""
    for legacy in LEGACY_APP_DATA_DIRNAMES:
        old_dir = os.path.join(base, legacy)
        if os.path.isdir(old_dir):
            try:
                os.rename(old_dir, new_dir)
                return old_dir
            except OSError:
                return ""
    return ""


def app_data_dir() -> str:
    migrate_legacy_app_data()
    return os.path.join(_data_base_dir(), APP_DATA_DIRNAME)
