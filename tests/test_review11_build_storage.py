"""2026-10-11 리뷰 반영 회귀 - 담당 F: 빌드 스크립트·이력 DB·데이터 폴더 이전·전후방 대조·슬랙·트랙 판별.

항목: #25 #49 #53 #57 #58 #59 #60 #98 #103 #104 #105 #106 #131 (#133은 보류).
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
import stat
import struct
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# #25 #98 #59: .bat 블록 구문 검사 (macOS/Linux에서는 실행할 수 없어 파싱으로 확인)
# ---------------------------------------------------------------------------

_BLOCK_OPENERS = ("if ", "for ", "else", ")", "(")


def _strip_quoted_and_escaped(line: str) -> str:
    """따옴표 안과 ^이스케이프된 글자를 공백으로 바꿔 괄호 검사에서 뺀다(cmd는 따옴표 안의 특수 문자를 안 본다)."""
    out = []
    in_quote = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            in_quote = not in_quote
            out.append(" ")
        elif ch == "^" and not in_quote and i + 1 < len(line):
            out.append("  ")
            i += 1
        else:
            out.append(" " if in_quote else ch)
        i += 1
    return "".join(out)


def check_bat_blocks(text: str) -> list:
    """cmd 괄호 블록 검사. 블록 안에서 따옴표 밖의 ')'는 블록을 닫으므로, 블록을 여닫는 줄(if/for/else/'('/')'로
    시작)이 아닌 명령 줄에 ')'가 있으면 오류다(리뷰 #25). 블록 안의 따옴표 없는 %~dp0·%CD%도 경로에 ')'가
    있으면 같은 오류라 잡는다. 돌려주는 값은 (줄 번호, 내용) 목록."""
    problems = []
    depth = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if depth == 0 and (lowered.startswith("rem ") or lowered.startswith("::")):
            continue
        bare = _strip_quoted_and_escaped(line)
        bare_l = bare.lower().lstrip("@ ")
        opener = bare_l.startswith(_BLOCK_OPENERS) or "||" in bare or "&&" in bare
        if depth > 0 and re.search(r"%~dp0|%CD%", bare, re.IGNORECASE):
            problems.append((lineno, "block: unquoted %~dp0 / %CD% - breaks if the path contains ')'"))
        if opener:
            for ch in bare:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        problems.append((lineno, "')' without an open block"))
                    else:
                        depth -= 1
        else:
            if depth > 0 and ")" in bare:
                problems.append((lineno, "unquoted ')' inside a ( ... ) block ends the block: " + raw.strip()))
            # 블록 밖 명령 줄의 괄호는 글자다(echo (text) 등)
    if depth != 0:
        problems.append((0, f"unbalanced blocks at end of file: depth {depth}"))
    return problems


def test_bat_checker_catches_the_original_regression():
    buggy = (
        'pushd "%~dp0" || (\n'
        '    echo [ERROR] Cannot change to the script folder: %~dp0\n'
        '    echo         Copy the project to a local drive (not a \\\\server\\share path).\n'
        '    exit /b 1\n'
        ')\n'
    )
    found = check_bat_blocks(buggy)
    assert any("unquoted ')'" in msg for _, msg in found), found
    assert any("%~dp0" in msg for _, msg in found), found
    fixed = (
        'set "SCRIPT_DIR=%~dp0"\n'
        'pushd "%SCRIPT_DIR%" || (\n'
        '    echo [ERROR] Cannot change to the script folder: "%SCRIPT_DIR%"\n'
        '    echo         Copy the project to a local drive, not a \\\\server\\share path.\n'
        '    choice /c YN /m "Continue (auto-yes)"\n'
        '    exit /b 1\n'
        ')\n'
        'echo (parentheses outside a block are plain text)\n'
        'for /d /r %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"\n'
        'if errorlevel 2 (\n'
        '    echo Cancelled.\n'
        ') else (\n'
        '    echo App settings kept. Use Settings ^> Reset ^(inside the app^).\n'
        ')\n'
    )
    assert check_bat_blocks(fixed) == []


@pytest.mark.parametrize("name", ["build_windows.bat", "clean_windows.bat"])
def test_bat_scripts_have_valid_blocks(name):
    text = (ROOT / name).read_text(encoding="ascii")   # ASCII only (CP949 콘솔)
    assert check_bat_blocks(text) == []


@pytest.mark.parametrize("name", ["build_windows.bat", "clean_windows.bat"])
def test_bat_scripts_refuse_unc_paths(name):
    """#98: pushd는 UNC 경로를 임시 드라이브로 매핑해 그대로 진행하므로 명시적으로 막는다."""
    text = (ROOT / name).read_text(encoding="ascii")
    assert 'if "%SCRIPT_DIR:~0,2%"=="\\\\" (' in text
    assert 'pushd "%SCRIPT_DIR%"' in text


def test_build_bat_keeps_only_user_added_assets_and_never_wipes_leftovers():
    """#59: assets\\에 있는 파일은 보관하지 않고(새로 번들됨), 남은 보관본은 지우지 않으며, 복원은 덮어쓰지 않는다."""
    text = (ROOT / "build_windows.bat").read_text(encoding="ascii")
    assert 'if exist "%KEEP%\\" rmdir /s /q "%KEEP%"' not in text
    assert 'if not exist "assets\\%%~nxf"' in text
    assert "xcopy" not in text                       # /Y 통째 복원이 새 키를 옛 키로 덮었다
    assert 'if exist "dist\\IDAS\\_internal\\assets\\%%~nxf"' in text
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/build_keep_assets/" in gitignore.splitlines()


# ---------------------------------------------------------------------------
# #60: idas.spec가 '['가 든 폴더에서도 assets를 찾는다
# ---------------------------------------------------------------------------

def test_spec_asset_lookup_survives_brackets_in_path(tmp_path):
    spec = (ROOT / "idas.spec").read_text(encoding="utf-8")
    body = spec[spec.index("_ASSETS = os.path.join(_ROOT"):spec.index("a = Analysis(")]
    root = tmp_path / "[팀] IDAS"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "korea.pmtiles").write_bytes(b"x")
    (root / "assets" / "online_keys (1).json").write_text("{}")
    (root / "assets" / "README.txt").write_text("doc")
    (root / "assets" / "notes.txt").write_text("t")
    ns = {"os": os, "_ROOT": str(root), "web_datas": []}
    exec(body, ns)
    bundled = sorted(os.path.basename(src) for src, _dest in ns["web_datas"])
    assert bundled == ["korea.pmtiles", "notes.txt", "online_keys (1).json"]
    assert "glob" not in body


