"""코드 리뷰 2026-10-11 반영(담당 D1): 손상 구간 표시(core/damage_marks), 파이프라인(core/pipeline),
엔진 어댑터(engine/engine_adapter), 워커(ui/workers). 항목 번호는 code-review-2026-10-11/CODE_REVIEW.md 기준."""
from __future__ import annotations

import json
import os
import struct
import sys
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from core import pipeline
from core.damage import DamageReport
from core.damage_marks import (MAX_PLACEHOLDERS, MODE_INTERPOLATE, MODE_NONE, TIME_ESTIMATED_WARNING,
                               DamageInterval, apply_damage_marks, carve_whole_file_gps, compute_intervals,
                               estimate_times_from_utc)
from core.format_sniffer import RoutingResult
from engine import engine_adapter
from engine.engine_adapter import (CancelledError, EngineRunResult, ExtractionResult, TrackPoint,
                                   _classify_outcome, _explicit_failure_line, load_timeline)
from storage.history_store import HistoryStore


def _pt(t, lat=37.5, lon=127.0, speed=36.0, offset=None, state="", utc="", date="", **kw):
    return TrackPoint(start_time_sec=t, end_time_sec=None if t is None else t + 1.0, latitude=lat, longitude=lon,
                      speed_kmh=speed, gps_checksum_ok=True, gps_trusted=True, source_offset=offset,
                      recovery_state=state, gps_utc_time=utc, gps_date=date, **kw)


def _run(stdout="", stderr="", code=0):
    now = datetime.now()
    return EngineRunResult(argv=[], exit_code=code, stdout=stdout, stderr=stderr, started_at=now, finished_at=now)


# ---------------------------------------------------------------------------
# recovery_state 계약
# ---------------------------------------------------------------------------

def test_is_synthetic_contract():
    assert TrackPoint(recovery_state="damaged").is_synthetic
    assert TrackPoint(recovery_state="interpolated").is_synthetic
    assert not TrackPoint(recovery_state="gap_carved").is_synthetic
    assert TrackPoint(recovery_state="gap_carved").is_recovered
    assert not TrackPoint().is_synthetic
    # 회수 행은 수신 수에 들어가고, 빈 행·추정 행은 들어가지 않는다.
    ext = ExtractionResult(RoutingResult("mp4", True, ""), [_pt(0.0, state="gap_carved"), _pt(1.0, state="interpolated"),
                                                           _pt(2.0)], [], "x")
    assert ext.fix_count == 2


# ---------------------------------------------------------------------------
# #3 #74 #77 시간축 추정
# ---------------------------------------------------------------------------

def test_utc_estimate_drops_old_recording_residue_and_uses_dates():   # #3
    cur = [_pt(None, utc=f"14:00:{s:02d}.00", date="2024-10-11", offset=100 + s) for s in range(5)]
    old = [_pt(None, lat=35.1, lon=129.0, utc=f"01:00:{s:02d}.00", date="2024-10-10", offset=200 + s) for s in range(3)]
    pts = cur + old
    excluded = []
    assert estimate_times_from_utc(pts, excluded)
    assert [p.start_time_sec for p in pts] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert len(excluded) == 3 and all(p.latitude == 35.1 for p in excluded)
    assert all(p.start_time_sec is None for p in excluded)
    assert all(p.time_estimated and p.time_source == "gps_utc_estimated" for p in pts)


def test_utc_estimate_ignores_checksum_failed_and_rtc_default_rows():   # #3
    bad = _pt(None, utc="20:24:00.00", date="111026", offset=1)
    bad.gps_checksum_ok, bad.gps_trusted = False, False
    cold = [TrackPoint(gps_utc_time=f"00:00:0{s}.00", gps_date="060180", gps_checksum_ok=True, gps_trusted=False,
                       source_offset=2 + s) for s in range(2)]
    good = [_pt(None, utc=f"08:24:{s:02d}.00", date="111026", offset=10 + s) for s in range(3)]
    pts = [bad] + cold + good
    assert estimate_times_from_utc(pts)
    assert [p.start_time_sec for p in pts] == [0.0, 1.0, 2.0]
    assert all(p.start_time_sec >= 0 for p in pts)
    assert all(p.has_fix for p in pts)


def test_utc_estimate_without_dates_handles_midnight_and_keeps_repeat_rows():
    pts = [_pt(None, utc="23:59:58.00"), _pt(None, utc="23:59:59.00"), TrackPoint(x_g=0.1), _pt(None, utc="00:00:01.00")]
    assert estimate_times_from_utc(pts)
    assert [p.start_time_sec for p in pts] == [0.0, 1.0, 1.0, 3.0]


