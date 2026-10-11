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
# 옮기지 못했거나 옛 폴더가 남았을 때 사용자에게 보여 줄 문구. 빈 문자열이면 알릴 것이 없다.
# (시작 때 main_window가 한 번 읽어 알리고 비우면 된다 - 리뷰 #58 "실패하면 알림".)
MIGRATION_NOTICE = ""

_DB_NAME = "history.db"
_DB_SIDECARS = ("-journal", "-wal", "-shm")


def _data_base_dir() -> str:
    if os.name == "nt":
        return os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def migrate_legacy_app_data() -> str:
    """예전 이름의 데이터 폴더가 있으면 새 이름으로 옮긴다. 옮긴 폴더 경로를 돌려주고, 할 일이
    없거나 실패하면 ""(그 경우 새 폴더를 새로 쓴다). 프로세스마다 한 번만 실행한다.

    - 새 폴더가 없으면 이름만 바꾼다. 새 폴더가 '사실상 비어' 있으면(이전 실행이 옮기지 못하고 빈 DB·
      빈 cases만 만든 경우) 내용을 옮겨 넣어 다시 시도한다 - 예전엔 비어 있지 않다고 바로 포기해
      이력이 영영 0건이었다(리뷰 #58).
    - 새 폴더에 이미 사건이 있으면 두 이력을 섞지 않고 MIGRATION_NOTICE로 알린다.
    - 옛 빌드가 폴더 이름만 바꾸고 DB 안의 절대경로를 고치지 않은 PC를 위해, 시작할 때마다 옛 접두
      경로를 새 폴더로 재작성한다(이번 실행에서 옮겼는지와 무관하게, 리뷰 #58·#16)."""
    global _migrated, MIGRATION_NOTICE
    if _migrated:
        return ""
    _migrated = True
    base = _data_base_dir()
    new_dir = os.path.join(base, APP_DATA_DIRNAME)
    moved = ""
    notices = []
    for legacy in LEGACY_APP_DATA_DIRNAMES:
        old_dir = os.path.join(base, legacy)
        if not os.path.isdir(old_dir):
            continue
        if moved:
            notices.append(f"예전 데이터 폴더가 더 남아 있습니다: {old_dir}")
            continue
        if not _effectively_empty(new_dir):
            notices.append(f"예전 데이터 폴더 {old_dir} 가 남아 있지만 새 폴더 {new_dir} 에 이미 사건 이력이 있어 "
                           "옮기지 않았습니다. 예전 사건이 필요하면 옛 폴더를 따로 보관하세요.")
            continue
        error = _move_legacy(old_dir, new_dir)
        if error:
            notices.append(f"예전 데이터 폴더 {old_dir} 를 {new_dir} 로 옮기지 못했습니다: {error}\n"
                           "다른 프로그램이 열고 있지 않은지, 폴더에 쓸 권한이 있는지 확인한 뒤 다시 실행하면 "
                           "다시 시도합니다. 그때까지 예전 사건 이력은 보이지 않습니다.")
            continue
        moved = old_dir
    for legacy in LEGACY_APP_DATA_DIRNAMES:
        _rewrite_db_paths(new_dir, os.path.join(base, legacy))
    MIGRATION_NOTICE = "\n\n".join(notices)
    return moved


def _is_empty_dir(path: str) -> bool:
    try:
        return not os.listdir(path)
    except OSError:
        return False


def _db_case_count(db: str):
    """history.db의 사건 수. 못 읽으면(손상·잠김) None."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "cases" not in tables:
                return 0
            return int(conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0])
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _effectively_empty(new_dir: str) -> bool:
    """새 폴더에 보존할 사용자 데이터가 없는가: 없거나, 비었거나, 사건 0건인 history.db·빈 cases·설정
    파일(settings.json 등 사건이 아닌 파일)만 있으면 비어 있다고 본다."""
    if not os.path.isdir(new_dir):
        return True
    try:
        entries = os.listdir(new_dir)
    except OSError:
        return False
    for name in entries:
        full = os.path.join(new_dir, name)
        if name == _DB_NAME:
            count = _db_case_count(full)
            if count is None or count > 0:
                return False
        elif name.startswith(_DB_NAME) and name[len(_DB_NAME):] in _DB_SIDECARS:
            continue
        elif os.path.isdir(full):
            if not _is_empty_dir(full):
                return False
        elif os.path.getsize(full) > 0 and os.path.splitext(name)[1].lower() not in (".json", ".ini", ".txt", ".log"):
            return False
    return True


def _move_legacy(old_dir: str, new_dir: str) -> str:
    """옛 폴더를 새 폴더로. 새 폴더가 없으면 이름만 바꾸고, 있으면(사실상 빈 폴더) 항목을 하나씩 옮긴다.
    성공하면 "", 실패하면 이유."""
    try:
        if not os.path.exists(new_dir):
            os.rename(old_dir, new_dir)
            return ""
        if _is_empty_dir(new_dir):
            os.rmdir(new_dir)
            os.rename(old_dir, new_dir)
            return ""
        for name in os.listdir(old_dir):
            src, dst = os.path.join(old_dir, name), os.path.join(new_dir, name)
            if os.path.isdir(src):
                if os.path.isdir(dst) and _is_empty_dir(dst):
                    os.rmdir(dst)
                if os.path.exists(dst):
                    continue   # 새 폴더 쪽에 내용이 있는 하위 폴더는 건드리지 않는다(사실상 빈 폴더 판정상 없다)
                os.rename(src, dst)
            elif name == _DB_NAME or (name.startswith(_DB_NAME) and name[len(_DB_NAME):] in _DB_SIDECARS):
                os.replace(src, dst)   # 새 폴더의 빈 DB(사건 0건)와 그 사이드카는 옛 것으로 바꾼다
            elif not os.path.exists(dst):
                os.rename(src, dst)
            # 새 폴더에 이미 있는 설정 파일 등은 새 쪽을 남긴다(옛 것은 옛 폴더에 그대로)
        if _is_empty_dir(old_dir):
            os.rmdir(old_dir)
        return ""
    except OSError as exc:
        return str(exc)


def _rewrite_db_paths(new_dir: str, old_dir: str) -> None:
    """DB 안의 절대경로(output_folder 등) 중 옛 폴더 접두를 새 폴더로 바꾼다. 폴더 이름만 바꾸면 옛 경로가
    남아 사건을 다시 열면 빈 결과가 되고 폴더 삭제도 실패했다(리뷰 #16). 옛 접두가 없는 DB는 그대로다."""
    import sqlite3
    db = os.path.join(new_dir, _DB_NAME)
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