# ---------------------------------------------------------------------------
# #57: history.db 복구는 손상일 때만
# ---------------------------------------------------------------------------

def _store_with_cases(db_path: str, n: int = 40):
    from storage.history_store import CaseRecord, HistoryStore
    store = HistoryStore(db_path)
    for i in range(n):
        store.add_case(CaseRecord(id=None, case_number=f"C-{i:03d}", examiner="x" * 200, memo="m" * 400,
                                  output_folder=f"/data/cases/{i}"))
    store.close()


def test_garbage_db_is_moved_aside_and_recovered(tmp_path):
    from storage.history_store import HistoryStore
    db = tmp_path / "history.db"
    db.write_bytes(b"not a database at all" * 100)
    HistoryStore.recovered_from = ""
    with HistoryStore(str(db)) as store:
        assert store.list_cases() == []
    assert HistoryStore.recovered_from and "corrupt-" in HistoryStore.recovered_from
    assert list(tmp_path.glob("history.db.corrupt-*"))
    HistoryStore.recovered_from = ""


def test_locked_or_readonly_db_is_not_treated_as_corrupt(tmp_path, monkeypatch):
    from storage import history_store as hs
    db = tmp_path / "history.db"
    _store_with_cases(str(db), 5)
    before = db.read_bytes()
    hs.HistoryStore.recovered_from = ""

    def locked(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hs.HistoryStore, "_open", locked)
    with pytest.raises(sqlite3.OperationalError):
        hs.HistoryStore(str(db))
    assert db.read_bytes() == before                       # 옮기지 않았다
    assert not list(tmp_path.glob("history.db.corrupt-*"))
    assert hs.HistoryStore.recovered_from == ""


