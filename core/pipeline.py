from __future__ import annotations

import glob
import json
import os
import shutil
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
    analyzed_at = datetime.now().isoformat(timespec="seconds")
    # 같은 파일인지 확인하느라 화면에서 이미 계산했으면 다시 읽지 않는다(큰 영상은 수 초).
    if precomputed_sha256:
        sha256 = precomputed_sha256
    else:
        report("파일 해시 계산 중 (SHA-256)...")
        sha256 = hashing.sha256_file(video_path)

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

        check_cancelled()
        report("원본 영상을 사건 폴더로 복사 중 (무결성 보존)...")
        source_copy_path = os.path.join(source_dir, os.path.basename(video_path))
        rear_copy_path = ""
        rear_sha256 = ""
        shutil.copy2(video_path, source_copy_path)
        report("분석 사본 SHA-256 검증 중...")
        if hashing.sha256_file(source_copy_path) != sha256:
            raise ValueError("원본 해시와 분석 사본 SHA-256이 다릅니다. 분석을 중단했습니다.")
        check_cancelled()
        # 후방 영상은 분석하지 않고 같이 보기용으로만 보존한다. 이름이 전방과 같으면
        # (드물지만) 덮어쓰지 않도록 접두어를 붙인다.
        if rear_video_path:
            if not os.path.isfile(rear_video_path):
                raise FileNotFoundError("후방 원본 영상을 찾을 수 없습니다.")
            rear_sha256 = hashing.sha256_file(rear_video_path)
            check_cancelled()
            report("후방 영상을 사건 폴더로 복사 중...")
            rear_name = os.path.basename(rear_video_path)
            if rear_name == os.path.basename(video_path):
                rear_name = "rear_" + rear_name
            rear_copy_path = os.path.join(source_dir, rear_name)
            shutil.copy2(rear_video_path, rear_copy_path)
            if hashing.sha256_file(rear_copy_path) != rear_sha256:
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

        _write_case_json(case_folder, case_id, case_number, examiner, memo, video_path, sha256,
                          routing, extraction, dur, settings, events, carve_slack,
                          vehicle_type, rear_copy_path=rear_copy_path, track_mode=track_mode,
                          created_at=analyzed_at)

        _record_engine_runs(history_store, case_id, extraction.engine_runs, output_dir)

        history_store.update_case_extraction(
            case_id, duration_sec=dur, avi_repaired=_avi_was_repaired(output_dir),
            output_folder=output_dir,
        )
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
        history_store.delete_case(case_id)
        shutil.rmtree(case_folder, ignore_errors=True)
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
    # Legacy cases have no reliable success marker: never silently promote failures to OK.
    fallback_status = "ok" if any(p.has_fix for p in points) else "unknown"
    extraction = ExtractionResult(
        routing=routing, points=points, engine_runs=[],
        status=metadata.get("status", fallback_status),
        status_detail=metadata.get("status_detail", "저장된 분석 상태를 확인할 수 없습니다." if fallback_status == "unknown" else ""),
        warnings=_collect_warnings(case.output_folder) if case.output_folder else [],
        avi_repaired=case.avi_repaired,
        slack_points=load_slack_points(case.output_folder) if case.output_folder else [],
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
        driving_events=events,
        sha256=case.source_video_sha256,
        vehicle_type=vehicle_type,
        rear_copy_path=rear_copy if rear_copy and os.path.isfile(rear_copy) else "",
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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except (OSError, ValueError):
        return False


def _avi_was_repaired(output_dir: str) -> bool:
    return bool(glob.glob(os.path.join(output_dir, "**", "*_wo_slack.avi"), recursive=True))


def _write_case_json(case_folder, case_id, case_number, examiner, memo, video_path, sha256,
                      routing, extraction: ExtractionResult, dur, settings, events,
                      carve_slack, vehicle_type: str, rear_copy_path: str = "", track_mode: str = "",
                      created_at: str = "", segments: Optional[List[Dict]] = None) -> None:
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
            "warnings": extraction.warnings,
            "copy_sha256_verified": True,
            "rear_sha256": hashing.sha256_file(rear_copy_path) if rear_copy_path else "",
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
        }, f, ensure_ascii=False, indent=2)


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
        if status == "ok" and seg.extraction.status != "ok":
            status = seg.extraction.status
            detail = f"{seg.label}: {seg.extraction.status_detail or seg.extraction.status_message}"
    first = segments[0].extraction
    combined = ExtractionResult(
        routing=first.routing, points=points, engine_runs=runs,
        used_input_path=segments[0].primary_copy_path, primary_source_file=first.primary_source_file,
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
    analyzed_at = datetime.now().isoformat(timespec="seconds")
    known = dict(precomputed_sha256 or {})

    def sha_of(path: str) -> str:
        if not known.get(path):
            check_cancelled()
            report(f"파일 해시 계산 중 (SHA-256) - {os.path.basename(path)}")
            known[path] = hashing.sha256_file(path)
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
        source_video_path=first, source_video_filename=os.path.basename(first),
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
                name = f"{n + 1:02d}_{os.path.basename(path)}"
                if role == "rear" and item.front and name == f"{n + 1:02d}_{os.path.basename(item.front)}":
                    name = f"{n + 1:02d}_rear_{os.path.basename(path)}"
                copy = os.path.join(source_dir, name)
                shutil.copy2(path, copy)
                if hashing.sha256_file(copy) != known[path]:
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
        _write_case_json(case_folder, case_id, case_number, examiner, memo, first, known[first],
                          routings[0], combined, offset, settings, events, carve_slack,
                          vehicle_type, track_mode=track_mode, created_at=analyzed_at, segments=records)
        history_store.update_case_extraction(case_id, duration_sec=offset,
                                             avi_repaired=combined.avi_repaired, output_folder=output_dir)
        history_store.set_segments(case_id, records)
        if track_mode:
            history_store.set_track_mode(case_id, track_mode)
        report("완료")
        return PipelineResult(
            case_id=case_id, case_folder=case_folder, source_copy_path=segments[0].primary_copy_path,
            extraction=combined, duration_sec=offset, driving_events=events, sha256=known[first],
            vehicle_type=vehicle_type, track_mode=track_mode, analyzed_at=analyzed_at, segments=segments)
    except BaseException:
        history_store.delete_case(case_id)
        shutil.rmtree(case_folder, ignore_errors=True)
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
            status=rec.get("status") or ("ok" if any(p.has_fix for p in points) else "unknown"),
            status_detail=rec.get("status_detail") or "",
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
        analyzed_at=case.created_at, segments=segments)
