from __future__ import annotations

import glob
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from core import acceleration, duration as duration_mod, format_sniffer, hashing, outliers
from core.acceleration import FlaggedSegment
from core.format_sniffer import RoutingResult
from engine.engine_adapter import (
    CancelledError,
    ExtractionResult,
    TrackPoint,
    load_existing_results,
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
    flagged_segments: List[FlaggedSegment]
    sha256: str
    accel_threshold_mps2: float
    rear_copy_path: str = ""   # 후방 영상 사본(같이 보기로 올린 경우). 없으면 빈 문자열

    @property
    def points(self) -> List[TrackPoint]:
        return self.extraction.points


def _safe_case_folder_name(case_number: str, case_id: int) -> str:
    safe = "".join(c for c in case_number if c.isalnum() or c in "-_") or "case"
    return f"{safe}_{case_id}"


def run_analysis_pipeline(
    video_path: str,
    case_number: str,
    examiner: str,
    memo: str,
    settings: Dict,
    cases_root_dir: str,
    history_store: HistoryStore,
    accel_threshold_mps2: float = acceleration.DEFAULT_THRESHOLD_MPS2,
    carve_slack: bool = False,
    progress_cb: ProgressCallback = None,
    cancel_event=None,
    precomputed_sha256: str = "",
    rear_video_path: str = "",
) -> PipelineResult:
    def report(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("분석이 취소되었습니다.")

    check_cancelled()
    # 같은 파일인지 확인하느라 화면에서 이미 계산했으면 다시 읽지 않는다(큰 영상은 수 초).
    if precomputed_sha256:
        sha256 = precomputed_sha256
    else:
        report("파일 해시 계산 중 (SHA-256)...")
        sha256 = hashing.sha256_file(video_path)

    check_cancelled()
    report("파일 형식 확인 중...")
    routing = format_sniffer.sniff(video_path)

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
    )
    case_id = history_store.add_case(provisional)

    case_folder = os.path.join(cases_root_dir, _safe_case_folder_name(case_number, case_id))
    source_dir = os.path.join(case_folder, "source")
    output_dir = os.path.join(case_folder, "engine_output")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    check_cancelled()
    report("원본 영상을 사건 폴더로 복사 중 (무결성 보존)...")
    source_copy_path = os.path.join(source_dir, os.path.basename(video_path))
    rear_copy_path = ""
    try:
        shutil.copy2(video_path, source_copy_path)
        # 후방 영상은 분석하지 않고 같이 보기용으로만 보존한다. 이름이 전방과 같으면
        # (드물지만) 덮어쓰지 않도록 접두어를 붙인다.
        if rear_video_path and os.path.isfile(rear_video_path):
            check_cancelled()
            report("후방 영상을 사건 폴더로 복사 중...")
            rear_name = os.path.basename(rear_video_path)
            if rear_name == os.path.basename(video_path):
                rear_name = "rear_" + rear_name
            rear_copy_path = os.path.join(source_dir, rear_name)
            shutil.copy2(rear_video_path, rear_copy_path)
    except BaseException:
        history_store.delete_case(case_id)
        shutil.rmtree(case_folder, ignore_errors=True)
        raise

    report("GPS/센서 메타데이터 추출 중..." + (" (슬랙 카빙 포함)" if carve_slack else ""))
    # 여기서부터 실패하거나 취소되면 방금 만든 사건 레코드를 되돌린다 -
    # 안 그러면 output_folder가 빈 고아 레코드가 History에 그대로 쌓인다.
    try:
        extraction = run_full_extraction(source_copy_path, output_dir, slack=carve_slack,
                                         cancel_event=cancel_event)
    except BaseException:
        history_store.delete_case(case_id)
        shutil.rmtree(case_folder, ignore_errors=True)
        raise

    report("영상 길이 계산 중...")
    dur = duration_mod.get_duration_sec(source_copy_path, routing.container,
                                        engine_output_dir=output_dir)

    report("이상치 확인 중...")
    outliers.mark_outliers(extraction.points)

    report("급가·감속 구간 분석 중...")
    flagged = acceleration.compute_flagged_segments(extraction.points, accel_threshold_mps2)

    _write_case_json(case_folder, case_id, case_number, examiner, memo, video_path, sha256,
                      routing, extraction, dur, settings, flagged, carve_slack,
                      rear_copy_path=rear_copy_path)

    for run in extraction.engine_runs:
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

    history_store.update_case_extraction(
        case_id, duration_sec=dur, avi_repaired=_avi_was_repaired(output_dir),
        output_folder=output_dir,
    )
    if rear_copy_path:
        history_store.set_rear_video(case_id, os.path.basename(rear_copy_path))

    report("완료")
    return PipelineResult(
        case_id=case_id,
        case_folder=case_folder,
        source_copy_path=source_copy_path,
        extraction=extraction,
        duration_sec=dur,
        flagged_segments=flagged,
        sha256=sha256,
        accel_threshold_mps2=accel_threshold_mps2,
        rear_copy_path=rear_copy_path,
    )


def reopen_case(case: CaseRecord) -> PipelineResult:
    points, primary, time_source = (
        load_existing_results(case.output_folder) if case.output_folder else ([], None, "")
    )
    outliers.mark_outliers(points)

    threshold = float(case.analysis_settings.get("accel_threshold_mps2",
                                                  acceleration.DEFAULT_THRESHOLD_MPS2))
    flagged = acceleration.compute_flagged_segments(points, threshold)

    routing = RoutingResult(
        container=case.detected_format, supported=True,
        reason="저장된 사건을 다시 열었습니다 (재추출 없이 기존 결과 표시).",
    )
    extraction = ExtractionResult(
        routing=routing, points=points, engine_runs=[],
        used_input_path=case.source_video_path, primary_source_file=primary,
        time_source=time_source,
    )
    case_folder = os.path.dirname(case.output_folder) if case.output_folder else ""
    rear_copy = (os.path.join(case_folder, "source", case.rear_video_filename)
                 if case_folder and case.rear_video_filename else "")
    return PipelineResult(
        case_id=case.id or -1,
        case_folder=case_folder,
        source_copy_path=(os.path.join(case_folder, "source", case.source_video_filename)
                          if case_folder else ""),
        extraction=extraction,
        duration_sec=case.duration_sec,
        flagged_segments=flagged,
        sha256=case.source_video_sha256,
        accel_threshold_mps2=threshold,
        rear_copy_path=rear_copy if rear_copy and os.path.isfile(rear_copy) else "",
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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except (OSError, ValueError):
        return False


def _avi_was_repaired(output_dir: str) -> bool:
    return bool(glob.glob(os.path.join(output_dir, "**", "*_wo_slack.avi"), recursive=True))


def _write_case_json(case_folder, case_id, case_number, examiner, memo, video_path, sha256,
                      routing, extraction: ExtractionResult, dur, settings, flagged,
                      carve_slack, rear_copy_path: str = "") -> None:
    case_json_path = os.path.join(case_folder, "case.json")
    with open(case_json_path, "w", encoding="utf-8") as f:
        json.dump({
            "case_id": case_id,
            "case_number": case_number,
            "examiner": examiner,
            "memo": memo,
            "source_video_filename": os.path.basename(video_path),
            "source_video_sha256": sha256,
            "detected_format": routing.container,
            "avi_repaired": _avi_was_repaired(os.path.join(case_folder, "engine_output")),
            "status": extraction.status,
            "status_detail": extraction.status_detail,
            "slack_point_count": len(extraction.slack_points),
            "extension_mismatch": routing.extension_mismatch,
            "engine": "integration_blackbox",
            "slack_carving": carve_slack,
            "duration_sec": dur,
            "time_source": extraction.time_source,
            "analysis_settings": settings,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "point_count": len(extraction.points),
            "gps_fix_count": extraction.fix_count,
            "outlier_count": extraction.outlier_count,
            "flagged_segment_count": len(flagged),
            "flagged_accel_count": acceleration.count_by_kind(flagged)[0],
            "flagged_decel_count": acceleration.count_by_kind(flagged)[1],
            "rear_video_filename": os.path.basename(rear_copy_path) if rear_copy_path else "",
        }, f, ensure_ascii=False, indent=2)