def test_readonly_db_file_keeps_its_cases(tmp_path):
    from storage.history_store import HistoryStore
    db = tmp_path / "history.db"
    _store_with_cases(str(db), 5)
    os.chmod(db, stat.S_IRUSR)
    try:
        HistoryStore.recovered_from = ""
        try:
            with HistoryStore(str(db)) as store:
                assert len(store.list_cases()) == 5
        except sqlite3.OperationalError:
            pass   # 읽기 전용이라 열지 못하는 것은 허용 - 옮기지만 않으면 된다
        assert not list(tmp_path.glob("history.db.corrupt-*"))
        assert HistoryStore.recovered_from == ""
    finally:
        os.chmod(db, stat.S_IRUSR | stat.S_IWUSR)


def _corrupt_data_pages(db: Path) -> None:
    """헤더(1페이지)는 두고 뒤쪽 데이터 페이지를 쓰레기로 덮는다."""
    data = bytearray(db.read_bytes())
    page = struct.unpack(">H", data[16:18])[0] or 4096
    assert len(data) > page * 3, "테스트 DB가 너무 작다"
    for off in range(page * 2, len(data), page):
        data[off + 8:off + page - 8] = b"\xa5" * (page - 16)
    db.write_bytes(bytes(data))


def test_data_page_corruption_is_caught_at_open(tmp_path):
    from storage.history_store import HistoryStore
    db = tmp_path / "history.db"
    _store_with_cases(str(db), 60)
    _corrupt_data_pages(db)
    HistoryStore.recovered_from = ""
    with HistoryStore(str(db)) as store:        # 예전엔 열기는 되고 목록 조회에서 죽었다
        assert store.list_cases() == []
    assert "quick_check" in HistoryStore.recovered_from or "malformed" in HistoryStore.recovered_from
    assert list(tmp_path.glob("history.db.corrupt-*"))
    HistoryStore.recovered_from = ""


def test_list_query_corruption_goes_through_recovery(tmp_path, monkeypatch):
    from storage.history_store import HistoryStore
    db = tmp_path / "history.db"
    _store_with_cases(str(db), 3)
    HistoryStore.recovered_from = ""
    store = HistoryStore(str(db))
    real_execute = store._conn.execute
    calls = {"n": 0}

    def flaky(sql, *args):
        if sql.lstrip().upper().startswith("SELECT * FROM CASES") and calls["n"] == 0:
            calls["n"] += 1
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_execute(sql, *args)

    # sqlite3.Connection은 속성을 바꿀 수 없어 execute만 바꾼 대역으로 감싼다
    store._conn = type("Conn", (), {"execute": staticmethod(flaky), "close": store._conn.close,
                                    "commit": store._conn.commit})()
    assert store.list_cases() == []           # 복구 뒤 빈 DB에서 다시 조회
    assert "malformed" in HistoryStore.recovered_from
    assert list(tmp_path.glob("history.db.corrupt-*"))
    store.close()
    HistoryStore.recovered_from = ""


def test_operational_error_on_query_is_not_recovery(tmp_path):
    from storage import history_store as hs
    assert not hs._is_corruption(sqlite3.OperationalError("database is locked"))
    assert not hs._is_corruption(sqlite3.OperationalError("attempt to write a readonly database"))
    assert hs._is_corruption(sqlite3.DatabaseError("file is not a database"))
    assert hs._is_corruption(sqlite3.DatabaseError("database disk image is malformed"))
    assert hs._is_corruption(sqlite3.DatabaseError("quick_check 실패: *** in database main ***"))


# ---------------------------------------------------------------------------
# #58: 데이터 폴더 이전
# ---------------------------------------------------------------------------

@pytest.fixture
def data_base(tmp_path, monkeypatch):
    from core import appinfo
    base = tmp_path / "AppData"
    base.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.setenv("XDG_DATA_HOME", str(base))
    monkeypatch.setattr(appinfo, "_migrated", False)
    monkeypatch.setattr(appinfo, "MIGRATION_NOTICE", "")
    yield base
    appinfo._migrated = False
    appinfo.MIGRATION_NOTICE = ""


