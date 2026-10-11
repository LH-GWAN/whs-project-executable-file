from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from core.appinfo import app_data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number TEXT NOT NULL,
    examiner TEXT,
    memo TEXT,
    source_video_path TEXT,
    source_video_filename TEXT,
    source_video_size_bytes INTEGER,
    source_video_sha256 TEXT,
    duration_sec REAL,
    detected_format TEXT,
    avi_repaired INTEGER DEFAULT 0,
    output_folder TEXT,
    analysis_settings_json TEXT,
    created_at TEXT NOT NULL,
    last_opened_at TEXT,
    report_pdf_path TEXT
);

CREATE TABLE IF NOT EXISTS engine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    script_name TEXT NOT NULL,
    argv_json TEXT,
    exit_code INTEGER,
    stdout_log_path TEXT,
    started_at TEXT,
    finished_at TEXT
);
"""


def default_app_data_dir() -> str:
    return app_data_dir()


@dataclass
class CaseRecord:
    id: Optional[int]
    case_number: str
    examiner: str = ""
    memo: str = ""
    source_video_path: str = ""
    source_video_filename: str = ""
    source_video_size_bytes: int = 0
    source_video_sha256: str = ""
    duration_sec: Optional[float] = None
    detected_format: str = ""
    avi_repaired: bool = False
    output_folder: str = ""
    analysis_settings: Dict = field(default_factory=dict)
    created_at: str = ""
    last_opened_at: Optional[str] = None
    report_pdf_path: Optional[str] = None
    rear_video_filename: str = ""   # 후방 영상 사본 이름(source/ 안). 없으면 빈 문자열
    # 파일 하나에 전·후방 트랙이 든 영상을 어떻게 볼지(core/video_tracks.TRACK_MODES). 빈 문자열이면 해당 없음
    track_mode: str = ""
    # 연속 영상 이어보기의 구간 목록(core/pipeline.segment_record 형식). 하나짜리 사건은 빈 목록
    segments: List[Dict] = field(default_factory=list)
    # 분석 상태(ExtractionResult.status). "analyzing"은 분석 도중 프로세스가 죽은 사건이다.
    analysis_status: str = ""
    # 엔진 산출물 manifest(engine_output/manifest.sha256)의 SHA-256. 분석 직후 고정해 두고 다시 열 때 대조한다 -
    # manifest를 지우거나 다시 만들면 '옛 사건'으로 통과하던 구멍을 막는다(리뷰 2026-10-11 #19). 옛 사건은 빈 문자열.
    manifest_sha256: str = ""
    # 손상 파일 처리 결과(core/pipeline.apply_damage dict)의 DB 사본. case.json이 깨져도 손상 표시를 잃지 않게(리뷰 #82).
    damage: Optional[Dict] = None


class HistoryStore:
    # 손상된 DB를 비켜 두고 새로 시작했을 때 그 경로. main_window가 시작 때 한 번 알린다(리뷰 #67).
    recovered_from: str = ""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(default_app_data_dir(), "history.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        try:
            self._open()
        except sqlite3.DatabaseError as exc:
            # 잠김·읽기 전용·권한 없음(OperationalError)은 손상이 아니다 - 정상 DB를 옮겨 버리면 사건
            # 이력이 통째로 사라진다(리뷰 #57). 그대로 올려 호출하는 쪽이 '이력 없이 계속'을 안내한다.
            self._close_quietly()
            if not _is_corruption(exc):
                raise
            # 쓰레기 바이트·잘린 파일·데이터 페이지 손상이면 실행할 때마다 시작에 실패했다(리뷰 #67).
            # 손상 파일을 옮기고 빈 DB로 시작한다. 사건 폴더(cases/)는 그대로 있으니 증거는 잃지 않는다.
            self._recover(exc)

    def _open(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # 헤더만 멀쩡하고 데이터 페이지가 손상된 DB는 열기는 되고 목록 조회에서 죽어 메인 창을 못 만들었다
        # (리뷰 #57). 페이지 전체를 읽는 quick_check로 열 때 가려낸다(이력 DB는 몇 MB라 금방 끝난다).
        verdict = self._conn.execute("PRAGMA quick_check").fetchone()
        if not verdict or str(verdict[0]).lower() != "ok":
            raise sqlite3.DatabaseError(f"quick_check 실패: {verdict[0] if verdict else '결과 없음'}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _close_quietly(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _recover(self, exc: BaseException) -> None:
        """손상된 DB를 옆에 옮겨 두고 빈 DB로 다시 연다. 옮기지 못하면(권한·잠김) 예외를 올린다."""
        self._close_quietly()
        aside = f"{self.db_path}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
        os.replace(self.db_path, aside)
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass
        self._open()
        HistoryStore.recovered_from = f"{aside} ({type(exc).__name__}: {exc})"

    def _fetchall(self, sql: str, params=()) -> list:
        """조회 중 손상이 드러나면(열 때는 통과한 페이지) 복구 경로로 보내고 빈 DB에서 다시 조회한다(리뷰 #57)."""
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as exc:
            if not _is_corruption(exc):
                raise
            self._recover(exc)
            return self._conn.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params=()):
        rows = self._fetchall(sql, params)
        return rows[0] if rows else None

    def _migrate(self) -> None:
        """예전 DB에 없는 컬럼을 보탠다. CREATE TABLE IF NOT EXISTS 는 기존 표를 안 바꾼다."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(cases)")}
        if "rear_video_filename" not in existing:
            self._conn.execute("ALTER TABLE cases ADD COLUMN rear_video_filename TEXT")
        if "track_mode" not in existing:
            self._conn.execute("ALTER TABLE cases ADD COLUMN track_mode TEXT")
        if "segments_json" not in existing:
            self._conn.execute("ALTER TABLE cases ADD COLUMN segments_json TEXT")
        if "analysis_status" not in existing:
            self._conn.execute("ALTER TABLE cases ADD COLUMN analysis_status TEXT")
        if "manifest_sha256" not in existing:
            self._conn.execute("ALTER TABLE cases ADD COLUMN manifest_sha256 TEXT")
        if "damage_json" not in existing:
            self._conn.execute("ALTER TABLE cases ADD COLUMN damage_json TEXT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def add_case(self, case: CaseRecord) -> int:
        cur = self._conn.execute(
            """INSERT INTO cases (
                case_number, examiner, memo, source_video_path, source_video_filename,
                source_video_size_bytes, source_video_sha256, duration_sec, detected_format,
                avi_repaired, output_folder, analysis_settings_json, created_at,
                last_opened_at, report_pdf_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case.case_number, case.examiner, case.memo, case.source_video_path,
                case.source_video_filename, case.source_video_size_bytes, case.source_video_sha256,
                case.duration_sec, case.detected_format, int(case.avi_repaired), case.output_folder,
                json.dumps(case.analysis_settings, ensure_ascii=False),
                case.created_at or datetime.now().isoformat(timespec="seconds"),
                case.last_opened_at, case.report_pdf_path,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def add_engine_run(self, case_id: int, script_name: str, argv: List[str], exit_code: int,
                        stdout_log_path: str, started_at: str, finished_at: str) -> int:
        cur = self._conn.execute(
            """INSERT INTO engine_runs (
                case_id, script_name, argv_json, exit_code, stdout_log_path, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (case_id, script_name, json.dumps(argv, ensure_ascii=False), exit_code,
             stdout_log_path, started_at, finished_at),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_case(self, case_id: int) -> Optional[CaseRecord]:
        row = self._fetchone("SELECT * FROM cases WHERE id = ?", (case_id,))
        return _row_to_case(row) if row else None

    def list_cases(self, search: Optional[str] = None) -> List[CaseRecord]:
        if search:
            like = f"%{search}%"
            rows = self._fetchall(
                """SELECT * FROM cases
                   WHERE case_number LIKE ? OR examiner LIKE ?
                   ORDER BY created_at DESC""",
                (like, like),
            )
        else:
            rows = self._fetchall("SELECT * FROM cases ORDER BY created_at DESC")
        return [_row_to_case(r) for r in rows]

    def find_cases_by_sha256(self, sha256: str) -> List[CaseRecord]:
        """같은 파일(내용 기준)을 분석한 이력. 같은 파일을 다시 올렸는지 확인할 때 쓴다."""
        if not sha256:
            return []
        rows = self._fetchall(
            "SELECT * FROM cases WHERE source_video_sha256 = ? ORDER BY created_at DESC",
            (sha256,),
        )
        return [_row_to_case(r) for r in rows]

    def touch_last_opened(self, case_id: int) -> None:
        self._conn.execute(
            "UPDATE cases SET last_opened_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), case_id),
        )
        self._conn.commit()

    def update_case_extraction(self, case_id: int, duration_sec: Optional[float],
                                avi_repaired: bool, output_folder: str) -> None:
        self._conn.execute(
            """UPDATE cases SET duration_sec = ?, avi_repaired = ?, output_folder = ?
               WHERE id = ?""",
            (duration_sec, int(avi_repaired), output_folder, case_id),
        )
        self._conn.commit()

    def delete_case(self, case_id: int) -> None:
        self._conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
        self._conn.commit()

    def update_case_info(self, case_id: int, case_number: str, examiner: str, memo: str) -> None:
        """사건번호·담당자·메모 수정. 폴더 이름과 분석 결과는 그대로 둔다."""
        self._conn.execute(
            "UPDATE cases SET case_number = ?, examiner = ?, memo = ? WHERE id = ?",
            (case_number, examiner, memo, case_id),
        )
        self._conn.commit()

    def set_rear_video(self, case_id: int, filename: str) -> None:
        self._conn.execute("UPDATE cases SET rear_video_filename = ? WHERE id = ?", (filename, case_id))
        self._conn.commit()

    def set_track_mode(self, case_id: int, mode: str) -> None:
        self._conn.execute("UPDATE cases SET track_mode = ? WHERE id = ?", (mode, case_id))
        self._conn.commit()

    def set_analysis_status(self, case_id: int, status: str) -> None:
        self._conn.execute("UPDATE cases SET analysis_status = ? WHERE id = ?", (status, case_id))
        self._conn.commit()

    def set_output_folder(self, case_id: int, output_folder: str) -> None:
        """사건 폴더를 만들자마자 기록한다 - 분석 중 프로세스가 죽어도 폴더를 찾아 지울 수 있게(리뷰 #31)."""
        self._conn.execute("UPDATE cases SET output_folder = ? WHERE id = ?", (output_folder, case_id))
        self._conn.commit()

    def set_manifest_sha256(self, case_id: int, digest: str) -> None:
        self._conn.execute("UPDATE cases SET manifest_sha256 = ? WHERE id = ?", (digest or "", case_id))
        self._conn.commit()

    def set_damage(self, case_id: int, damage: Optional[Dict]) -> None:
        """손상 처리 결과를 DB에도 남긴다(None이면 비움)."""
        self._conn.execute("UPDATE cases SET damage_json = ? WHERE id = ?",
                           (json.dumps(damage, ensure_ascii=False) if damage else None, case_id))
        self._conn.commit()

    def set_source_filename(self, case_id: int, filename: str) -> None:
        """source/ 안 사본 이름이 확정된 뒤(긴 경로라 줄였을 때) DB에 반영한다(리뷰 #96·#117)."""
        self._conn.execute("UPDATE cases SET source_video_filename = ? WHERE id = ?", (filename, case_id))
        self._conn.commit()

    def set_segments(self, case_id: int, segments: List[Dict]) -> None:
        self._conn.execute("UPDATE cases SET segments_json = ? WHERE id = ?",
                           (json.dumps(segments, ensure_ascii=False), case_id))
        self._conn.commit()

    def set_report_path(self, case_id: int, report_pdf_path: str) -> None:
        self._conn.execute(
            "UPDATE cases SET report_pdf_path = ? WHERE id = ?", (report_pdf_path, case_id),
        )
        self._conn.commit()


_CORRUPTION_MARKERS = ("malformed", "not a database", "corrupt", "quick_check")


def _is_corruption(exc: BaseException) -> bool:
    """SQLITE_NOTADB·SQLITE_CORRUPT(둘 다 기본 DatabaseError)와 quick_check 실패만 손상으로 본다.
    잠김·읽기 전용·열 수 없음은 OperationalError라 손상이 아니다(리뷰 #57)."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    if type(exc) is sqlite3.DatabaseError:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _CORRUPTION_MARKERS)


def _row_to_case(row: sqlite3.Row) -> CaseRecord:
    return CaseRecord(
        id=row["id"],
        case_number=row["case_number"],
        examiner=row["examiner"] or "",
        memo=row["memo"] or "",
        source_video_path=row["source_video_path"] or "",
        source_video_filename=row["source_video_filename"] or "",
        source_video_size_bytes=row["source_video_size_bytes"] or 0,
        source_video_sha256=row["source_video_sha256"] or "",
        duration_sec=row["duration_sec"],
        detected_format=row["detected_format"] or "",
        avi_repaired=bool(row["avi_repaired"]),
        output_folder=row["output_folder"] or "",
        analysis_settings=_load_json_dict(row["analysis_settings_json"]),
        created_at=row["created_at"] or "",
        last_opened_at=row["last_opened_at"],
        report_pdf_path=row["report_pdf_path"],
        rear_video_filename=(row["rear_video_filename"] or "") if "rear_video_filename" in row.keys() else "",
        track_mode=(row["track_mode"] or "") if "track_mode" in row.keys() else "",
        segments=_load_segments(row),
        analysis_status=(row["analysis_status"] or "") if "analysis_status" in row.keys() else "",
        manifest_sha256=(row["manifest_sha256"] or "") if "manifest_sha256" in row.keys() else "",
        damage=_load_damage(row),
    )


def _load_json_dict(text: Optional[str]) -> Dict:
    """깨진 JSON 행 하나가 전체 목록 조회를 막지 않게 빈 값으로 읽는다(리뷰 #67)."""
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _load_damage(row: sqlite3.Row) -> Optional[Dict]:
    if "damage_json" not in row.keys() or not row["damage_json"]:
        return None
    value = _load_json_dict(row["damage_json"])
    return value or None


def _load_segments(row: sqlite3.Row) -> List[Dict]:
    if "segments_json" not in row.keys() or not row["segments_json"]:
        return []
    try:
        value = json.loads(row["segments_json"])
    except ValueError:
        return []
    return value if isinstance(value, list) else []
