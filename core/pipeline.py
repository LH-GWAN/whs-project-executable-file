from __future__ import annotations

import glob
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
    사본은 rmtree가 조용히 건너뛰었다(리뷰 #28)."""
    from core.case_deletion import _rmtree
    try:
        if case_folder and os.path.isdir(case_folder):
            _rmtree(case_folder)
    except OSError:
        shutil.rmtree(case_folder, ignore_errors=True)
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


def write_artifact_manifest(output_dir: str) -> str:
    """엔진 산출물(CSV·로그)의 SHA-256 목록을 engine_output/manifest.sha256 에 남긴다. 다시 열 때 대조해
    산출물이 바뀌었는지 알린다(리뷰 #92). 반환값: manifest 경로."""
    lines = []
    for root, _dirs, files in os.walk(output_dir):
        for name in sorted(files):
            if name == "manifest.sha256":
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, output_dir).replace("\\", "/")
            try:
                lines.append(f"{hashing.sha256_file(full)}  {rel}")
            except OSError:
                continue
    path = os.path.join(output_dir, "manifest.sha256")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def verify_artifact_manifest(output_dir: str) -> Optional[bool]:
    """manifest.sha256 대조. 없으면 None, 전부 일치하면 True, 하나라도 다르거나 빠지면 False."""
    path = os.path.join(output_dir or "", "manifest.sha256")
    if not output_dir or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                digest, rel = line.split("  ", 1)
                full = os.path.join(output_dir, rel)
                if not os.path.isfile(full) or hashing.sha256_file(full) != digest:
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

        report("GPS/센서 메타데이터 추출 중..." + (" (슬랙 카빙 포함)" if carve_slack else ""))
        # 여기서부터 실패하거나 취소되면 방금 만든 사건 레코드를 되돌린다 -
        # 안 그러면 output_folder가 빈 고아 레코드가 History에 그대로 쌓인다.
        extraction = run_full_extraction(source_copy_path, output_dir, slack=carve_slack,
                                         cancel_event=cancel_event)

        report("영상 길이 계산 중...")
        dur = duration_mod.get_duration_sec(source_copy_path, routing.container,
                                            engine_output_dir=output_dir)

        report("이상치 확인 중...")
        outliers.mark_outliers(extraction.points)
        extraction.avi_repaired = _avi_was_repaired(output_dir)
        if extraction.status == "ok" and not extraction.fix_count:
            extraction.status = STATUS_GPS_UNTRUSTED
            extraction.status_detail = "모든 GPS 좌표가 검증 기준에서 제외됐습니다."

        report("위험운전 행동 분석 중...")
        vehicle_type = driving_events.normalize_vehicle(vehicle_type)
        events = driving_events.detect_driving_events(extraction.points, vehicle_type)

        _record_engine_runs(history_store, case_id, extraction.engine_runs, output_dir)
        write_artifact_manifest(output_dir)
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
        )
    except BaseException:
        _rollback_case(history_store, case_id, case_folder)
        raise


def reopen_case(case: CaseRecord) -> PipelineResult:
    if case.segments:
        return _reopen_sequence(case)
    points, primary, time_source = (
        load_existing_results(case.output_folder) if case.output_folder else ([], None, "")
    )
    outliers.mark_outliers(points)

    # 차종 선택이 생기기 전 사건(임계값 m/s²만 저장)은 승용차 기준으로 다시 판정한다.
    vehicle_type = driving_events.normalize_vehicle(case.analysis_settings.get("vehicle_type"))
    events = driving_events.detect_driving_events(points, vehicle_type)

    routing = RoutingResult(
        container=case.detected_format, supported=True,
        reason="저장된 사건을 다시 열었습니다 (재추출 없이 기존 결과 표시).",
    )
    case_folder = os.path.dirname(case.output_folder) if case.output_folder else ""
    metadata = {}
    try:
        with open(os.path.join(case_folder, "case.json"), encoding="utf-8") as f:
            metadata = json.load(f)
        if not isinstance(metadata, dict):
            metadata = {}
    except (OSError, ValueError):
        pass
    # 상태는 DB(analysis_status) > case.json 순으로 믿는다. 둘 다 없으면 좌표가 있어도 'ok'로 올리지
    # 않는다 - case.json이 0바이트로 잘린 timed_out 사건이 리포트에 'ok'로 찍혔다(리뷰 #30).
    # "analyzing"은 분석 도중 프로세스가 죽은 사건이다(리뷰 #31).
    status = case.analysis_status or str(metadata.get("status") or "")
    if status == "analyzing":
        status, detail = "engine_failed", "분석이 끝나기 전에 프로그램이 종료됐습니다. 이 사건은 다시 분석하세요."
    elif not status:
        status, detail = "unknown", "저장된 분석 상태를 확인할 수 없습니다."
    else:
        detail = str(metadata.get("status_detail") or "")
    extraction = ExtractionResult(
        routing=routing, points=points, engine_runs=[],
        status=status, status_detail=detail,
        warnings=_collect_warnings(case.output_folder) if case.output_folder else [],
        avi_repaired=case.avi_repaired,
        slack_points=load_slack_points(case.output_folder) if case.output_folder else [],
        used_input_path=case.source_video_path, primary_source_file=primary,
        time_source=time_source,
    )
    case_folder = os.path.dirname(case.output_folder) if case.output_folder else ""
    rear_copy = (os.path.join(case_folder, "source", case.rear_video_filename)
                 if case_folder and case.rear_video_filename else "")
    copy_name = str(metadata.get("source_video_filename") or case.source_video_filename)
    return PipelineResult(
        case_id=case.id or -1,
        case_folder=case_folder,
        source_copy_path=(os.path.join(case_folder, "source", copy_name) if case_folder else ""),
        artifacts_verified=verify_artifact_manifest(case.output_folder),
        extraction=extraction,
        duration_sec=case.duration_sec,
        driving_events=events,
        sha256=case.source_video_sha256,
        vehicle_type=vehicle_type,
        # 사본이 없어도 경로를 남긴다 - 그래야 무결성 표시등이 '사본 없음'(회색)으로 알린다. 예전엔
        # 비워서 후방이 조용히 빠지고 표시등이 초록이 됐다(리뷰 #17).
        rear_copy_path=rear_copy,
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
        combined, events = combine_segments(segments)
        records = [segment_record(seg, routings[seg.index].container) for seg in segments]
        track_mode = next((it.track_mode for it in items if it.track_mode), "")
        write_artifact_manifest(output_dir)
        _write_case_json(case_folder, case_id, case_number, examiner, memo, segments[0].primary_copy_path,
                          known[first], routings[0], combined, offset, settings, events, carve_slack,
                          vehicle_type, track_mode=track_mode, created_at=analyzed_at, segments=records,
                          original_filename=os.path.basename(first))
        history_store.update_case_extraction(case_id, duration_sec=offset,
                                             avi_repaired=combined.avi_repaired, output_folder=output_dir)
        history_store.set_analysis_status(case_id, combined.status)
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


def _reopen_sequence(case: CaseRecord) -> PipelineResult:
    case_folder = os.path.dirname(case.output_folder) if case.output_folder else ""
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
        artifacts_verified=verify_artifact_manifest(case.output_folder))