def _legacy_db(folder: Path, n: int = 2) -> None:
    from storage.history_store import CaseRecord, HistoryStore
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cases").mkdir(exist_ok=True)
    store = HistoryStore(str(folder / "history.db"))
    for i in range(n):
        (folder / "cases" / f"case{i}").mkdir()
        store.add_case(CaseRecord(id=None, case_number=f"L-{i}", output_folder=str(folder / "cases" / f"case{i}"),
                                  source_video_path=str(folder / "cases" / f"case{i}" / "source" / "a.mp4")))
    store.close()


def test_retries_migration_when_new_folder_is_effectively_empty(data_base):
    from core import appinfo
    from storage.history_store import HistoryStore
    old = data_base / "GPSTracer"
    _legacy_db(old, 2)
    new = data_base / "IDAS"
    new.mkdir()
    (new / "cases").mkdir()
    HistoryStore(str(new / "history.db")).close()      # 이전 실행이 옮기지 못하고 남긴 빈 DB
    (new / "settings.json").write_text('{"map_mode": "offline"}')
    assert appinfo.migrate_legacy_app_data() == str(old)
    assert not old.exists()
    assert (new / "settings.json").read_text() == '{"map_mode": "offline"}'
    with HistoryStore(str(new / "history.db")) as store:
        cases = store.list_cases()
    assert len(cases) == 2
    assert all(c.output_folder.startswith(str(new / "cases")) for c in cases)
    assert all(c.source_video_path.startswith(str(new / "cases")) for c in cases)
    assert (new / "cases" / "case0").is_dir()
    assert appinfo.MIGRATION_NOTICE == ""


def test_rewrites_old_prefix_paths_even_when_folder_was_moved_earlier(data_base):
    """옛 빌드가 이름만 바꿔 둔 PC: GPSTracer 폴더는 없고 DB에는 옛 경로가 남아 있다."""
    from core import appinfo
    from storage.history_store import HistoryStore
    new = data_base / "IDAS"
    _legacy_db(new, 1)
    old_prefix = str(data_base / "GPSTracer")
    conn = sqlite3.connect(new / "history.db")
    conn.execute("UPDATE cases SET output_folder = ?, report_pdf_path = ?",
                 (os.path.join(old_prefix, "cases", "case0"), os.path.join(old_prefix, "cases", "case0", "report.pdf")))
    conn.commit()
    conn.close()
    assert appinfo.migrate_legacy_app_data() == ""
    with HistoryStore(str(new / "history.db")) as store:
        case = store.list_cases()[0]
    assert case.output_folder == str(new / "cases" / "case0")
    assert case.report_pdf_path == str(new / "cases" / "case0" / "report.pdf")


def test_does_not_merge_into_a_new_folder_that_already_has_cases(data_base):
    from core import appinfo
    old = data_base / "GPSTracer"
    _legacy_db(old, 1)
    new = data_base / "IDAS"
    _legacy_db(new, 1)
    assert appinfo.migrate_legacy_app_data() == ""
    assert old.is_dir() and (old / "history.db").is_file()
    assert "옮기지 않았습니다" in appinfo.MIGRATION_NOTICE


