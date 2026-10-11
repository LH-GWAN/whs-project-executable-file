from __future__ import annotations

import glob
import hashlib
import inspect
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Dict, List, Optional

from core import driving_events, duration as duration_mod, format_sniffer, hashing, outliers
from core.driving_events import DrivingEvent
from core.format_sniffer import RoutingResult
from engine.engine_adapter import (
    CancelledError,
    ExtractionResult,
    TrackPoint,
    load_existing_results,
    load_slack_points,
    _collect_warnings,
    STATUS_GPS_UNTRUSTED,
    detect_slack,
    run_full_extraction,
)
from storage.history_store import CaseRecord, HistoryStore

ProgressCallback = Optional[Callable[[str], None]]


@dataclass
class PipelineResult:
    case_id: int
    case_folder: str
    source_copy_path: str
    extraction: ExtractionResult
    duration_sec: Optional[float]
    driving_events: List[DrivingEvent]
    sha256: str
    vehicle_type: str   # 위험운전 판별 기준 차종(core/driving_events.VEHICLE_*)
    rear_copy_path: str = ""   # 후방 영상 사본(같이 보기로 올린 경우). 없으면 빈 문자열
    track_mode: str = ""       # 파일 하나에 전·후방 트랙이 든 영상의 보기 방식(both/front/rear)
    rear_sha256: str = ""      # 후방 원본 해시(사본과 대조를 마친 값)
    analyzed_at: str = ""      # 분석(추출) 시각 - 이 PC의 현지 시각 ISO 문자열
    # 다시 열 때 엔진 산출물 manifest 대조 결과. None=manifest 없음(옛 사건·새 분석 직후).
    artifacts_verified: Optional[bool] = None
    # 연속 영상 이어보기의 구간들. 비어 있으면 영상 하나짜리 사건이다. 이어보기면 extraction·
    # driving_events·duration_sec는 구간들을 시간축으로 이어 붙인 값(Composed)이다.
    segments: List["SegmentResult"] = field(default_factory=list)
    # 헤더를 이식한 작업 사본(손상 파일). 있으면 Tracker는 이 파일을 튼다(원본 사본은 재생이 안 된다).
    repaired_path: str = ""
    # 이식 직후 잰 작업 사본의 SHA-256(case.json damage.repaired_sha256과 같다). 무결성 표시등이 재생 파일도
    # 대조할 수 있게 둔다 - 예전엔 이 사본의 해시가 어디에도 없어 바꿔치기해도 표시등이 초록이었다(리뷰 #42).
    repaired_sha256: str = ""
    # 다시 열 때 손상 정보의 출처: "case.json" / "db"(case.json이 깨져 DB 사본을 씀) / "" (손상 사건 아님).
    damage_source: str = ""

    @property
    def playback_path(self) -> str:
        return self.repaired_path if self.repaired_path and os.path.isfile(self.repaired_path) else self.source_copy_path

    @property
    def damage(self) -> Optional[dict]:
        return self.extraction.damage

    @property
    def points(self) -> List[TrackPoint]:
        return self.extraction.points

    @property
    def is_sequence(self) -> bool:
        return bool(self.segments)


@dataclass
class SegmentResult:
    """이어보기의 영상 하나. 시각·행 번호는 그 영상 기준(0초부터)이고, offset_sec만큼 뒤에 이어진다."""
    index: int
    front_copy_path: str
    rear_copy_path: str
    front_sha256: str
    rear_sha256: str
    track_mode: str
    duration_sec: Optional[float]
    offset_sec: float
    extraction: ExtractionResult
    driving_events: List[DrivingEvent]
    front_original: str = ""
    rear_original: str = ""

    @property
    def label(self) -> str:
        return f"video{self.index + 1}"

    @property
    def primary_copy_path(self) -> str:
        return self.front_copy_path or self.rear_copy_path

    @property
    def primary_sha256(self) -> str:
        return self.front_sha256 if self.front_copy_path else self.rear_sha256

    @property
    def primary_is_rear(self) -> bool:
        return not self.front_copy_path and bool(self.rear_copy_path)

    @property
    def points(self) -> List[TrackPoint]:
        return self.extraction.points


_COPY_CHUNK = 4 * 1024 * 1024