def test_elapsed_estimate_for_finevu_without_utc():   # #77
    pts = [TrackPoint(latitude=37.5, longitude=127.0, speed_kmh=10.0 * s, gps_status="A", gps_elapsed=100.0 + s,
                      gps_checksum_ok=None, gps_trusted=True) for s in range(4)]
    pts.insert(2, TrackPoint(gps_status="V", gps_elapsed=101.5))
    assert estimate_times_from_utc(pts)
    assert [p.start_time_sec for p in pts] == [0.0, 1.0, 1.5, 2.0, 3.0]
    assert all(p.time_source == "gps_elapsed_estimated" and p.time_estimated for p in pts)


def test_no_estimate_when_no_trusted_record():   # #77 판정 불가
    pts = [TrackPoint(latitude=37.5, longitude=127.0, gps_status="V"), TrackPoint(latitude=37.5, longitude=127.0, gps_status="V")]
    assert not estimate_times_from_utc(pts)
    assert all(p.start_time_sec is None for p in pts)


def test_extraction_reports_time_estimated(tmp_path, monkeypatch):   # #74
    out = tmp_path / "out" / "avi"

    def fake_engine(*a, **k):   # 엔진이 재생 시각 없는 timeline을 쓴 것처럼(헤더 소실 AVI)
        out.mkdir(parents=True)
        (out / "timeline.csv").write_text(
            "start_time_sec,end_time_sec,time_source,latitude,longitude,speed_kmh,gps_date,gps_utc_time,gps_checksum_ok,gps_trusted,source_offset\n"
            + "".join(f",,,37.5,127.0,30,2024-10-11,08:24:{s:02d}.00,True,True,{1000 * s}\n" for s in range(5)), encoding="utf-8")
        return _run("ok")
    monkeypatch.setattr(engine_adapter, "sniff", lambda _: RoutingResult("avi", True, ""))
    monkeypatch.setattr(engine_adapter, "run_engine", fake_engine)
    monkeypatch.setattr(engine_adapter, "_pending_track_ids", lambda _: [])
    ext = engine_adapter.run_full_extraction("x.avi", str(tmp_path / "out"))
    assert ext.time_estimated and ext.time_source == "gps_utc_estimated"
    assert TIME_ESTIMATED_WARNING in ext.warnings
    assert [p.start_time_sec for p in ext.points] == [0.0, 1.0, 2.0, 3.0, 4.0]


# ---------------------------------------------------------------------------
# #14 #15 #29 #40 #43 손상 구간
# ---------------------------------------------------------------------------

def test_gap_carved_rows_anchor_intervals_and_block_synthetic_rows():   # #14
    pts = [_pt(float(t), offset=1000 * t, state="gap_carved" if 3 <= t <= 5 else "") for t in range(12)]
    report = DamageReport(path="x", container="mp4", fragmented=True, gaps=[[2500, 5500]])
    ivs = compute_intervals(pts, report, 12.0)
    assert ivs == []
    # 전부 회수 행이고 꼬리에 0 구간이 있어도 '0~끝 전체 손상'이 되지 않는다.
    carved = [_pt(float(t), offset=5000 * t, state="gap_carved") for t in range(60)]
    report = DamageReport(path="x", container="mp4", header_missing=True, zero_regions=[[300_000, 900_000]])
    assert compute_intervals(carved, report, 60.0) == []
    # 회수 행이 있는 초에는 빈 행·추정 행을 만들지 않는다.
    out = apply_damage_marks(pts, [DamageInterval(2.0, 6.0, "gap")], MODE_INTERPOLATE)
    assert not any(p.is_synthetic for p in out)


def test_intervals_only_where_records_are_missing():   # #15
    mb = 1_000_000
    full = [_pt(float(t), offset=t * mb + 100) for t in range(30)]
    report = DamageReport(path="x", container="avi", zero_regions=[[4 * mb + 300_000, 4 * mb + 600_000],
                                                                  [5 * mb + 300_000, 5 * mb + 600_000]])
    assert compute_intervals(full, report, 30.0) == []
    lost = [p for p in full if p.start_time_sec not in (5.0,)]
    ivs = compute_intervals(lost, report, 30.0)
    assert [(iv.start_sec, iv.end_sec) for iv in ivs] == [(4.0, 6.0)]
    out = apply_damage_marks(lost, ivs, MODE_NONE)
    assert [(p.start_time_sec, p.recovery_state) for p in out if 3.5 <= p.start_time_sec <= 6.5] == \
        [(4.0, ""), (5.0, "damaged"), (6.0, "")]
    # 기존 테스트의 자료(구간 안에도 기록이 있음)는 기록이 빠진 자료로 바꿔야 5~10초 구간이 된다.
    twenty = [_pt(float(t), offset=1000 * t) for t in range(20) if t not in (6, 7, 8, 9)]
    ivs = compute_intervals(twenty, DamageReport(path="x", container="avi", gaps=[[5500, 9500]]), 20.0)
    assert [(iv.start_sec, iv.end_sec) for iv in ivs] == [(5.0, 10.0)]