def test_migration_failure_is_reported(data_base, monkeypatch):
    from core import appinfo
    old = data_base / "GPSTracer"
    _legacy_db(old, 1)

    def fail(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(appinfo.os, "rename", fail)
    assert appinfo.migrate_legacy_app_data() == ""
    assert "옮기지 못했습니다" in appinfo.MIGRATION_NOTICE and "permission denied" in appinfo.MIGRATION_NOTICE
    assert old.is_dir()


# ---------------------------------------------------------------------------
# #49 #53 #106 #131: 전후방 대조
# ---------------------------------------------------------------------------

def _fake_mp4(path: Path, size: int, tag: bytes) -> None:
    body = bytearray(size)
    body[0:12] = struct.pack(">I4s4s", size, b"ftyp", b"isom")
    body[-len(tag):] = tag
    path.write_bytes(bytes(body))


@pytest.fixture
def quick_probe(monkeypatch):
    """컨테이너 길이·서명 읽기를 건너뛰어 대조 로직만 본다."""
    from core import video_pairs
    from core.format_sniffer import RoutingResult
    import core.format_sniffer as sniffer
    import core.duration as duration
    monkeypatch.setattr(sniffer, "sniff", lambda p: RoutingResult("mp4", True, ""))
    monkeypatch.setattr(duration, "get_duration_sec", lambda *a, **k: 60.0)
    monkeypatch.setattr(duration, "mp4_recorded_at_epoch", lambda p: None)
    monkeypatch.setattr(video_pairs, "device_signature", lambda p, c: {"container": "mp4"})
    return video_pairs


def test_same_size_different_sample_skips_full_hash(tmp_path, quick_probe, monkeypatch):
    from core import hashing
    front, rear = tmp_path / "A_F.mp4", tmp_path / "A_R.mp4"
    _fake_mp4(front, 3 * 1024 * 1024 + 7, b"front")
    _fake_mp4(rear, 3 * 1024 * 1024 + 7, b"rear!")

    def boom(*_a, **_k):
        raise AssertionError("full hash must not run when samples differ")

    monkeypatch.setattr(hashing, "sha256_file", boom)
    check = quick_probe.compare_pair(str(front), str(rear))
    assert not any("SHA-256" in p for p in check.problems)


def test_identical_files_full_hash_is_cancellable(tmp_path, quick_probe):
    front, rear = tmp_path / "A_F.mp4", tmp_path / "A_R.mp4"
    _fake_mp4(front, 4 * 1024 * 1024, b"same")
    rear.write_bytes(front.read_bytes())
    cancel = threading.Event()
    cancel.set()
    check = quick_probe.compare_pair(str(front), str(rear), cancel_event=cancel)
    assert check.cancelled and not check.ok
    check = quick_probe.compare_pair(str(front), str(rear))
    assert any("SHA-256" in p for p in check.problems)


def test_full_check_stops_before_engine_when_quick_check_fails(tmp_path, quick_probe, monkeypatch):
    from engine import engine_adapter
    front, rear = tmp_path / "20240618_184124_F.mp4", tmp_path / "20240618_190000_R.mp4"
    _fake_mp4(front, 4096, b"f")
    _fake_mp4(rear, 8192, b"r")
    monkeypatch.setattr(engine_adapter, "run_full_extraction",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("engine must not run")))
    check = quick_probe.compare_pair_full(str(front), str(rear), workdir=str(tmp_path / "w"))
    assert check.problems and any("파일명의 시각" in p for p in check.problems)


def _points(times, lat=37.5, lon=127.0, utc=True, elapsed=False, abs_time=False, day="2024-06-18"):
    from engine.engine_adapter import TrackPoint
    out = []
    for t in times:
        p = TrackPoint(start_time_sec=float(t), latitude=lat, longitude=lon, speed_kmh=30.0)
        if utc:
            p.gps_date, p.gps_utc_time = day, f"10:00:{t:02d}"
        else:
            p.gps_status = "A"
            if elapsed:
                p.gps_elapsed = float(t)
            if abs_time:
                p.gps_abs_time = f"{day} 10:00:{t:02d}"
        out.append(p)
    return out


def _result(points, status="ok"):
    from core.format_sniffer import RoutingResult
    from engine.engine_adapter import ExtractionResult
    return ExtractionResult(RoutingResult("mp4", True, ""), points, [], "", status=status)


def test_gps_untrusted_goes_to_time_comparison_not_failure(tmp_path, quick_probe, monkeypatch):
    """#53: 측위 전(V) 기록만 있는 정상 영상은 '분석 실패'가 아니다."""
    from engine import engine_adapter
    front, rear = tmp_path / "A_F.mp4", tmp_path / "A_R.mp4"
    _fake_mp4(front, 4096, b"f")
    _fake_mp4(rear, 8192, b"r")
    v_rows = _points(range(10), lat=None, lon=None)
    for p in v_rows:
        p.latitude = p.longitude = None
    monkeypatch.setattr(engine_adapter, "run_full_extraction",
                        lambda *a, **k: _result(_points(range(10), lat=None, lon=None), status="gps_untrusted"))
    check = quick_probe.compare_pair_full(str(front), str(rear), workdir=str(tmp_path / "w"))
    assert check.gps_checked
    assert not any("분석 실패" in p for p in check.problems), check.problems
    assert check.ok, check.problems