def _copy_cancellable(src: str, dst: str, cancel_event=None) -> None:
    """shutil.copy2 대신 청크 단위로 복사하며 취소를 확인한다. 큰 영상(수 GB)을 통째로 복사하는 동안
    취소가 먹지 않아 워커가 한참 돌았고, 그 사이 창을 닫으면 실행 중인 QThread가 파괴돼 abort됐다
    (리뷰 #15). 취소되면 쓰다 만 사본을 지우고 CancelledError."""
    try:
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError("분석이 취소되었습니다.")
                chunk = fin.read(_COPY_CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
        shutil.copystat(src, dst)
    except BaseException:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise


def _sha256_cancellable(path: str, cancel_event=None) -> str:
    def keep_going(_done: int, _total: int) -> bool:
        return cancel_event is None or not cancel_event.is_set()
    digest = hashing.sha256_file(path, progress_cb=keep_going)
    if not digest:
        raise CancelledError("분석이 취소되었습니다.")
    return digest


# 사본·엔진 출력 경로가 Windows MAX_PATH(260)에 걸리면 추출이 실패하거나 'GPS 없음'이 된다(리뷰 #66).
# 전체 경로가 이 길이를 넘을 것 같으면 사본 이름을 짧은 고정 이름으로 바꾼다(원본 이름은 case.json에 남는다).
_MAX_COPY_PATH = 200


def _copy_name(source_dir: str, name: str, short: str) -> str:
    path = os.path.join(source_dir, name)
    if len(path) + 40 > _MAX_COPY_PATH:   # 뒤에 engine_output/<stem>/TRACK…/coordinates.csv 가 붙는다
        return short + os.path.splitext(name)[1]
    return name


def _rollback_case(history_store: HistoryStore, case_id: int, case_folder: str) -> None:
    """실패·취소 때 사본 폴더와 레코드를 치운다. 폴더를 먼저(읽기 전용도 풀어서) 지우고, 단계마다 따로
    감싼다 - 예전엔 delete_case가 먼저라 DB 삭제가 실패하면(디스크 가득 참) 폴더가 남았고, 읽기 전용
    사본은 rmtree가 조용히 건너뛰었다(리뷰 #28). 폴더가 끝내 남으면(백신이 사본을 잡고 있는 등) 레코드를
    지우지 않고 '실패' 상태로 둔다 - 레코드를 지우면 고아 사본이 이력에서 사라진다(리뷰 2026-10-11 #99)."""
    from core.case_deletion import _rmtree
    try:
        if case_folder and os.path.isdir(case_folder):
            _rmtree(case_folder)
    except OSError:
        shutil.rmtree(case_folder, ignore_errors=True)
    if case_folder and os.path.isdir(case_folder):
        try:
            history_store.set_analysis_status(case_id, "engine_failed")
        except Exception:  # noqa: BLE001 - 원래 예외를 덮지 않는다
            pass
        return
    try:
        history_store.delete_case(case_id)
    except Exception:  # noqa: BLE001 - 원래 예외를 덮지 않는다
        pass


def _write_json_atomic(path: str, data: Dict) -> None:
    """임시 파일에 쓰고 fsync 후 os.replace - 쓰다 실패해도 기존 파일이 잘리지 않는다(리뷰 #29)."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".case-", suffix=".json.tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


MANIFEST_NAME = "manifest.sha256"
# case.json 가운데 다시 열 때 행을 '재현'하는 데 쓰는 필드. manifest에 `@case.json:<필드>` 항목으로 해시를 넣어
# case.json을 고쳐 추정 좌표를 '일치' 상태로 보이게 하는 길을 막는다(리뷰 2026-10-11 #19).
MANIFEST_CASE_FIELDS = ("damage",)


def _canonical_json_sha256(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def write_artifact_manifest(output_dir: str, case_fields: Optional[Dict] = None) -> str:
    """엔진 산출물(CSV·로그)의 SHA-256 목록을 engine_output/manifest.sha256 에 남긴다. 다시 열 때 대조해
    산출물이 바뀌었는지 알린다(리뷰 #92). case_fields(case.json의 재현 필드, 예: {"damage": …})를 주면
    `@case.json:damage  <해시>` 항목으로 함께 고정한다. 반환값: manifest 경로."""
    lines = []
    for root, _dirs, files in os.walk(output_dir):
        for name in sorted(files):
            if name == MANIFEST_NAME:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, output_dir).replace("\\", "/")
            try:
                lines.append(f"{hashing.sha256_file(full)}  {rel}")
            except OSError:
                continue
    for key in MANIFEST_CASE_FIELDS:
        if case_fields is not None and key in case_fields:
            lines.append(f"{_canonical_json_sha256(case_fields[key])}  @case.json:{key}")
    path = os.path.join(output_dir, MANIFEST_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def manifest_sha256(output_dir: str) -> str:
    """manifest 파일 자체의 SHA-256(DB에 고정해 둔다). 없으면 빈 문자열."""
    path = os.path.join(output_dir or "", MANIFEST_NAME)
    try:
        return hashing.sha256_file(path) if output_dir and os.path.isfile(path) else ""
    except OSError:
        return ""


def verify_artifact_manifest(output_dir: str, expected_manifest_sha256: str = "",
                             case_metadata: Optional[Dict] = None,
                             cancel_event=None) -> Optional[bool]:
    """manifest.sha256 대조. 없으면 None(옛 사건), 전부 일치하면 True, 하나라도 다르거나 빠지면 False.

    - 목록에 없는 파일이 engine_output 아래에 있으면 False - 다시 열 때는 폴더의 timeline.csv 가운데 fix가
      가장 많은 것을 고르므로, 끼워 넣은 CSV가 목록 밖이라고 통과하면 그 데이터가 화면에 나온다(리뷰 #19).
    - expected_manifest_sha256(DB에 고정한 값)이 있으면 manifest 자체도 대조한다. 값이 있는데 manifest가
      없으면 지운 것이므로 False.
    - `@case.json:<필드>` 항목은 case_metadata(case.json 내용)의 그 필드 해시와 대조한다.
    - cancel_event가 서면 CancelledError."""
    path = os.path.join(output_dir or "", MANIFEST_NAME)
    if not output_dir or not os.path.isfile(path):
        return False if expected_manifest_sha256 else None
    if expected_manifest_sha256 and manifest_sha256(output_dir) != expected_manifest_sha256:
        return False
    listed = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                digest, rel = line.split("  ", 1)
                if rel.startswith("@case.json:"):
                    key = rel[len("@case.json:"):]
                    if not isinstance(case_metadata, dict) or _canonical_json_sha256(case_metadata.get(key)) != digest:
                        return False
                    continue
                listed.add(rel)
                full = os.path.join(output_dir, rel)
                if not os.path.isfile(full) or _sha256_cancellable(full, cancel_event) != digest:
                    return False
        for root, _dirs, files in os.walk(output_dir):
            for name in files:
                if name == MANIFEST_NAME:
                    continue
                rel = os.path.relpath(os.path.join(root, name), output_dir).replace("\\", "/")
                if rel not in listed:
                    return False
    except (OSError, ValueError):
        return False
    return True


def _safe_case_folder_name(case_number: str, case_id: int) -> str:
    safe = "".join(c for c in case_number if c.isalnum() or c in "-_") or "case"
    return f"{safe[:48]}_{case_id}"


def run_analysis_pipeline(
    video_path: str,
    case_number: str,
    examiner: str,
    memo: str,
    settings: Dict,
    cases_root_dir: str,
    history_store: HistoryStore,
    vehicle_type: str = driving_events.DEFAULT_VEHICLE,
    carve_slack: bool = False,
    progress_cb: ProgressCallback = None,
    cancel_event=None,
    precomputed_sha256: str = "",
    rear_video_path: str = "",
    track_mode: str = "",
    damage_report=None,
    recovery_mode: str = "none",
    reference_path: str = "",
) -> PipelineResult:
    def report(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("분석이 취소되었습니다.")

    check_cancelled()
    analyzed_at = datetime.now().astimezone().isoformat(timespec="seconds")   # 시간대 포함(리뷰 #99)
    # 같은 파일인지 확인하느라 화면에서 이미 계산했으면 다시 읽지 않는다(큰 영상은 수 초).
    if precomputed_sha256:
        sha256 = precomputed_sha256
    else:
        report("파일 해시 계산 중 (SHA-256)...")
        sha256 = _sha256_cancellable(video_path, cancel_event)

    check_cancelled()
    report("파일 형식 확인 중...")
    routing = format_sniffer.sniff(video_path)

    if not routing.supported:
        raise ValueError(routing.reason)
    if not any(settings.get(key, True) for key in ("tracker", "speed", "location")):
        raise ValueError("분석 항목을 최소 한 개 선택하세요.")

    provisional = CaseRecord(
        id=None,
        case_number=case_number,
        examiner=examiner,
        memo=memo,
        source_video_path=video_path,
        source_video_filename=os.path.basename(video_path),
        source_video_size_bytes=os.path.getsize(video_path),
        source_video_sha256=sha256,
        detected_format=routing.container,
        analysis_settings=settings,
        created_at=analyzed_at,
    )
    case_id = history_store.add_case(provisional)

    case_folder = os.path.join(cases_root_dir, _safe_case_folder_name(case_number, case_id))
    source_dir = os.path.join(case_folder, "source")
    output_dir = os.path.join(case_folder, "engine_output")
    try:
        os.makedirs(case_folder, exist_ok=False)
    except BaseException:
        history_store.delete_case(case_id)
        raise  # 기존 폴더는 이 실행의 소유물이 아니므로 삭제하지 않는다.
    try:
        os.makedirs(source_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        # 분석 중 프로세스가 죽어도 폴더를 찾아 지우고 '분석 중 중단'으로 알 수 있게 바로 기록한다(리뷰 #31).
        history_store.set_output_folder(case_id, output_dir)
        history_store.set_analysis_status(case_id, "analyzing")

        check_cancelled()
        report("원본 영상을 사건 폴더로 복사 중 (무결성 보존)...")
        source_copy_path = os.path.join(source_dir, _copy_name(source_dir, os.path.basename(video_path), "front"))
        rear_copy_path = ""
        rear_sha256 = ""
        _copy_cancellable(video_path, source_copy_path, cancel_event)
        report("분석 사본 SHA-256 검증 중...")
        if _sha256_cancellable(source_copy_path, cancel_event) != sha256:
            raise ValueError("원본 해시와 분석 사본 SHA-256이 다릅니다. 분석을 중단했습니다.")
        check_cancelled()
        # 후방 영상은 분석하지 않고 같이 보기용으로만 보존한다. 이름이 전방과 같으면
        # (드물지만) 덮어쓰지 않도록 접두어를 붙인다.
        if rear_video_path:
            if not os.path.isfile(rear_video_path):
                raise FileNotFoundError("후방 원본 영상을 찾을 수 없습니다.")
            rear_sha256 = _sha256_cancellable(rear_video_path, cancel_event)
            check_cancelled()
            report("후방 영상을 사건 폴더로 복사 중...")
            rear_name = _copy_name(source_dir, os.path.basename(rear_video_path), "rear")
            # NTFS·APFS는 대소문자를 구분하지 않아 REC.MP4와 rec.mp4가 같은 파일이다(리뷰 #18).
            if rear_name.casefold() == os.path.basename(source_copy_path).casefold() \
                    or os.path.exists(os.path.join(source_dir, rear_name)):
                rear_name = "rear_" + rear_name
            rear_copy_path = os.path.join(source_dir, rear_name)
            _copy_cancellable(rear_video_path, rear_copy_path, cancel_event)
            if _sha256_cancellable(rear_copy_path, cancel_event) != rear_sha256:
                raise ValueError("후방 사본 SHA-256이 다릅니다. 분석을 중단했습니다.")

        # 사본 이름이 긴 경로 때문에 줄어들었으면 DB에도 반영한다 - case.json이 없을 때 다시 열면
        # 원래 이름으로 사본을 찾아 틀렸다(리뷰 #96·#117).
        if os.path.basename(source_copy_path) != provisional.source_video_filename:
            history_store.set_source_filename(case_id, os.path.basename(source_copy_path))

        # 손상 파일: 헤더가 사라졌고 참조 영상이 있으면 헤더를 이식한 작업 사본으로 분석·재생한다.
        # 이식 전에 참조 해시, 이식 직후 사본 해시를 남겨 case.json·DB에 고정한다(리뷰 #42).
        repaired_path, repair_note = "", ""
        reference_sha256, repaired_sha256 = "", ""
        slack_check: Dict = {}
        if damage_report is not None and damage_report.needs_reference and reference_path:
            if not os.path.isfile(reference_path):
                raise FileNotFoundError("참조 영상을 찾을 수 없습니다.")
            report("참조 영상 SHA-256 계산 중...")
            reference_sha256 = _sha256_cancellable(reference_path, cancel_event)
            check_cancelled()
            report("참조 영상의 헤더를 이식한 작업 사본 만드는 중...")
            from core.damage_repair import transplant_header
            stem, ext = os.path.splitext(os.path.basename(source_copy_path))
            repaired_path = os.path.join(source_dir, f"{stem}_repaired{ext}")
            scan_target = replace_report_path(damage_report, source_copy_path)
            repair_note = _transplant_cancellable(transplant_header, scan_target, reference_path,
                                                 repaired_path, cancel_event)
            check_cancelled()
            report("이식 사본 SHA-256 계산 중...")
            repaired_sha256 = _sha256_cancellable(repaired_path, cancel_event)
            check_cancelled()
            # 헤더 소실 원본은 RIFF/MP4 머리가 없어 올리는 단계의 슬랙 확인(detect_slack)이 '형식 아님'으로 끝난다.
            # 이식 사본에는 원본의 잔재·idx1 뒤 꼬리가 그대로 있으니 사본 기준으로 다시 확인해 둔다(리뷰 #85).
            report("이식 사본의 슬랙(옛 녹화 잔재) 확인 중...")
            slack_check = _detect_slack_on_repaired(repaired_path, cancel_event)
            check_cancelled()
        engine_input = repaired_path or source_copy_path

        report("GPS/센서 메타데이터 추출 중..." + (" (슬랙 카빙 포함)" if carve_slack else ""))
        # 여기서부터 실패하거나 취소되면 방금 만든 사건 레코드를 되돌린다 -
        # 안 그러면 output_folder가 빈 고아 레코드가 History에 그대로 쌓인다.
        # moov가 사라진 MP4(조각 MP4 포함)는 참조 없이는 샘플 표를 못 읽는다. 엔진 결과가 비면 파일 전체
        # NMEA 카빙으로 남은 GPS를 뽑아 UTC로 시간축을 추정해 본 궤적으로 쓴다(영상 재생 불가, 리뷰 #17).
        gps_only = bool(damage_report is not None and damage_report.header_missing
                        and damage_report.container == "mp4" and not repaired_path)
        extraction = run_full_extraction(engine_input, output_dir, slack=carve_slack,
                                         cancel_event=cancel_event)
        check_cancelled()
        # 조각이 아닌 MP4는 moov 내용이 손상돼 엔진이 일부만 읽어도(리뷰 #28) 파일 전체 카빙을 돌려 더 많으면 그것을 쓴다.
        if gps_only and (not extraction.points or not damage_report.fragmented):
            report("moov 없음/손상 - 파일 전체에서 GPS 문장 회수 중...")
            from core.damage_marks import carve_whole_file_gps
            lo, hi = (damage_report.media_range or [0, None])
            carved = carve_whole_file_gps(engine_input, lo, hi, cancel_event)
            check_cancelled()
            _promote_carved_gps(extraction, carved, os.path.join(output_dir, "recovered_gps"))
        if slack_check:
            extraction.warnings.extend(slack_check.get("warnings") or [])

        report("영상 길이 계산 중...")
        dur = duration_mod.get_duration_sec(engine_input, routing.container,
                                            engine_output_dir=output_dir)
        dur = effective_duration(dur, extraction, routing.container)
        check_cancelled()

        report("이상치 확인 중...")
        outliers.mark_outliers(extraction.points)
        extraction.avi_repaired = _avi_was_repaired(output_dir)
        if extraction.status == "ok" and not extraction.fix_count:
            extraction.status = STATUS_GPS_UNTRUSTED
            extraction.status_detail = "모든 GPS 좌표가 검증 기준에서 제외됐습니다."
        if damage_report is not None and damage_report.damaged:
            report("손상 구간 표시·복구 중...")
            check_cancelled()
            # 손상 구간의 바이트 위치는 이식 사본이 아니라 원 사본 기준이다. 이식하면 앞부분 길이가
            # 달라지므로 보고서의 구간을 사본 기준으로 옮긴다.
            shift = _repaired_shift(damage_report, repaired_path) if repaired_path else 0
            extraction.damage = apply_damage(extraction, damage_report, recovery_mode, dur,
                                             offset_shift=shift, repaired_path=repaired_path,
                                             reference_path=reference_path, repair_note=repair_note,
                                             reference_sha256=reference_sha256, repaired_sha256=repaired_sha256)
            if slack_check:
                extraction.damage["slack_checked_on"] = "repaired"
                extraction.damage["slack_available"] = bool(slack_check.get("has_slack"))
                extraction.damage["slack_info"] = slack_check.get("info") or {}
                extraction.damage["slack_carved"] = bool(carve_slack)
            if any(iv.get("placeholder_limited") for iv in extraction.damage.get("intervals", [])):
                extraction.warnings.append("손상 구간이 많아 표시용 빈 행 한도를 넘었습니다. 일부 손상 구간은 행 없이 "
                                           "구간 목록에만 남아 있습니다.")

        report("위험운전 행동 분석 중...")
        check_cancelled()
        vehicle_type = driving_events.normalize_vehicle(vehicle_type)
        events = driving_events.detect_driving_events(extraction.points, vehicle_type)

        check_cancelled()
        _record_engine_runs(history_store, case_id, extraction.engine_runs, output_dir)
        write_artifact_manifest(output_dir, case_fields={"damage": extraction.damage})
        _write_case_json(case_folder, case_id, case_number, examiner, memo, source_copy_path, sha256,
                          routing, extraction, dur, settings, events, carve_slack,
                          vehicle_type, rear_copy_path=rear_copy_path, track_mode=track_mode,
                          created_at=analyzed_at, rear_sha256=rear_sha256,
                          original_filename=os.path.basename(video_path))

        history_store.update_case_extraction(
            case_id, duration_sec=dur, avi_repaired=_avi_was_repaired(output_dir),
            output_folder=output_dir,
        )
        history_store.set_analysis_status(case_id, extraction.status)
        history_store.set_manifest_sha256(case_id, manifest_sha256(output_dir))
        if extraction.damage:
            history_store.set_damage(case_id, extraction.damage)
        if rear_copy_path:
            history_store.set_rear_video(case_id, os.path.basename(rear_copy_path))
        if track_mode:
            history_store.set_track_mode(case_id, track_mode)

        report("완료")
        return PipelineResult(
            case_id=case_id,
            case_folder=case_folder,
            source_copy_path=source_copy_path,
            extraction=extraction,
            duration_sec=dur,
            driving_events=events,
            sha256=sha256,
            vehicle_type=vehicle_type,
            rear_copy_path=rear_copy_path,
            track_mode=track_mode,
            rear_sha256=rear_sha256,
            analyzed_at=analyzed_at,
            repaired_path=repaired_path,
            repaired_sha256=repaired_sha256,
        )
    except BaseException:
        _rollback_case(history_store, case_id, case_folder)
        raise


def _detect_slack_on_repaired(repaired_path: str, cancel_event) -> Dict:
    """이식 사본의 슬랙 유무. {'has_slack', 'info', 'warnings'}. 엔진이 못 돌아도 분석은 계속한다."""
    try:
        info = detect_slack([repaired_path], cancel_event=cancel_event).get(repaired_path) or {}
    except CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 슬랙 확인 실패는 본 분석을 막지 않는다
        info = {"path": repaired_path, "has_slack": False, "error": str(exc)}
    has_slack = bool(info.get("has_slack"))
    warnings = ["슬랙(옛 녹화 잔재) 확인은 원본이 아니라 헤더를 이식한 작업 사본 기준입니다."]
    if has_slack:
        warnings.append("이식 사본에서 슬랙(컨테이너가 참조하지 않는 옛 녹화 영역)이 확인됐습니다. 슬랙 카빙을 켜지 않았다면 "
                        "'슬랙 카빙 포함'으로 다시 분석해야 그 영역의 GPS가 슬랙 표에 나옵니다.")
    elif info.get("error"):
        warnings.append(f"이식 사본의 슬랙 확인 실패: {info['error']}")
    return {"has_slack": has_slack, "info": info, "warnings": warnings}


def _transplant_cancellable(transplant_header, report, reference_path: str, out_path: str, cancel_event) -> str:
    """헤더 이식. transplant_header가 cancel_event를 받으면 넘긴다(core/damage_repair가 지원하면 취소 가능,
    리뷰 #100). 취소·실패하면 쓰다 만 사본을 지운다."""
    try:
        if cancel_event is not None and "cancel_event" in inspect.signature(transplant_header).parameters:
            return transplant_header(report, reference_path, out_path, cancel_event=cancel_event)
        return transplant_header(report, reference_path, out_path)
    except InterruptedError as exc:
        _remove_quietly(out_path)
        raise CancelledError("분석이 취소되었습니다.") from exc
    except BaseException:
        _remove_quietly(out_path)
        raise


def _remove_quietly(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


CARVED_TIMELINE_FIELDS = ["sample", "start_time_sec", "end_time_sec", "time_source", "time_estimated",
                          "recovery_state", "source_offset", "latitude", "longitude", "speed_kmh", "track_deg",
                          "gps_date", "gps_utc_time", "gps_checksum_ok", "gps_trusted", "gps_status"]


def _write_carved_timeline(folder: str, points: List[TrackPoint]) -> str:
    """카빙 결과를 엔진 산출물 형식(timeline.csv)으로 남긴다. 추정 시각·회수 상태 열을 같이 적어 다시 열어도
    처음 분석과 같은 표시(time_estimated, gap_carved)가 되게 한다(리뷰 #41). 반환값: CSV 경로."""
    import csv
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "timeline.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CARVED_TIMELINE_FIELDS)
        for i, p in enumerate(points, start=1):
            w.writerow([i, f"{p.start_time_sec:.3f}", f"{p.end_time_sec:.3f}", p.time_source,
                        "True" if p.time_estimated else "False", p.recovery_state,
                        p.source_offset if p.source_offset is not None else "",
                        f"{p.latitude:.6f}", f"{p.longitude:.6f}",
                        f"{p.speed_kmh:.3f}" if p.speed_kmh is not None else "",
                        p.track_deg if p.track_deg is not None else "", p.gps_date, p.gps_utc_time,
                        p.gps_checksum_ok, p.gps_trusted, p.gps_status])
    return path


def _promote_carved_gps(extraction: ExtractionResult, carved: List[TrackPoint], timeline_dir: str = "") -> None:
    """moov 소실 MP4: 미디어 영역에서 카빙한 GPS(시각 없음)를 날짜+UTC 기준 시간축으로 본 궤적에 올린다.
    연속성이 끊긴 뒤쪽(옛 녹화 잔재)은 슬랙으로 분리하고(리뷰 #3), time_source·primary 파일 경로를 처음부터
    채운다(리뷰 #41). timeline_dir을 주면 recovered_gps/timeline.csv로 남긴다."""
    from core.damage_marks import TIME_ESTIMATED_WARNING, TIME_SOURCE_UTC_ESTIMATED, estimate_times_from_utc
    points = [p for p in carved if p.has_coords and p.gps_utc_time]
    excluded: List[TrackPoint] = []
    if not estimate_times_from_utc(points, excluded):
        return
    if extraction.points:
        # 엔진이 손상된 moov로 일부만 읽은 경우: 카빙이 더 많이 회수했을 때만 바꾼다(리뷰 #28).
        if len(points) <= extraction.fix_count:
            return
        extraction.warnings.append(f"손상된 moov로 엔진이 읽은 {extraction.fix_count}건 대신 파일 전체 카빙으로 회수한 "
                                   f"{len(points)}건을 씁니다(재생 시각은 UTC 추정).")
    extraction.points = points
    extraction.status = "ok"
    extraction.status_detail = ("moov(헤더)가 없어 샘플 표 대신 파일 전체에서 GPS 문장을 회수하고 "
                                "UTC로 시간축을 추정했습니다. 영상은 재생할 수 없습니다.")
    extraction.time_source = TIME_SOURCE_UTC_ESTIMATED
    extraction.warnings.append("moov 소실: GPS를 미디어 영역 카빙으로 회수해 UTC 기준 시간축을 추정함(영상 재생 불가)")
    if TIME_ESTIMATED_WARNING not in extraction.warnings:
        extraction.warnings.append(TIME_ESTIMATED_WARNING)
    if excluded:
        extraction.slack_points = list(getattr(extraction, "slack_points", []) or []) + excluded
        extraction.warnings.append(f"카빙한 GPS 가운데 날짜·UTC 연속성이 끊긴 {len(excluded)}건은 다른 녹화의 "
                                   "잔재로 보고 본 궤적에서 빼 슬랙으로 두었습니다.")
    if timeline_dir:
        extraction.primary_source_file = _write_carved_timeline(timeline_dir, points)


def replace_report_path(report, path: str):
    """검사 결과의 파일 경로를 분석 사본으로 바꾼 복사본(바이트 구조는 같다)."""
    from core.damage import DamageReport
    data = report.to_dict()
    data["path"] = path
    return DamageReport.from_dict(data)


def _repaired_shift(report, repaired_path: str) -> int:
    """이식 사본에서 미디어가 몇 바이트 옮겨졌는지(원 사본 기준 구간을 사본 기준으로 옮길 때 더한다)."""
    try:
        if report.container == "avi":
            import mmap
            from core.paths import ensure_vendor_importable
            ensure_vendor_importable()
            import integration_avi as _avi
            with open(repaired_path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                _h, movi, _i, _ = _avi.find_top_level_sections(mm)
            _avi.WARNINGS.clear()
            if movi and report.avi_movi_fourcc is not None:
                return movi[0].data_start - report.avi_movi_fourcc
        else:
            from core.paths import ensure_vendor_importable
            ensure_vendor_importable()
            import integration_mp4 as _mp4
            size = os.path.getsize(repaired_path)
            with open(repaired_path, "rb") as f:
                boxes, _b, _k = _mp4.scan_top_level_bounded(f, size)
            _mp4.WARNINGS.clear()
            first = next((b.start for b in boxes if b.box_type == b"moof"), None)
            if first is not None and report.mp4_first_moof is not None:
                return first - report.mp4_first_moof
    except (OSError, ValueError):
        pass
    return 0


def apply_damage(extraction: ExtractionResult, report, mode: str, duration_sec,
                 offset_shift: int = 0, repaired_path: str = "", reference_path: str = "",
                 repair_note: str = "", reference_sha256: Optional[str] = None,
                 repaired_sha256: str = "") -> Dict:
    """손상 구간을 시간축에 올리고(빈 행) 선택에 따라 임의 복구 행을 넣는다. case.json에 남길 dict를 돌려준다.
    reference_sha256이 None이면(처음 분석에서 미리 재지 않은 경우) 참조 파일을 해시한다. 문자열(빈 값 포함)을
    주면 그대로 쓴다 - 다시 열 때는 저장값을 넘겨 참조를 다시 읽지 않는다(리뷰 #45)."""
    from core.damage_marks import (MODE_NONE, apply_damage_marks, compute_intervals, describe_intervals)
    mode = mode if mode in ("none", "interpolate") else MODE_NONE
    shifted = report
    if offset_shift:
        shifted = replace_report_path(report, report.path)
        shifted.gaps = [[a + offset_shift, b + offset_shift] for a, b in report.gaps]
        shifted.zero_regions = [[a + offset_shift, b + offset_shift] for a, b in report.zero_regions]
    intervals = compute_intervals(extraction.points, shifted, duration_sec)
    extraction.points = apply_damage_marks(extraction.points, intervals, mode)
    if reference_sha256 is None:
        reference_sha256 = ""
        if reference_path and os.path.isfile(reference_path):
            try:
                reference_sha256 = _sha256_cancellable(reference_path, None)
            except OSError:
                reference_sha256 = ""
    return {
        "report": report.to_dict(),
        "mode": mode,
        "offset_shift": offset_shift,
        "intervals": [iv.to_dict() for iv in intervals],
        "interval_lines": describe_intervals(intervals),
        "summary": report.summary_lines(),
        "repaired_file": os.path.basename(repaired_path) if repaired_path else "",
        "repaired_sha256": repaired_sha256 or "",
        "reference_path": reference_path,
        "reference_sha256": reference_sha256 or "",
        "repair_note": repair_note,
        "time_estimated": extraction.time_estimated,
        "time_source": extraction.time_source,
    }


def reapply_damage(extraction: ExtractionResult, meta: Dict, duration_sec) -> Dict:
    """다시 열 때: 저장된 검사 결과·복구 방식으로 손상 구간 행을 같은 규칙으로 다시 만든다. 참조 영상은
    다시 해시하지 않고 저장값을 쓴다(리뷰 #45)."""
    from core.damage import DamageReport
    report = DamageReport.from_dict(meta.get("report") or {})
    repaired = meta.get("repaired_file") or ""
    out = apply_damage(extraction, report, str(meta.get("mode") or "none"), duration_sec,
                       offset_shift=int(meta.get("offset_shift") or 0), repaired_path=repaired,
                       reference_path=str(meta.get("reference_path") or ""),
                       repair_note=str(meta.get("repair_note") or ""),
                       reference_sha256=str(meta.get("reference_sha256") or ""),
                       repaired_sha256=str(meta.get("repaired_sha256") or ""))
    for key in ("slack_checked_on", "slack_available", "slack_info", "slack_carved"):
        if key in meta:
            out[key] = meta[key]
    return out


DURATION_MISMATCH_SEC = 0.5


def effective_duration(header_duration: Optional[float], extraction: ExtractionResult, container: str) -> Optional[float]:
    """영상 길이. AVI는 헤더(avih/strh)의 계획 길이가 아니라 엔진이 색인에서 잰 길이(measured_duration_sec,
    빈 자리 포함)를 쓴다 - 사전 할당 파일(헤더 60초, 녹화 20초)에서 헤더 길이를 쓰면 19~60초가 '손상 구간'이
    되고 Tracker 막대가 늘어났다(리뷰 2026-10-11 #11). 0.5초 넘게 다를 때만 바꾸고 경고를 남긴다."""
    info = extraction.engine_damage if isinstance(extraction.engine_damage, dict) else None
    measured = info.get("measured_duration_sec") if info else None
    if container != "avi" or not isinstance(measured, (int, float)) or not (0 < measured <= duration_mod.MAX_PLAUSIBLE_DURATION_SEC):
        return header_duration
    measured = float(measured)
    if header_duration is not None and abs(measured - header_duration) <= DURATION_MISMATCH_SEC:
        return header_duration
    extraction.warnings.append(
        (f"영상 길이는 헤더의 계획 길이({header_duration:.1f}초)가 아니라 색인에서 잰 길이({measured:.1f}초)를 씁니다"
         if header_duration is not None else f"영상 길이는 색인에서 잰 길이({measured:.1f}초)를 씁니다")
        + " (녹화가 중단된 사전 할당 파일 등).")
    return measured


def _artifacts_present(output_dir: str) -> bool:
    """엔진 산출물(CSV·로그·manifest)이 하나라도 있는가. 폴더만 있고 비었으면 False(리뷰 #75)."""
    if not output_dir or not os.path.isdir(output_dir):
        return False
    for _root, _dirs, files in os.walk(output_dir):
        if files:
            return True
    return False


def _plausible_duration(value, output_dir: str, source_path: str, container: str,
                        warnings: List[str]) -> Optional[float]:
    """DB에 저장된 길이를 다시 열 때도 검사한다 - all-1s mvhd가 49.7일로 저장된 옛 사건이 그대로
    '1193:02:47'로 표시됐다(리뷰 #24·#112). 틀리면 사본 헤더·timeline에서 다시 구한다."""
    try:
        dur = float(value) if value is not None else None
    except (TypeError, ValueError):
        dur = None
    if dur is not None and 0 < dur <= duration_mod.MAX_PLAUSIBLE_DURATION_SEC:
        return dur
    if dur is None:
        return None
    fixed = None
    if source_path and os.path.isfile(source_path):
        try:
            fixed = duration_mod.get_duration_sec(source_path, container, engine_output_dir=output_dir)
        except Exception:  # noqa: BLE001 - 길이 재계산 실패는 열기를 막지 않는다
            fixed = None
    if fixed is None and output_dir:
        fixed = duration_mod._timeline_fallback(output_dir)
    warnings.append(f"저장된 영상 길이({dur:.0f}초)가 비정상이라 "
                    + (f"{fixed:.1f}초로 다시 구했습니다." if fixed else "길이를 비웠습니다."))
    return fixed


def read_case_metadata(case_folder: str) -> tuple:
    """case.json을 읽는다. (dict, 오류 문자열). 없으면 ({}, ""), 깨졌으면 ({}, 사유)."""
    path = os.path.join(case_folder, "case.json") if case_folder else ""
    if not path or not os.path.isfile(path):
        return {}, ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, "case.json의 내용이 객체가 아닙니다."
        return data, ""
    except (OSError, ValueError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def reopen_case(case: CaseRecord, cancel_event=None) -> PipelineResult:
    """저장된 사건을 다시 연다. 참조 영상은 해시하지 않고 저장값을 쓴다(리뷰 #45) - 긴 작업(manifest 대조)은
    cancel_event로 멈출 수 있으니 워커 스레드(ui/workers.ReopenWorker)에서 부른다."""
    if case.segments:
        return _reopen_sequence(case, cancel_event=cancel_event)
    case_folder = os.path.dirname(case.output_folder) if case.output_folder else ""
    metadata, metadata_error = read_case_metadata(case_folder)
    artifacts_present = _artifacts_present(case.output_folder)
    excluded: List[TrackPoint] = []
    load_warnings: List[str] = []
    points, primary, time_source = (
        load_existing_results(case.output_folder, excluded_out=excluded, warnings_out=load_warnings)
        if artifacts_present else ([], None, ""))
    outliers.mark_outliers(points)

    # 차종 선택이 생기기 전 사건(임계값 m/s²만 저장)은 승용차 기준으로 다시 판정한다.
    vehicle_type = driving_events.normalize_vehicle(case.analysis_settings.get("vehicle_type"))
    events = driving_events.detect_driving_events(points, vehicle_type)

    routing = RoutingResult(
        container=case.detected_format, supported=True,
        reason="저장된 사건을 다시 열었습니다 (재추출 없이 기존 결과 표시).",
    )
    # 상태는 DB(analysis_status) > case.json 순으로 믿는다. 둘 다 없으면 좌표가 있어도 'ok'로 올리지
    # 않는다 - case.json이 0바이트로 잘린 timed_out 사건이 리포트에 'ok'로 찍혔다(리뷰 #30).
    # "analyzing"은 분석 도중 프로세스가 죽은 사건이다(리뷰 #31). 산출물이 없으면 상태와 상관없이
    # unknown이다 - 폴더가 사라진 사건이 'GPS 없는 정상 분석'처럼 열렸다(리뷰 #75).
    status = case.analysis_status or str(metadata.get("status") or "")
    if not artifacts_present:
        status, detail = "unknown", ("엔진 산출물이 없습니다(engine_output 폴더가 비었거나 사라짐). "
                                     "이 사건의 결과는 확인할 수 없습니다.")
    elif status == "analyzing":
        status, detail = "engine_failed", "분석이 끝나기 전에 프로그램이 종료됐습니다. 이 사건은 다시 분석하세요."
    elif not status:
        status, detail = "unknown", "저장된 분석 상태를 확인할 수 없습니다."
    else:
        detail = str(metadata.get("status_detail") or "")
    warnings = (_collect_warnings(case.output_folder) if artifacts_present else []) + load_warnings
    if metadata_error:
        warnings.append(f"case.json을 읽지 못했습니다({metadata_error}). DB에 저장된 정보로 열었습니다.")
    extraction = ExtractionResult(
        routing=routing, points=points, engine_runs=[],
        status=status, status_detail=detail,
        warnings=warnings,
        avi_repaired=case.avi_repaired,
        slack_points=(load_slack_points(case.output_folder) if artifacts_present else []) + excluded,
        used_input_path=case.source_video_path, primary_source_file=primary,
        time_source=time_source,
        engine_damage=metadata.get("engine_damage") if isinstance(metadata.get("engine_damage"), dict) else None,
    )
    rear_copy = (os.path.join(case_folder, "source", case.rear_video_filename)
                 if case_folder and case.rear_video_filename else "")
    copy_name = str(metadata.get("source_video_filename") or case.source_video_filename)
    duration_sec = _plausible_duration(case.duration_sec, case.output_folder,
                                       os.path.join(case_folder, "source", copy_name) if case_folder else "",
                                       case.detected_format, extraction.warnings)
    repaired_path, repaired_sha256, damage_source = "", "", ""
    # 손상 정보는 case.json > DB 사본 순. case.json이 깨지거나 손상 필드가 빠졌는데 DB에 있으면 DB 것을
    # 쓰고 경고한다 - 예전엔 case.json이 0바이트가 되면 손상 표시가 사라져 정상 사건으로 열렸다(리뷰 #82).
    damage_meta = metadata.get("damage") if isinstance(metadata.get("damage"), dict) else None
    if damage_meta:
        damage_source = "case.json"
    elif case.damage:
        damage_meta, damage_source = case.damage, "db"
        extraction.warnings.append("case.json에 손상 정보가 없어 사건 이력 DB에 저장된 손상 정보를 썼습니다"
                                   + (" (case.json 손상)." if metadata_error else "."))
    if damage_meta:
        # 손상 구간 행(빈 행·임의 복구)은 CSV에 없다. 저장한 검사 결과와 복구 방식으로 같은 규칙으로 다시 만든다.
        if damage_meta.get("repaired_file"):
            repaired_path = os.path.join(case_folder, "source", str(damage_meta["repaired_file"]))
        repaired_sha256 = str(damage_meta.get("repaired_sha256") or "")
        try:
            extraction.damage = reapply_damage(extraction, damage_meta, duration_sec)
            events = driving_events.detect_driving_events(extraction.points, vehicle_type)
        except Exception as exc:  # noqa: BLE001 - 손상 정보가 깨져도 사건은 연다
            extraction.warnings.append(f"손상 구간 정보를 복원하지 못했습니다: {exc}")
            extraction.damage = damage_meta
    return PipelineResult(
        case_id=case.id or -1,
        case_folder=case_folder,
        source_copy_path=(os.path.join(case_folder, "source", copy_name) if case_folder else ""),
        artifacts_verified=verify_artifact_manifest(case.output_folder, case.manifest_sha256,
                                                    metadata if not metadata_error else None, cancel_event),
        extraction=extraction,
        duration_sec=duration_sec,
        driving_events=events,
        sha256=case.source_video_sha256,
        vehicle_type=vehicle_type,
        # 사본이 없어도 경로를 남긴다 - 그래야 무결성 표시등이 '사본 없음'(회색)으로 알린다. 예전엔
        # 비워서 후방이 조용히 빠지고 표시등이 초록이 됐다(리뷰 #17).
        rear_copy_path=rear_copy,
        repaired_path=repaired_path,
        repaired_sha256=repaired_sha256,
        damage_source=damage_source,
        track_mode=case.track_mode or "",
        rear_sha256=str(metadata.get("rear_sha256") or ""),
        analyzed_at=case.created_at,
    )


def update_case_json(case_folder: str, case_number: str, examiner: str, memo: str) -> bool:
    """사건 정보 수정을 case.json에도 반영한다(DB 유실 대비 사본). 파일이 없으면 False."""
    path = os.path.join(case_folder, "case.json") if case_folder else ""
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        data["case_number"] = case_number
        data["examiner"] = examiner
        data["memo"] = memo
        data["info_updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json_atomic(path, data)
        return True
    except (OSError, ValueError):
        return False


def _avi_was_repaired(output_dir: str) -> bool:
    return bool(glob.glob(os.path.join(output_dir, "**", "*_wo_slack.avi"), recursive=True))


def _write_case_json(case_folder, case_id, case_number, examiner, memo, video_path, sha256,
                      routing, extraction: ExtractionResult, dur, settings, events,
                      carve_slack, vehicle_type: str, rear_copy_path: str = "", track_mode: str = "",
                      created_at: str = "", segments: Optional[List[Dict]] = None,
                      rear_sha256: str = "", original_filename: str = "") -> None:
    case_json_path = os.path.join(case_folder, "case.json")
    _write_json_atomic(case_json_path, {
            "case_id": case_id,
            "case_number": case_number,
            "examiner": examiner,
            "memo": memo,
            "source_video_filename": os.path.basename(video_path),   # source/ 안 사본 이름
            "original_filename": original_filename or os.path.basename(video_path),
            "source_video_sha256": sha256,
            "detected_format": routing.container,
            "avi_repaired": _avi_was_repaired(os.path.join(case_folder, "engine_output")),
            "status": extraction.status,
            "status_detail": extraction.status_detail,
            "warnings": extraction.warnings,
            "copy_sha256_verified": True,
            "rear_sha256": rear_sha256,   # 이미 사본과 대조한 값(다시 해시하지 않음, 리뷰 #95)
            "engine_runs": [{"argv": r.argv, "exit_code": r.exit_code,
                "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat()}
                for r in extraction.engine_runs],
            "slack_point_count": len(extraction.slack_points),
            "extension_mismatch": routing.extension_mismatch,
            "engine": "integration_blackbox",
            "slack_carving": carve_slack,
            "duration_sec": dur,
            "time_source": extraction.time_source,
            # 재생 시각을 GPS 기록으로 추정했는가(헤더 소실). 손상 사건이 아니어도 남긴다(리뷰 #74).
            "time_estimated": extraction.time_estimated,
            "engine_damage": extraction.engine_damage,
            "analysis_settings": settings,
            "created_at": created_at or datetime.now().isoformat(timespec="seconds"),
            "point_count": len(extraction.points),
            "gps_fix_count": extraction.fix_count,
            "outlier_count": extraction.outlier_count,
            "vehicle_type": vehicle_type,
            "driving_event_count": len(events),
            "driving_event_counts": driving_events.count_events(events),
            "rear_video_filename": os.path.basename(rear_copy_path) if rear_copy_path else "",
            "track_mode": track_mode,
            "segments": segments or [],
            "damage": extraction.damage,
        })


def _record_engine_runs(history_store: HistoryStore, case_id: int, runs, output_dir: str) -> None:
    for run in runs:
        log_path = os.path.join(
            output_dir, f"_run_{run.started_at.strftime('%H%M%S%f')}.log",
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(
                "ARGV: " + " ".join(run.argv) + "\n"
                + (f"NOTE: {run.note}\n" if run.note else "")
                + f"EXIT: {run.exit_code}\n\n"
                "--- stdout ---\n" + run.stdout + "\n"
                "--- stderr ---\n" + run.stderr
            )
        history_store.add_engine_run(
            case_id, "integration_blackbox", run.argv, run.exit_code, log_path,
            run.started_at.isoformat(timespec="seconds"),
            run.finished_at.isoformat(timespec="seconds"),
        )


# ---------------------------------------------------------------------------
# 연속 영상 이어보기
# ---------------------------------------------------------------------------

def combine_segments(segments: List[SegmentResult]) -> tuple:
    """구간들을 시간축으로 이어 붙인 (추출 결과, 위험운전 목록). 시각은 offset만큼 밀고 행 번호는
    앞 구간 행 수만큼 민다. 위험운전은 구간마다 판정한 것을 옮긴다 - 구간 경계를 넘는 판정은 하지
    않는다(두 파일 사이 1초 안팎은 어느 영상에도 없는 시간이다)."""
    points: List[TrackPoint] = []
    events: List[DrivingEvent] = []
    runs, warnings, slack = [], [], []
    status, detail = "ok", ""
    for seg in segments:
        base, off = len(points), seg.offset_sec
        for p in seg.extraction.points:
            points.append(replace(
                p,
                start_time_sec=None if p.start_time_sec is None else p.start_time_sec + off,
                end_time_sec=None if p.end_time_sec is None else p.end_time_sec + off,
                segment_index=seg.index))
        for ev in seg.driving_events:
            events.append(replace(
                ev, start_index=ev.start_index + base, end_index=ev.end_index + base,
                start_time_sec=None if ev.start_time_sec is None else ev.start_time_sec + off,
                end_time_sec=None if ev.end_time_sec is None else ev.end_time_sec + off))
        runs += seg.extraction.engine_runs
        warnings += [f"[{seg.label}] {w}" for w in seg.extraction.warnings]
        slack += seg.extraction.slack_points
    # 구간마다 비정상 상태를 모두 모은다 - 첫 하나만 남기면 다른 구간의 시간 초과가 묻힌다(리뷰 #32).
    bad = [seg for seg in segments if seg.extraction.status != "ok"]
    if bad:
        priority = ["timed_out", "engine_failed", "unsupported", "gps_untrusted", "no_gps", "unknown"]
        status = min((seg.extraction.status for seg in bad),
                     key=lambda st: priority.index(st) if st in priority else len(priority))
        detail = "; ".join(f"{seg.label}: {seg.extraction.status}"
                           + (f"({seg.extraction.status_detail})" if seg.extraction.status_detail else "")
                           for seg in bad)
    first = segments[0].extraction
    combined = ExtractionResult(
        routing=first.routing, points=points, engine_runs=runs,
        used_input_path=segments[0].primary_copy_path,
        primary_source_file="; ".join(s.extraction.primary_source_file for s in segments
                                      if s.extraction.primary_source_file) or None,
        time_source=first.time_source, warnings=warnings, status=status, status_detail=detail,
        slack_points=slack, avi_repaired=any(s.extraction.avi_repaired for s in segments))
    return combined, events


def segment_record(seg: SegmentResult, container: str) -> Dict:
    """DB·case.json에 남기는 구간 정보. 사본 이름은 사건 폴더 source/ 기준."""
    return {
        "label": seg.label,
        "front_filename": os.path.basename(seg.front_copy_path) if seg.front_copy_path else "",
        "rear_filename": os.path.basename(seg.rear_copy_path) if seg.rear_copy_path else "",
        "front_sha256": seg.front_sha256,
        "rear_sha256": seg.rear_sha256,
        "front_original": seg.front_original,
        "rear_original": seg.rear_original,
        "track_mode": seg.track_mode,
        "duration_sec": seg.duration_sec,
        "offset_sec": seg.offset_sec,
        "output_subdir": f"seg{seg.index + 1:02d}",
        "container": container,
        "status": seg.extraction.status,
        "status_detail": seg.extraction.status_detail,
    }


def _segment_span(seg_duration: Optional[float], points: List[TrackPoint]) -> float:
    if seg_duration:
        return float(seg_duration)
    times = [p.end_time_sec or p.start_time_sec for p in points if p.start_time_sec is not None]
    return max(times) if times else 0.0


def run_sequence_pipeline(
    items,
    case_number: str,
    examiner: str,
    memo: str,
    settings: Dict,
    cases_root_dir: str,
    history_store: HistoryStore,
    vehicle_type: str = driving_events.DEFAULT_VEHICLE,
    carve_slack: bool = False,
    progress_cb: ProgressCallback = None,
    cancel_event=None,
    precomputed_sha256: Optional[Dict[str, str]] = None,
) -> PipelineResult:
    """연속 영상 이어보기 분석. items는 core/video_sequence.SequenceItem 목록(검사를 마친 순서).
    영상마다 원본↔사본 SHA-256을 대조하고 따로 추출한 뒤 시간축으로 이어 붙인다. 이어 붙인 영상
    파일은 만들지 않는다(재인코딩하면 원본과 다른 파일이 된다)."""
    def report(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("분석이 취소되었습니다.")

    if len(items) < 1:
        raise ValueError("이어볼 영상이 없습니다.")
    if not any(settings.get(key, True) for key in ("tracker", "speed", "location")):
        raise ValueError("분석 항목을 최소 한 개 선택하세요.")
    vehicle_type = driving_events.normalize_vehicle(vehicle_type)
    analyzed_at = datetime.now().astimezone().isoformat(timespec="seconds")   # 시간대 포함(리뷰 #99)
    known = dict(precomputed_sha256 or {})

    def sha_of(path: str) -> str:
        if not known.get(path):
            check_cancelled()
            report(f"파일 해시 계산 중 (SHA-256) - {os.path.basename(path)}")
            known[path] = _sha256_cancellable(path, cancel_event)
        return known[path]

    routings = []
    for item in items:
        check_cancelled()
        routing = format_sniffer.sniff(item.primary)
        if not routing.supported:
            raise ValueError(f"{os.path.basename(item.primary)}: {routing.reason}")
        routings.append(routing)
        for path in (item.front, item.rear):
            if path:
                sha_of(path)

    first = items[0].primary
    provisional = CaseRecord(
        id=None, case_number=case_number, examiner=examiner, memo=memo,
        source_video_path=first,
        source_video_filename=f"01_{os.path.basename(first)}",   # source/ 안 사본 이름(리뷰 #96)
        source_video_size_bytes=sum(os.path.getsize(p) for it in items for p in (it.front, it.rear) if p),
        source_video_sha256=known[first], detected_format=routings[0].container,
        analysis_settings=settings, created_at=analyzed_at,
    )
    case_id = history_store.add_case(provisional)
    case_folder = os.path.join(cases_root_dir, _safe_case_folder_name(case_number, case_id))
    source_dir = os.path.join(case_folder, "source")
    output_dir = os.path.join(case_folder, "engine_output")
    try:
        os.makedirs(case_folder, exist_ok=False)
    except BaseException:
        history_store.delete_case(case_id)
        raise
    try:
        os.makedirs(source_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        history_store.set_output_folder(case_id, output_dir)
        history_store.set_analysis_status(case_id, "analyzing")
        segments: List[SegmentResult] = []
        offset = 0.0
        total = len(items)
        for n, item in enumerate(items):
            label = f"video{n + 1}"
            copies = {}
            for role, path in (("front", item.front), ("rear", item.rear)):
                if not path:
                    copies[role] = ""
                    continue
                check_cancelled()
                report(f"[{label}/{total}] {'전방' if role == 'front' else '후방'} 영상을 사건 폴더로 복사 중...")
                name = _copy_name(source_dir, f"{n + 1:02d}_{os.path.basename(path)}", f"{n + 1:02d}_{role}")
                if role == "rear" and (os.path.exists(os.path.join(source_dir, name)) or (
                        copies["front"] and name.casefold() == os.path.basename(copies["front"]).casefold())):
                    name = f"{n + 1:02d}_rear_{os.path.basename(path)}"
                copy = os.path.join(source_dir, name)
                _copy_cancellable(path, copy, cancel_event)
                if _sha256_cancellable(copy, cancel_event) != known[path]:
                    raise ValueError(f"{os.path.basename(path)}: 원본 해시와 사본 SHA-256이 다릅니다. 분석을 중단했습니다.")
                copies[role] = copy
            primary_copy = copies["front"] or copies["rear"]
            seg_out = os.path.join(output_dir, f"seg{n + 1:02d}")
            check_cancelled()
            report(f"[{label}/{total}] GPS/센서 메타데이터 추출 중...")
            extraction = run_full_extraction(primary_copy, seg_out, slack=carve_slack, cancel_event=cancel_event)
            dur = duration_mod.get_duration_sec(primary_copy, routings[n].container, engine_output_dir=seg_out)
            dur = effective_duration(dur, extraction, routings[n].container)
            outliers.mark_outliers(extraction.points)
            extraction.avi_repaired = _avi_was_repaired(seg_out)
            if extraction.status == "ok" and not extraction.fix_count:
                extraction.status = STATUS_GPS_UNTRUSTED
                extraction.status_detail = "모든 GPS 좌표가 검증 기준에서 제외됐습니다."
            for p in extraction.points:
                p.segment_index = n
            events = driving_events.detect_driving_events(extraction.points, vehicle_type)
            segments.append(SegmentResult(
                index=n, front_copy_path=copies["front"], rear_copy_path=copies["rear"],
                front_sha256=known.get(item.front, "") if item.front else "",
                rear_sha256=known.get(item.rear, "") if item.rear else "",
                track_mode=item.track_mode, duration_sec=dur, offset_sec=offset,
                extraction=extraction, driving_events=events,
                front_original=item.front, rear_original=item.rear))
            offset += _segment_span(dur, extraction.points)
            _record_engine_runs(history_store, case_id, extraction.engine_runs, seg_out)

        report("영상을 이어 붙이는 중...")
        check_cancelled()
        combined, events = combine_segments(segments)
        records = [segment_record(seg, routings[seg.index].container) for seg in segments]
        track_mode = next((it.track_mode for it in items if it.track_mode), "")
        # 사본 이름이 줄어들었으면 DB의 사본 이름도 맞춘다(리뷰 #117).
        first_copy_name = os.path.basename(segments[0].primary_copy_path)
        if first_copy_name != provisional.source_video_filename:
            history_store.set_source_filename(case_id, first_copy_name)
        write_artifact_manifest(output_dir, case_fields={"damage": None})
        _write_case_json(case_folder, case_id, case_number, examiner, memo, segments[0].primary_copy_path,
                          known[first], routings[0], combined, offset, settings, events, carve_slack,
                          vehicle_type, track_mode=track_mode, created_at=analyzed_at, segments=records,
                          original_filename=os.path.basename(first))
        history_store.update_case_extraction(case_id, duration_sec=offset,
                                             avi_repaired=combined.avi_repaired, output_folder=output_dir)
        history_store.set_analysis_status(case_id, combined.status)
        history_store.set_manifest_sha256(case_id, manifest_sha256(output_dir))
        history_store.set_segments(case_id, records)
        if track_mode:
            history_store.set_track_mode(case_id, track_mode)
        report("완료")
        return PipelineResult(
            case_id=case_id, case_folder=case_folder, source_copy_path=segments[0].primary_copy_path,
            extraction=combined, duration_sec=offset, driving_events=events, sha256=known[first],
            vehicle_type=vehicle_type, track_mode=track_mode, analyzed_at=analyzed_at, segments=segments)
    except BaseException:
        _rollback_case(history_store, case_id, case_folder)
        raise


def _reopen_sequence(case: CaseRecord, cancel_event=None) -> PipelineResult:
    case_folder = os.path.dirname(case.output_folder) if case.output_folder else ""
    metadata, metadata_error = read_case_metadata(case_folder)
    source_dir = os.path.join(case_folder, "source")
    vehicle_type = driving_events.normalize_vehicle(case.analysis_settings.get("vehicle_type"))
    segments: List[SegmentResult] = []
    offset = 0.0
    for n, rec in enumerate(case.segments):
        seg_out = os.path.join(case.output_folder, rec.get("output_subdir") or f"seg{n + 1:02d}")
        points, primary, time_source = load_existing_results(seg_out) if os.path.isdir(seg_out) else ([], None, "")
        outliers.mark_outliers(points)
        for p in points:
            p.segment_index = n
        front = os.path.join(source_dir, rec["front_filename"]) if rec.get("front_filename") else ""
        rear = os.path.join(source_dir, rec["rear_filename"]) if rec.get("rear_filename") else ""
        extraction = ExtractionResult(
            routing=RoutingResult(container=rec.get("container") or case.detected_format, supported=True,
                                  reason="저장된 사건을 다시 열었습니다 (재추출 없이 기존 결과 표시)."),
            points=points, engine_runs=[], used_input_path=front or rear,
            primary_source_file=primary, time_source=time_source,
            warnings=_collect_warnings(seg_out) if os.path.isdir(seg_out) else [],
            status=(rec.get("status") or "unknown") if os.path.isdir(seg_out) else "unknown",
            status_detail=(rec.get("status_detail") or "") if os.path.isdir(seg_out)
            else "엔진 산출물 폴더가 없습니다.",
            slack_points=load_slack_points(seg_out) if os.path.isdir(seg_out) else [],
            avi_repaired=_avi_was_repaired(seg_out))
        dur = rec.get("duration_sec")
        segments.append(SegmentResult(
            index=n, front_copy_path=front, rear_copy_path=rear,
            front_sha256=rec.get("front_sha256") or "", rear_sha256=rec.get("rear_sha256") or "",
            track_mode=rec.get("track_mode") or "", duration_sec=dur, offset_sec=offset,
            extraction=extraction,
            driving_events=driving_events.detect_driving_events(points, vehicle_type),
            front_original=rec.get("front_original") or "", rear_original=rec.get("rear_original") or ""))
        offset += _segment_span(dur, points)
    combined, events = combine_segments(segments)
    return PipelineResult(
        case_id=case.id or -1, case_folder=case_folder,
        source_copy_path=segments[0].primary_copy_path if segments else "",
        extraction=combined, duration_sec=case.duration_sec or offset, driving_events=events,
        sha256=case.source_video_sha256, vehicle_type=vehicle_type, track_mode=case.track_mode or "",
        analyzed_at=case.created_at, segments=segments,
        artifacts_verified=verify_artifact_manifest(case.output_folder, case.manifest_sha256,
                                                    metadata if not metadata_error else None, cancel_event))