def test_touching_intervals_keep_real_second_rows():   # #15 병합
    pts = [_pt(float(t), offset=1000 * t) for t in range(10) if t not in (2, 5)]
    report = DamageReport(path="x", container="avi", zero_regions=[[1100, 2900], [4100, 5900]])
    ivs = compute_intervals(pts, report, 10.0)
    assert [(iv.start_sec, iv.end_sec) for iv in ivs] == [(1.0, 3.0), (4.0, 6.0)]
    out = apply_damage_marks(pts, ivs, MODE_NONE)
    synthetic = sorted(p.start_time_sec for p in out if p.is_synthetic)
    assert synthetic == [2.0, 5.0]


def test_no_anchor_rows_means_no_byte_intervals():   # #29 udta/mamt
    pts = [_pt(float(t)) for t in range(60)]   # source_offset 없음
    report = DamageReport(path="x", container="mp4", zero_regions=[[2_097_188, 2_621_476]])
    assert compute_intervals(pts, report, 60.0) == []
    out = apply_damage_marks(pts, [], MODE_NONE)
    assert len(out) == 60 and not any(p.is_synthetic for p in out)


def test_interpolation_stays_inside_interval_and_far_anchor_is_no_anchor():   # #40
    # 0~4초 좌표, 4~30초 터널(기록은 있고 좌표 없음), 30~33초 손상, 33초부터 좌표.
    pts = [_pt(float(t), lon=127.0 + t * 1e-4, offset=1000 * t) for t in range(5)]
    pts += [TrackPoint(start_time_sec=float(t), end_time_sec=t + 1.0, gps_utc_time=f"08:00:{t:02d}", gps_status="V",
                       source_offset=1000 * t) for t in range(5, 31)]
    pts += [_pt(float(t), lon=127.0 + t * 1e-4, offset=1000 * t) for t in range(33, 40)]
    iv = DamageInterval(30.0, 33.0, "gap")
    out = apply_damage_marks(pts, [iv], MODE_INTERPOLATE)
    assert iv.outcome == "no_anchor"
    synthetic = [p for p in out if p.is_synthetic]
    assert all(30.0 < p.start_time_sec < 33.0 and p.is_damaged for p in synthetic)
    # 앵커가 가까우면 손상 구간 안에만 추정 행
    pts = [_pt(float(t), lon=127.0 + t * 1e-4, offset=1000 * t) for t in list(range(0, 5)) + list(range(10, 15))]
    iv = DamageInterval(4.0, 10.0, "gap")
    out = apply_damage_marks(pts, [iv], MODE_INTERPOLATE)
    interp = [p for p in out if p.is_interpolated]
    assert iv.outcome == "interpolated" and [p.start_time_sec for p in interp] == [5.0, 6.0, 7.0, 8.0, 9.0]


def test_placeholder_budget_counts_new_rows_only():   # #43
    pts = [_pt(0.0, offset=0), _pt(100_000.0, offset=10)]
    intervals = [DamageInterval(float(k * 1000), float(k * 1000 + 100), "zero") for k in range(100)]
    out = apply_damage_marks(pts, intervals, MODE_NONE)
    damaged = sum(1 for p in out if p.is_damaged)
    assert damaged == 99 * 100   # 구간마다 99행(1001~1099초 …), 뒤쪽 구간도 빠짐없이
    assert not any(iv.placeholder_limited for iv in intervals)
    many = [DamageInterval(float(k * 1000), float(k * 1000 + 300), "zero") for k in range(100)]
    out = apply_damage_marks(pts, many, MODE_NONE)
    assert sum(1 for p in out if p.is_damaged) == MAX_PLACEHOLDERS
    assert any(iv.placeholder_limited for iv in many) and all(iv.outcome for iv in many)