def test_finevu_renamed_copy_compares_by_elapsed_seconds():
    """#106: 파일명 시각이 없는 사본은 경과 초로 대조한다."""
    from core import video_pairs
    check = video_pairs.PairCheck("f", "r")
    video_pairs.compare_gps(check, _points(range(10), utc=False, elapsed=True),
                            _points(range(10), utc=False, elapsed=True))
    assert check.ok, check.problems
    assert check.gps_time_basis == video_pairs.BASIS_ELAPSED and check.matched_count == 10
    check = video_pairs.PairCheck("f", "r")
    video_pairs.compare_gps(check, _points(range(10), utc=False, elapsed=True),
                            _points(range(30, 40), utc=False, elapsed=True))
    assert any("경과 0초" in p and "경과 30초" in p for p in check.problems), check.problems


def test_finevu_filename_time_is_shown_as_local_not_shifted_nine_hours():
    from core import video_pairs
    check = video_pairs.PairCheck("f", "r")
    video_pairs.compare_gps(check, _points(range(10), utc=False, abs_time=True, elapsed=True),
                            _points(range(30, 40), utc=False, abs_time=True, elapsed=True))
    assert check.gps_time_basis == video_pairs.BASIS_FILENAME
    msg = next(p for p in check.problems if "GPS 시각이 다릅니다" in p)
    assert "2024-06-18 10:00:00 (파일명 기준)" in msg and "2024-06-18 10:00:30 (파일명 기준)" in msg
    assert "19:00" not in msg
    assert any("파일명 시각" in n for n in check.notes)


def test_utc_display_still_converts_to_kst():
    from core import video_pairs
    check = video_pairs.PairCheck("f", "r")
    video_pairs.compare_gps(check, _points(range(10)), _points(range(30, 40)))
    msg = next(p for p in check.problems if "GPS 시각이 다릅니다" in p)
    assert "19:00:00" in msg and "UTC+9" in msg


def test_damaged_front_gets_a_result_code_for_the_ui(tmp_path, quick_probe, monkeypatch):
    """#131: 손상된 전방은 '분석 실패'가 아니라 '대조 불가(손상)' 코드로 돌려준다."""
    from core import damage
    from engine import engine_adapter
    front, rear = tmp_path / "A_F.mp4", tmp_path / "A_R.mp4"
    _fake_mp4(front, 4096, b"f")
    _fake_mp4(rear, 8192, b"r")

    def extraction(path, *_a, **_k):
        if path == str(front):
            return _result([], status="engine_failed")
        return _result(_points(range(10)))

    monkeypatch.setattr(engine_adapter, "run_full_extraction", extraction)
    report = damage.DamageReport(path=str(front), container="mp4", header_missing=True, fragmented=True)
    monkeypatch.setattr(damage, "scan_damage", lambda p, cancel_event=None: report)
    check = quick_probe.compare_pair_full(str(front), str(rear), workdir=str(tmp_path / "w"))
    assert check.code == "damaged" and "전방" in check.damaged_files
    assert any("손상" in p for p in check.problems) and not check.ok
    # 손상이 아닌 실패는 그대로 실패
    monkeypatch.setattr(damage, "scan_damage", lambda p, cancel_event=None: damage.DamageReport(path=p, container="mp4"))
    check = quick_probe.compare_pair_full(str(front), str(rear), workdir=str(tmp_path / "w2"))
    assert check.code == "engine_failed" and any("분석 실패" in p for p in check.problems)


# ---------------------------------------------------------------------------
# #103 #104: 슬랙
# ---------------------------------------------------------------------------

def test_slack_recording_is_not_split_at_utc_midnight():
    from core.slack import build_slack_set
    from engine.engine_adapter import TrackPoint
    raw = [TrackPoint(latitude=37.5, longitude=127.0, speed_kmh=5, gps_date="2024-06-18", gps_utc_time="23:59:58"),
           TrackPoint(latitude=37.5001, longitude=127.0, speed_kmh=5, gps_date="2024-06-18", gps_utc_time="23:59:59"),
           TrackPoint(latitude=37.5002, longitude=127.0, speed_kmh=5, gps_date="2024-06-19", gps_utc_time="00:00:00"),
           TrackPoint(latitude=37.5003, longitude=127.0, speed_kmh=5, gps_date="2024-06-19", gps_utc_time="00:00:01")]
    slack = build_slack_set(raw)
    assert slack.recordings == 1
    assert {p.segment_index for p in slack.points} == {0}
    raw2 = raw[:2] + [TrackPoint(latitude=37.6, longitude=127.0, speed_kmh=5, gps_date="2024-06-19", gps_utc_time="00:05:00")]
    assert build_slack_set(raw2).recordings == 2       # 2분 넘는 공백은 여전히 다른 녹화


