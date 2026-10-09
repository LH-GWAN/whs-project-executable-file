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
    if os.path.exists(new_dir) and not _is_empty_dir(new_dir):
        return ""
    for legacy in LEGACY_APP_DATA_DIRNAMES:
        old_dir = os.path.join(base, legacy)
        if os.path.isdir(old_dir):
            try:
                # 이전 시도가 실패해 빈 새 폴더만 남았으면 치우고 다시 옮긴다(리뷰 #27).
                if os.path.isdir(new_dir):
                    os.rmdir(new_dir)
                os.rename(old_dir, new_dir)
            except OSError:
                return ""
            _rewrite_db_paths(new_dir, old_dir)
            return old_dir
    return ""


def _is_empty_dir(path: str) -> bool:
    try:
        return not os.listdir(path)
    except OSError:
        return False


def _rewrite_db_paths(new_dir: str, old_dir: str) -> None:
    """옮긴 DB 안의 절대경로(output_folder 등)를 새 폴더로 바꾼다. 폴더 이름만 바꾸면 옛 경로가
    남아 사건을 다시 열면 빈 결과가 되고 폴더 삭제도 실패했다(리뷰 #16)."""
    import sqlite3
    db = os.path.join(new_dir, "history.db")
    if not os.path.isfile(db):
        return
    prefix = old_dir.rstrip("/\\")
    try:
        conn = sqlite3.connect(db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cases)")}
            for column in ("output_folder", "report_pdf_path", "source_video_path"):
                if column not in columns:
                    continue
                rows = conn.execute(f"SELECT id, {column} FROM cases WHERE {column} IS NOT NULL").fetchall()
                for case_id, value in rows:
                    if isinstance(value, str) and value.startswith(prefix) \
                            and (len(value) == len(prefix) or value[len(prefix)] in "/\\"):
                        conn.execute(f"UPDATE cases SET {column} = ? WHERE id = ?",
                                     (new_dir.rstrip("/\\") + value[len(prefix):], case_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass
def app_data_dir() -> str:
    migrate_legacy_app_data()
    return os.path.join(_data_base_dir(), APP_DATA_DIRNAME)