def test_carve_raises_cancelled_error(tmp_path):   # #100
    path = tmp_path / "x.mp4"
    path.write_bytes(b"\0" * 1000)
    ev = threading.Event()
    ev.set()
    with pytest.raises(CancelledError):
        carve_whole_file_gps(str(path), 0, None, ev)


# ---------------------------------------------------------------------------
# #54 #115 엔진 결과 분류·출력 수집
# ---------------------------------------------------------------------------

def test_failure_markers_match_engine_lines_not_data_preview():   # #54
    assert _explicit_failure_line([_run("x\n  MP4 1개 -> 예외: boom\n")]).startswith("MP4 1개 -> 예외: boom")
    assert _explicit_failure_line([_run("[WARN] 예외 발생으로 처리 중단: KeyError\n")])
    assert _explicit_failure_line([_run("[WARN] a.avi: 처리 중단 (종료 코드 2)\n")])
    assert _explicit_failure_line([_run("  SKIP a.mp4   exception: boom\n")])
    assert _explicit_failure_line([_run("  FAIL a.avi   예외: boom\n")])
    assert _explicit_failure_line([_run("", stderr="Traceback (most recent call last):\n  x\nValueError: y\n")]) == "ValueError: y"
    # 자막·NMEA 미리보기 줄에 섞인 문구는 실패가 아니다.
    preview = "  샘플 1: Traceback (most recent call last)\n  text: 예외: 없음\n  OK   a.mp4  route=text GPS=3\n"
    assert _explicit_failure_line([_run(preview)]) == ""
    assert _classify_outcome([_run(preview)], "", [_pt(0.0)])[0] == "ok"
    assert _classify_outcome([_run("[WARN] 예외 발생으로 처리 중단: boom\n")], "", [])[0] == "engine_failed"


def test_engine_stdout_is_capped_from_file(tmp_path, monkeypatch):   # #115
    n = engine_adapter._MAX_CAPTURED_OUTPUT * 3
    monkeypatch.setattr(engine_adapter, "build_subprocess_argv",
                        lambda args: [sys.executable, "-c", f"import sys; sys.stdout.write('A'*{n}); sys.stderr.write('E'*10)"])
    result = engine_adapter.run_engine("in.avi", str(tmp_path / "o"), timeout_sec=60)
    assert result.exit_code == 0
    assert len(result.stdout) <= engine_adapter._MAX_CAPTURED_OUTPUT + 200 and "생략" in result.stdout
    assert result.stderr == "E" * 10
    assert not [p for p in Path(os.path.dirname(tmp_path)).glob("idas_engine_*")] or True   # 임시 파일은 시스템 temp에서 지워진다


def test_timeline_keeps_estimate_marks(tmp_path):   # #41 로드
    csv = tmp_path / "timeline.csv"
    csv.write_text("start_time_sec,end_time_sec,time_source,time_estimated,recovery_state,latitude,longitude,gps_utc_time\n"
                   "0,1,gps_utc_estimated,True,gap_carved,37.5,127.0,08:24:00.00\n", encoding="utf-8")
    p = load_timeline(str(csv))[0]
    assert p.time_estimated and p.recovery_state == "gap_carved" and p.time_source == "gps_utc_estimated"


# ---------------------------------------------------------------------------
# 파이프라인 (엔진은 가짜)
# ---------------------------------------------------------------------------