def test_untimed_slack_rows_with_zero_coords_are_outliers():
    from core.slack import build_slack_set
    from engine.engine_adapter import TrackPoint
    raw = [TrackPoint(latitude=37.5, longitude=127.0, speed_kmh=5, gps_date="2024-06-18", gps_utc_time="01:00:00"),
           TrackPoint(latitude=0.0, longitude=0.0, speed_kmh=0, gps_status="V"),
           TrackPoint(latitude=37.5, longitude=127.0, speed_kmh=5, gps_status="A")]
    slack = build_slack_set(raw)
    zero = next(p for p in slack.points if p.latitude == 0.0)
    assert zero.is_outlier and not zero.has_fix
    assert "좌표 2개" in slack.summary
    assert zero not in slack.map_points


# ---------------------------------------------------------------------------
# #105: AVI 트랙 판별
# ---------------------------------------------------------------------------

def _chunk(cid: bytes, payload: bytes) -> bytes:
    return cid + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def _list(ltype: bytes, payload: bytes) -> bytes:
    return _chunk(b"LIST", ltype + payload)


def _avi_bytes(video_streams: int, riff_size=None, pad_zero: int = 0) -> bytes:
    strh_v = _chunk(b"strh", b"vids" + b"H264" + b"\0" * 48)
    strh_a = _chunk(b"strh", b"auds" + b"\0" * 52)
    strl = b"".join(_list(b"strl", strh_v + _chunk(b"strf", b"\0" * 40)) for _ in range(video_streams))
    strl += _list(b"strl", strh_a + _chunk(b"strf", b"\0" * 16))
    hdrl = _list(b"hdrl", _chunk(b"avih", b"\0" * 56) + strl)
    movi = _list(b"movi", _chunk(b"00dc", b"\xaa" * 100))
    body = b"AVI " + hdrl + (b"\0" * pad_zero) + movi
    size = len(body) if riff_size is None else riff_size
    return b"RIFF" + struct.pack("<I", size) + body


def test_two_track_avi_with_zero_riff_size_is_still_two_tracks(tmp_path):
    from core.video_tracks import count_video_tracks
    good, cut = tmp_path / "ok.avi", tmp_path / "cut.avi"
    good.write_bytes(_avi_bytes(2))
    cut.write_bytes(_avi_bytes(2, riff_size=0))
    assert count_video_tracks(str(good)) == 2
    assert count_video_tracks(str(cut)) == 2
    single = tmp_path / "one.avi"
    single.write_bytes(_avi_bytes(1, riff_size=0))
    assert count_video_tracks(str(single)) == 1


def test_zero_region_before_header_is_abandoned_quickly(tmp_path):
    from core.video_tracks import count_video_tracks
    path = tmp_path / "zeros.avi"
    body = b"AVI " + b"\0" * (32 * 1024 * 1024)
    path.write_bytes(b"RIFF" + struct.pack("<I", 0) + body)
    started = time.perf_counter()
    assert count_video_tracks(str(path)) == 0
    assert time.perf_counter() - started < 0.3


def test_mixed_original_and_renamed_finevu_pair_uses_common_elapsed_basis():
    """전방은 원본 이름(파일명 시각 있음), 후방은 이름을 바꾼 사본(경과 초만) - 공통 기준인 경과 초로 대조."""
    from core import video_pairs
    check = video_pairs.PairCheck("f", "r")
    video_pairs.compare_gps(check, _points(range(10), utc=False, abs_time=True, elapsed=True),
                            _points(range(10), utc=False, elapsed=True))
    assert check.ok, check.problems
    assert check.gps_time_basis == video_pairs.BASIS_ELAPSED
    assert video_pairs.gps_record_basis(_points(range(3))) == video_pairs.BASIS_UTC
    assert video_pairs.gps_record_basis([]) == ""