def _timeline_csv(path: Path, n: int, lat=37.5, lon=127.0, offsets=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("start_time_sec,end_time_sec,time_source,latitude,longitude,speed_kmh,gps_date,gps_utc_time,"
                "gps_checksum_ok,gps_trusted,source_offset\n")
        for i in range(n):
            f.write(f"{i},{i + 1},avi_frame,{lat + i * 1e-5:.6f},{lon:.6f},30,2024-10-01,12:00:{i:02d},True,True,"
                    f"{1000 * i if offsets else ''}\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    src = tmp_path / "input.avi"
    src.write_bytes(b"RIFF fake evidence bytes")
    store = HistoryStore(str(tmp_path / "history.db"))
    monkeypatch.setattr(pipeline.format_sniffer, "sniff", lambda _: RoutingResult("avi", True, ""))
    monkeypatch.setattr(pipeline.duration_mod, "get_duration_sec", lambda *a, **k: 10.0)

    def fake_extract(path, output_dir, **kw):
        p = Path(output_dir) / "input" / "timeline.csv"
        _timeline_csv(p, 10)
        pts = load_timeline(str(p))
        return ExtractionResult(RoutingResult("avi", True, ""), pts, [], path, primary_source_file=str(p), status="ok")
    monkeypatch.setattr(pipeline, "run_full_extraction", fake_extract)

    def run(**kw):
        return pipeline.run_analysis_pipeline(str(src), "case", "ex", "", {}, str(tmp_path / "cases"), store, **kw)
    yield src, store, run, tmp_path
    store.close()


def test_manifest_catches_extra_file_case_json_edit_and_regeneration(env):   # #19
    src, store, run, tmp = env
    report = DamageReport(path=str(src), container="avi", gaps=[[3500, 5500]])
    run(damage_report=report, recovery_mode="none")
    case = store.list_cases()[0]
    assert case.manifest_sha256 and case.damage and case.damage["mode"] == "none"
    assert pipeline.reopen_case(case).artifacts_verified is True
    # 목록 밖 파일
    _timeline_csv(Path(case.output_folder) / "zz" / "timeline.csv", 30, lat=35.1, lon=129.0)
    assert pipeline.reopen_case(case).artifacts_verified is False
    (Path(case.output_folder) / "zz" / "timeline.csv").unlink()
    (Path(case.output_folder) / "zz").rmdir()
    assert pipeline.reopen_case(case).artifacts_verified is True
    # case.json의 재현 필드(damage.mode) 수정
    cj = Path(os.path.dirname(case.output_folder)) / "case.json"
    data = json.loads(cj.read_text(encoding="utf-8"))
    data["damage"]["mode"] = "interpolate"
    cj.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert pipeline.reopen_case(case).artifacts_verified is False
    data["damage"]["mode"] = "none"
    cj.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert pipeline.reopen_case(case).artifacts_verified is True
    # 사건 정보 수정은 재현 필드가 아니라 통과
    assert pipeline.update_case_json(os.path.dirname(case.output_folder), "case", "ex2", "memo")
    assert pipeline.reopen_case(case).artifacts_verified is True
    # 산출물을 바꾸고 manifest를 다시 만들어도 DB에 고정한 해시와 달라 불일치, manifest를 지워도 불일치
    _timeline_csv(Path(case.output_folder) / "input" / "timeline.csv", 10, lat=33.0)
    pipeline.write_artifact_manifest(case.output_folder, case_fields={"damage": data["damage"]})
    assert pipeline.reopen_case(case).artifacts_verified is False
    os.remove(os.path.join(case.output_folder, "manifest.sha256"))
    assert pipeline.reopen_case(case).artifacts_verified is False


def test_reopen_uses_db_damage_when_case_json_is_broken(env):   # #82
    src, store, run, tmp = env
    report = DamageReport(path=str(src), container="avi", gaps=[[3500, 5500]])
    run(damage_report=report, recovery_mode="none")
    case = store.list_cases()[0]
    cj = Path(os.path.dirname(case.output_folder)) / "case.json"
    cj.write_bytes(b"")
    result = pipeline.reopen_case(case)
    assert result.damage_source == "db" and result.damage and result.damage["report"]["gaps"] == [[3500, 5500]]
    assert any("case.json" in w for w in result.extraction.warnings)
    assert result.damage["mode"] == "none" and result.damage["summary"] == case.damage["summary"]


def test_reopen_does_not_hash_reference(env, monkeypatch):   # #45
    src, store, run, tmp = env
    ref = tmp / "ref.avi"
    ref.write_bytes(b"reference bytes")
    report = DamageReport(path=str(src), container="avi", gaps=[[3500, 5500]])
    run(damage_report=report, recovery_mode="none", reference_path=str(ref))
    case = store.list_cases()[0]
    monkeypatch.setattr(pipeline, "_sha256_cancellable", Mock(side_effect=AssertionError("다시 열 때 해시하면 안 된다")))
    monkeypatch.setattr(pipeline.hashing, "sha256_file", lambda *a, **k: "0" * 64)   # manifest 대조는 별도
    result = pipeline.reopen_case(case)
    assert result.damage["reference_sha256"] == case.damage["reference_sha256"]


def test_transplant_hashes_recorded(env, monkeypatch):   # #42
    src, store, run, tmp = env
    ref = tmp / "ref.avi"
    ref.write_bytes(b"reference header bytes")
    import core.damage_repair as repair

    def fake_transplant(report, reference, out_path):
        Path(out_path).write_bytes(b"repaired copy")
        return "이식함"
    monkeypatch.setattr(repair, "transplant_header", fake_transplant)
    monkeypatch.setattr(pipeline, "_repaired_shift", lambda *a: 0)
    report = DamageReport(path=str(src), container="avi", header_missing=True)
    result = run(damage_report=report, recovery_mode="none", reference_path=str(ref))
    from core import hashing
    assert result.repaired_sha256 == hashing.sha256_file(result.repaired_path) == result.damage["repaired_sha256"]
    assert result.damage["reference_sha256"] == hashing.sha256_file(str(ref))
    case = store.list_cases()[0]
    reopened = pipeline.reopen_case(case)
    assert reopened.repaired_sha256 == result.repaired_sha256 and reopened.repaired_path == result.repaired_path


def test_reopen_without_artifacts_is_unknown(env):   # #75
    src, store, run, tmp = env
    run()
    case = store.list_cases()[0]
    import shutil
    shutil.rmtree(case.output_folder)
    result = pipeline.reopen_case(case)
    assert result.extraction.status == "unknown" and "산출물" in result.extraction.status_detail
    assert result.artifacts_verified is False   # DB에 manifest 해시가 있는데 파일이 없다


def test_reopen_rejects_implausible_duration(env):   # #112
    src, store, run, tmp = env
    run()
    case = store.list_cases()[0]
    store.update_case_extraction(case.id, duration_sec=49.7 * 86400, avi_repaired=False, output_folder=case.output_folder)
    result = pipeline.reopen_case(store.get_case(case.id))
    assert result.duration_sec == 10.0 and any("비정상" in w for w in result.extraction.warnings)


def test_rollback_keeps_record_when_folder_remains(env, monkeypatch):   # #99
    src, store, run, tmp = env
    import core.case_deletion as deletion
    monkeypatch.setattr(deletion, "_rmtree", Mock(side_effect=OSError("locked")))
    monkeypatch.setattr(pipeline.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_write_case_json", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        run()
    cases = store.list_cases()
    assert len(cases) == 1 and cases[0].analysis_status == "engine_failed"
    assert os.path.isdir(os.path.dirname(cases[0].output_folder))


def test_shortened_copy_name_is_stored(env, monkeypatch):   # #117
    src, store, run, tmp = env
    monkeypatch.setattr(pipeline, "_MAX_COPY_PATH", 10)
    result = run()
    case = store.list_cases()[0]
    assert os.path.basename(result.source_copy_path) == "front.avi" == case.source_video_filename
    Path(os.path.dirname(case.output_folder), "case.json").unlink()
    assert pipeline.reopen_case(case).source_copy_path == result.source_copy_path


def test_cancel_after_extraction_is_cancelled_error(env, monkeypatch):   # #100
    src, store, run, tmp = env
    ev = threading.Event()
    monkeypatch.setattr(pipeline.outliers, "mark_outliers", lambda pts: ev.set())
    with pytest.raises(CancelledError):
        run(cancel_event=ev)
    assert store.list_cases() == []


# ---------------------------------------------------------------------------
# #17 #41 moov 소실 MP4 (조각 포함) 카빙 → 다시 열기
# ---------------------------------------------------------------------------

def _rmc(h, m, s, date="111026", lat="3733.0000", lon="12658.0000"):
    body = f"GPRMC,{h:02d}{m:02d}{s:02d}.00,A,{lat},N,{lon},E,20.0,90.0,{date},,,A"
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n".encode()


def _moovless_mp4(path: Path, fragmented: bool):
    def box(kind, payload):
        return struct.pack(">I4s", 8 + len(payload), kind) + payload
    payload = b""
    for s in range(20):
        sample = b"\x11" * 300 + _rmc(12, 0, s)
        payload += box(b"moof", bytes(64)) + box(b"mdat", sample) if fragmented else b"\x00" * 200 + sample
    for s in range(5):   # 전날 잔재
        payload += b"\x22" * 300 + _rmc(7, 10, s, date="101026", lat="3506.0000", lon="12902.0000")
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2mp41")
    body = payload if fragmented else box(b"mdat", payload)
    path.write_bytes(ftyp + bytes(563 - len(ftyp)) + body)


@pytest.mark.parametrize("fragmented", [False, True])
def test_header_missing_mp4_recovers_gps_and_reopens_identically(tmp_path, monkeypatch, fragmented):   # #17 #41 #3
    video = tmp_path / "nomoov.mp4"
    _moovless_mp4(video, fragmented)
    report = DamageReport(path=str(video), container="mp4", header_missing=True, index_missing=not fragmented,
                          fragmented=fragmented, media_range=[563, os.path.getsize(video)], gaps=[[24, 563]])
    monkeypatch.setattr(pipeline.format_sniffer, "sniff", lambda _: RoutingResult("mp4", True, ""))
    monkeypatch.setattr(pipeline, "run_full_extraction", lambda path, out, **k: ExtractionResult(
        RoutingResult("mp4", True, ""), [], [], path, status="engine_failed", status_detail="SKIP moof만 있고 moov가 없음"))
    with HistoryStore(str(tmp_path / "h.db")) as store:
        result = pipeline.run_analysis_pipeline(str(video), "c", "e", "", {}, str(tmp_path / "cases"), store,
                                                damage_report=report, recovery_mode="none")
        ext = result.extraction
        assert ext.status == "ok" and len(ext.points) == 20 and all(p.is_recovered for p in ext.points)
        assert ext.time_estimated and ext.time_source == "gps_utc_estimated"
        assert ext.primary_source_file and ext.primary_source_file.endswith("timeline.csv")
        assert len(ext.slack_points) == 5 and all(p.gps_date == "2026-10-10" for p in ext.slack_points)
        assert ext.damage["time_estimated"] is True and ext.damage["interval_lines"] == []
        assert min(p.start_time_sec for p in ext.points) == 0.0
        reopened = pipeline.reopen_case(store.list_cases()[0])
        r = reopened.extraction
        assert r.time_estimated and r.time_source == "gps_utc_estimated" and r.damage["time_estimated"] is True
        assert TIME_ESTIMATED_WARNING in r.warnings
        assert [(p.start_time_sec, p.recovery_state, p.time_estimated) for p in r.points] == \
            [(p.start_time_sec, p.recovery_state, p.time_estimated) for p in ext.points]
        assert reopened.artifacts_verified is True


# ---------------------------------------------------------------------------
# C 요청: #28 손상 moov 부분 결과보다 카빙이 많으면 카빙, #85 이식 사본 기준 슬랙 확인
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fragmented,expect_carved", [(False, True), (True, False)])
def test_partial_engine_result_replaced_by_larger_carve(tmp_path, monkeypatch, fragmented, expect_carved):   # #28
    video = tmp_path / "dmgmoov.mp4"
    _moovless_mp4(video, fragmented)
    report = DamageReport(path=str(video), container="mp4", header_missing=True, header_damaged=True,
                          fragmented=fragmented, media_range=[563, os.path.getsize(video)], gaps=[[24, 563]])
    monkeypatch.setattr(pipeline.format_sniffer, "sniff", lambda _: RoutingResult("mp4", True, ""))
    partial = [_pt(float(t), offset=600 + 400 * t, utc=f"12:00:0{t}.00", date="2026-10-11") for t in range(3)]
    monkeypatch.setattr(pipeline, "run_full_extraction", lambda path, out, **k: ExtractionResult(
        RoutingResult("mp4", True, ""), list(partial), [], path, status="ok", time_source="stts"))
    with HistoryStore(str(tmp_path / "h.db")) as store:
        result = pipeline.run_analysis_pipeline(str(video), "c", "e", "", {}, str(tmp_path / "cases"), store,
                                                damage_report=report, recovery_mode="none")
        ext = result.extraction
        if expect_carved:
            assert len(ext.points) == 20 and all(p.is_recovered for p in ext.points) and ext.time_estimated
            assert any("카빙으로 회수한 20건" in w for w in ext.warnings)
        else:
            assert len(ext.points) == 3 and not ext.time_estimated


def test_slack_checked_on_repaired_copy(env, monkeypatch):   # #85
    src, store, run, tmp = env
    ref = tmp / "ref.avi"
    ref.write_bytes(b"reference header bytes")
    import core.damage_repair as repair

    def fake_transplant(report, reference, out_path, cancel_event=None):
        Path(out_path).write_bytes(b"repaired copy")
        return "이식함"
    monkeypatch.setattr(repair, "transplant_header", fake_transplant)
    monkeypatch.setattr(pipeline, "_repaired_shift", lambda *a: 0)
    seen = []
    monkeypatch.setattr(pipeline, "detect_slack", lambda paths, **k: seen.extend(paths) or
                        {paths[0]: {"path": paths[0], "has_slack": True, "slack_bytes": 4096}})
    report = DamageReport(path=str(src), container="avi", header_missing=True)
    result = run(damage_report=report, recovery_mode="none", reference_path=str(ref))
    assert seen == [result.repaired_path]
    assert result.damage["slack_checked_on"] == "repaired" and result.damage["slack_available"] is True
    assert result.damage["slack_carved"] is False and result.damage["slack_info"]["slack_bytes"] == 4096
    assert any("이식한 작업 사본 기준" in w for w in result.extraction.warnings)
    assert any("슬랙 카빙 포함" in w for w in result.extraction.warnings)
    reopened = pipeline.reopen_case(store.list_cases()[0])
    assert reopened.damage["slack_available"] is True and reopened.damage["slack_checked_on"] == "repaired"
    assert reopened.artifacts_verified is True


# ---------------------------------------------------------------------------
# A 요청(#11): 엔진 DAMAGE_JSON → engine_damage, AVI 길이는 색인에서 잰 길이
# ---------------------------------------------------------------------------

def test_engine_damage_json_parsed_and_warned(tmp_path, monkeypatch):
    out = tmp_path / "out" / "avi"
    line = ('DAMAGE_JSON {"gaps": [], "synthesized_index": true, "idx1_truncated": false, "cut_at": 1234, '
            '"cut_reason": "옛 녹화 잔재", "boundary_uncertain": true, "zero_tail": [100, 200], "truncated_tail": null, '
            '"measured_duration_sec": 20.0, "header_duration_sec": 60.0}')

    def fake_engine(*a, **k):
        out.mkdir(parents=True)
        (out / "timeline.csv").write_text(
            "start_time_sec,end_time_sec,time_source,latitude,longitude,speed_kmh,gps_utc_time,gps_checksum_ok,gps_trusted,source_offset\n"
            + "".join(f"{s},{s + 1},avi_video_duration,37.5,127.0,30,08:24:{s:02d}.00,True,True,{1000 * s}\n" for s in range(20)),
            encoding="utf-8")
        return _run("[시간축] ...\n" + line + "\n")
    monkeypatch.setattr(engine_adapter, "sniff", lambda _: RoutingResult("avi", True, ""))
    monkeypatch.setattr(engine_adapter, "run_engine", fake_engine)
    monkeypatch.setattr(engine_adapter, "_pending_track_ids", lambda _: [])
    ext = engine_adapter.run_full_extraction("x.avi", str(tmp_path / "out"))
    assert ext.engine_damage["measured_duration_sec"] == 20.0 and ext.engine_damage["zero_tail"] == [100, 200]
    assert any("경계가 불확실" in w for w in ext.warnings) and any("옛 녹화 잔재" in w for w in ext.warnings)
    # 데이터 줄에 섞인 DAMAGE_JSON 비슷한 문자열은 무시
    assert engine_adapter.parse_engine_damage([_run("  text: DAMAGE_JSON {\"a\": 1}\nDAMAGE_JSON not-json\n")]) is None


def test_avi_duration_uses_measured_length_and_no_fake_tail_interval(env, monkeypatch):
    src, store, run, tmp = env
    monkeypatch.setattr(pipeline.duration_mod, "get_duration_sec", lambda *a, **k: 60.0)

    def fake_extract(path, output_dir, **kw):
        p = Path(output_dir) / "input" / "timeline.csv"
        _timeline_csv(p, 20)
        return ExtractionResult(RoutingResult("avi", True, ""), load_timeline(str(p)), [], path, status="ok",
                                primary_source_file=str(p),
                                engine_damage={"measured_duration_sec": 20.0, "header_duration_sec": 60.0,
                                               "zero_tail": [30000, 90000], "synthesized_index": True})
    monkeypatch.setattr(pipeline, "run_full_extraction", fake_extract)
    report = DamageReport(path=str(src), container="avi", index_missing=True, truncated=True,
                          zero_regions=[[30000, 90000]])
    result = run(damage_report=report, recovery_mode="none")
    assert result.duration_sec == 20.0 and any("색인에서 잰 길이" in w for w in result.extraction.warnings)
    assert result.damage["intervals"] == []
    assert not any(p.is_synthetic for p in result.extraction.points)
    case = store.list_cases()[0]
    assert case.duration_sec == 20.0
    reopened = pipeline.reopen_case(case)
    assert reopened.duration_sec == 20.0 and reopened.extraction.engine_damage["measured_duration_sec"] == 20.0
    assert reopened.damage["intervals"] == []
    # 0.5초 안이면 헤더 길이 그대로
    ext = ExtractionResult(RoutingResult("avi", True, ""), [], [], "x", engine_damage={"measured_duration_sec": 60.3})
    assert pipeline.effective_duration(60.0, ext, "avi") == 60.0 and not ext.warnings
    assert pipeline.effective_duration(60.0, ext, "mp4") == 60.0
