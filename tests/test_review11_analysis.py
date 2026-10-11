"""2026-10-11 코드 리뷰 반영(판정·표·리포트): #4 #20 #22 #44 #52 #76 #102 #113 #116 #122.

합성 TrackPoint 목록만 쓴다 - 영상·키 불필요. recovery_state 계약: gap_carved는 회수한 실측(판정에 포함),
damaged·interpolated는 합성 행(판정·집계에서 제외, 따로 센다).
"""
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

from core import outliers
from core.acceleration import (EVENT_MAX_WINDOW_SEC, _distinct_fix_indices, compute_point_accelerations,
                               compute_point_rate_windows, is_synthetic, speed_rate_pairs)
from core.driving_events import EV_DECEL, EV_STOP, detect_driving_events
from core.format_sniffer import RoutingResult
from core.location_table import (IMPACT_G, gps_slot_rows, has_frame_detail, row_texts, validation_text)
from core.pipeline import PipelineResult
from engine.engine_adapter import ExtractionResult, TrackPoint


def pt(t, speed=30.0, lat=37.5, lon=127.0, state="", utc=True, **kw):
    kw.setdefault("gps_checksum_ok", True)
    kw.setdefault("gps_trusted", True)
    p = TrackPoint(start_time_sec=float(t), latitude=lat, longitude=lon, speed_kmh=speed,
                   gps_status="A", recovery_state=state, **kw)
    if utc:
        p.gps_date = "111026"
        p.gps_utc_time = "%02d%02d%02d" % (3, int(t) // 60, int(t) % 60)
    else:
        p.gps_elapsed = float(t)
        p.gps_abs_time = "2026-10-11 12:%02d:%02d" % (int(t) // 60, int(t) % 60)
    return p


def damaged(t):
    return TrackPoint(start_time_sec=float(t), end_time_sec=t + 1.0, time_source="damage", recovery_state="damaged")


def interpolated(t, lat=37.5, lon=127.0):
    return TrackPoint(start_time_sec=float(t), end_time_sec=t + 1.0, time_source="interpolated",
                      latitude=lat, longitude=lon, speed_kmh=None, recovery_state="interpolated")


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _report(points, events=None, damage=None):
    from report.report_builder import render_report_html
    ex = ExtractionResult(RoutingResult("mp4", True, ""), points, [], "", damage=damage)
    if events is None:
        events = detect_driving_events(points, "car")
    pr = PipelineResult(1, "", "x.mp4", ex, 60.0, events, "0" * 64, "car")
    return _text(render_report_html(pr, "C-1", "tester", ""))


# ---------------------------------------------------------------------------
# #4 gap_carved는 실측 - 속도·위험운전 판정에 넣는다
# ---------------------------------------------------------------------------

def test_4_gap_carved_rows_are_judged():
    speeds = [60] * 15 + [60, 40, 20, 5, 0] + [0] * 5
    pts = [pt(i, v, state="gap_carved") for i, v in enumerate(speeds)]
    assert len(_distinct_fix_indices(pts)) == 25
    assert len(speed_rate_pairs(pts)) == 24
    kinds = [e.kind for e in detect_driving_events(pts, "car")]
    assert EV_DECEL in kinds and EV_STOP in kinds


def test_4_synthetic_rows_are_excluded_and_not_crossed():
    pts = [pt(0, 60), pt(1, 60), damaged(2), damaged(3), pt(4, 10), pt(5, 10), interpolated(6), pt(7, 60)]
    assert [is_synthetic(p) for p in pts] == [False, False, True, True, False, False, True, False]
    assert _distinct_fix_indices(pts) == [0, 1, 4, 5, 7]
    pairs = {(a, b) for a, b, _dt, _r in speed_rate_pairs(pts)}
    assert pairs == {(0, 1), (4, 5)}   # 손상·추정 행을 가로지르는 60→10, 10→60은 판정하지 않는다


def test_4_report_says_no_samples_instead_of_no_events():
    # 판정 표본이 없으면(실측 1개) '위험운전 없음'을 사실처럼 적지 않는다.
    text = _report([pt(0, 60)], events=[])
    assert "판정 불가" in text and "해당 기준에 걸린 위험운전 행동이 없습니다" not in text
    # 표본이 있고 0건이면 '없음'.
    text = _report([pt(0, 30), pt(1, 31), pt(2, 32)], events=[])
    assert "0건 (없음)" in text and "판정 불가" not in text


# ---------------------------------------------------------------------------
# #20 리포트 집계에서 합성 행을 빼고 따로 적는다
# ---------------------------------------------------------------------------

def test_20_report_counts_synthetic_rows_separately():
    real = [pt(t, 20) for t in range(15)] + [pt(t, 20) for t in range(55, 70)]
    pts = real[:15] + [damaged(t) for t in range(15, 35)] + [interpolated(t) for t in range(35, 55)] + real[15:]
    text = _report(pts, events=[])
    assert "추출 지점 70개" in text and "실측 30개" in text
    assert "GPS 수신 30개" in text and "GPS 미기록 0개" in text
    assert "손상 구간 20행" in text and "임의 복구(추정) 20행" in text
    assert re.search(r"정상 30 / 실패 0 / 끊김 0 / 미제공 0", text)


# ---------------------------------------------------------------------------
# #22 이상치 묶음 열쇠는 record_key() + 좌표
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("utc", [True, False])
def test_22_speed_spike_during_stop_is_outlier(utc):
    pts = [pt(t, 150.0 if t == 30 else 0.0, utc=utc) for t in range(40)]
    assert outliers.mark_outliers(pts) == 1 and pts[30].is_outlier
    assert detect_driving_events(pts, "car") == []


@pytest.mark.parametrize("utc", [True, False])
def test_22_position_spike_then_stop_marks_only_the_spike(utc):
    pts = [pt(t, 0.0, lat=37.5 + (0.02 if t == 30 else 0.0), utc=utc) for t in range(40)]
    assert outliers.mark_outliers(pts) == 1
    assert [i for i, p in enumerate(pts) if p.is_outlier] == [30]


# ---------------------------------------------------------------------------
# #44 합성 행은 1초 보기에 늘 들어가고 프레임 단위 판정에서는 빠진다
# ---------------------------------------------------------------------------

def test_44_synthetic_rows_in_slot_view_not_in_frame_detail():
    pts = [pt(t, 20) for t in range(10)] + [damaged(t) for t in range(10, 50)] + [pt(t, 20) for t in range(50, 60)]
    assert gps_slot_rows(pts) == list(range(60))
    assert not has_frame_detail(pts)   # 1Hz 영상에 손상 40행이 끼어도 '프레임 단위'가 아니다
    # 프레임 단위 영상(초당 3행)에 손상 행이 끼어도 판정은 그대로다.
    frame = [pt(i // 3, 20) for i in range(30)] + [damaged(t) for t in range(10, 20)]
    assert has_frame_detail(frame)
    slots = gps_slot_rows(frame)
    assert slots[:3] == [0, 3, 6] and all(i in slots for i in range(30, 40))


# ---------------------------------------------------------------------------
# #52 1.5초 넘는 창은 말풍선 가속도로 보이지 않는다
# ---------------------------------------------------------------------------

def test_52_long_window_rate_is_not_a_per_second_acceleration():
    pts = [pt(0, 47), pt(1, 47), pt(4, 5), pt(5, 5)]   # 1→4초 사이 3초 공백
    assert detect_driving_events(pts, "car") == []
    accels = compute_point_accelerations(pts)
    assert accels[2] is None and accels[3] is not None
    windows = compute_point_rate_windows(pts)
    assert windows[2] is not None and windows[2][1] == 3.0 and windows[2][1] > EVENT_MAX_WINDOW_SEC
    assert windows[3][1] == 1.0


# ---------------------------------------------------------------------------
# #76 차량이 낼 수 없는 변화율은 '검증 필요'로 분리
# ---------------------------------------------------------------------------

def test_76_impossible_rate_is_separated_not_judged():
    pts = [pt(t, v) for t, v in enumerate([30, 30, 80, 30, 30])]
    outliers.mark_outliers(pts)
    assert [p.is_outlier for p in pts] == [False, False, True, False, False]
    assert "검증 필요" in pts[2].outlier_reason
    assert detect_driving_events(pts, "car") == []

    pts = [pt(t, v) for t, v in enumerate([30, 150, 150, 30])]
    outliers.mark_outliers(pts)
    assert [p.is_outlier for p in pts] == [False, True, True, False]
    assert detect_driving_events(pts, "car") == []


def test_76_real_hard_braking_and_crash_are_kept():
    # 기준 안의 급정지(초당 15)는 그대로 판정한다.
    pts = [pt(t, v) for t, v in enumerate([20, 20, 5, 5])]
    assert outliers.mark_outliers(pts) == 0
    assert [e.kind for e in detect_driving_events(pts, "car")] == [EV_STOP]
    # 충돌처럼 속도가 급히 떨어진 뒤 돌아오지 않는 감속은 이상치가 아니다(측정 오류 근거가 없다).
    pts = [pt(t, v) for t, v in enumerate([60, 60, 0, 0, 0])]
    assert outliers.mark_outliers(pts) == 0
    assert [e.kind for e in detect_driving_events(pts, "car")] == [EV_STOP]


# ---------------------------------------------------------------------------
# #102 리포트 임시 HTML은 앱 데이터 폴더 아래, 닫히면 지운다
# ---------------------------------------------------------------------------

def test_102_exporter_temp_file_under_app_dir_and_cleaned_on_close(qapp, tmp_path, monkeypatch):
    from core import appinfo
    from report import report_builder
    monkeypatch.setattr(appinfo, "app_data_dir", lambda: str(tmp_path / "IDAS"))
    stale = tmp_path / "IDAS" / report_builder.REPORT_TMP_DIRNAME / "idas-report-stale.html"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    os.utime(stale, (1, 1))
    exporter = report_builder.ReportExporter("<html>x</html>", str(tmp_path / "out.pdf"), lambda ok, msg: None)
    try:
        assert exporter.temp_path.startswith(str(tmp_path / "IDAS" / report_builder.REPORT_TMP_DIRNAME))
        assert os.path.isfile(exporter.temp_path)
        assert not stale.exists()   # 예전에 남은 임시 파일도 치운다
        exporter.close()
        assert not os.path.exists(exporter.temp_path) and exporter.temp_path == ""
    finally:
        exporter.deleteLater()


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# #113 충격 행 우선 표본 + 최대 충격 요약
# ---------------------------------------------------------------------------

def test_113_impact_rows_are_sampled_and_summarized():
    from report.report_builder import _select_row_indices
    pts = [pt(t, 30, x_g=0.0, y_g=0.0, z_g=1.0) for t in range(600)]
    pts[321].x_g = 3.5   # 합력 3.64 g
    pts[123].y_g = 2.0   # 2.24 g
    chosen = _select_row_indices(pts, [])
    assert len(chosen) == 200 and 321 in chosen and 123 in chosen
    # 프레임 단위 영상에서 G센서 전용 행(1초 보기에 없는 행)의 충격도 표에 싣는다.
    frame = []
    for sec in range(100):
        frame.append(pt(sec, 30))
        frame += [TrackPoint(start_time_sec=sec + 0.1 * k, x_g=0.1, y_g=0.1, z_g=1.0) for k in range(1, 10)]
    frame[555].x_g = 4.0
    assert has_frame_detail(frame) and 555 in _select_row_indices(frame, [])
    text = _report(pts, events=[])
    assert "최대 충격 3.64 g" in text and "321.0초" in text and f"{IMPACT_G:g} g 이상" in text


# ---------------------------------------------------------------------------
# #116 V 끊김 행은 표·리포트 모두 '끊김'
# ---------------------------------------------------------------------------

def test_116_dropout_counted_as_dropout_not_failed_or_unknown():
    pts = [pt(0, 30), pt(1, 30)]
    v = TrackPoint(start_time_sec=2.0, gps_status="V", gps_date="111026", gps_utc_time="030002",
                   gps_checksum_ok=False)
    pts.append(v)
    assert validation_text(v) == "끊김"
    text = _report(pts, events=[])
    assert re.search(r"정상 2 / 실패 0 / 끊김 1 / 미제공 0", text)


# ---------------------------------------------------------------------------
# #122 gap_carved 행도 검증 실패가 먼저
# ---------------------------------------------------------------------------

def test_122_carved_row_failure_shown_before_recovery():
    bad = pt(0, 30, state="gap_carved", gps_trusted=False)
    cells = row_texts(bad, [])
    assert cells[1] == "(검증 실패)" and cells[7] == "실패 (손상 구간 회수)"
    good = pt(1, 30, state="gap_carved")
    assert validation_text(good) == "손상 구간 회수"


# ---------------------------------------------------------------------------
# 조정자 추가 요청(D1·E 연계): #42 #74 #77 #85 #101/#131
# ---------------------------------------------------------------------------

def _report_html(points, damage=None, settings=None, **pr_kw):
    from report.report_builder import render_report_html
    ex = ExtractionResult(RoutingResult("mp4", True, ""), points, [], "", damage=damage)
    pr = PipelineResult(1, "", "x.mp4", ex, 60.0, [], "0" * 64, "car", **pr_kw)
    return render_report_html(pr, "C-1", "tester", "", analysis_settings=settings)


def test_42_repaired_and_reference_hashes_only_when_present():
    pts = [pt(0, 30), pt(1, 30)]
    plain = _text(_report_html(pts))
    assert "이식 사본 SHA-256" not in plain and "참조 영상 SHA-256" not in plain
    damage = {"mode": "none", "summary": [], "interval_lines": [], "repaired_file": "x.repaired.mp4",
              "repaired_sha256": "a" * 64, "reference_path": "/ref/REC_ref.mp4", "reference_sha256": "b" * 64}
    text = _text(_report_html(pts, damage=damage))
    assert "이식 사본 SHA-256 " + "a" * 64 in text and "x.repaired.mp4" in text and "작업 사본" in text
    assert "참조 영상 SHA-256 " + "b" * 64 in text and "REC_ref.mp4" in text


def test_74_time_estimated_note_and_header_without_damage():
    from core.damage_marks import TIME_ESTIMATED_WARNING
    pts = [pt(0, 30), pt(1, 30)]
    assert "시각(초, 추정)" not in _report_html(pts) and TIME_ESTIMATED_WARNING not in _report_html(pts)
    pts[1].time_estimated = True   # 손상 사건이 아니어도 추정 시각이면 안내한다
    html = _report_html(pts)
    assert "<th>시각(초, 추정)</th>" in html and TIME_ESTIMATED_WARNING in _text(html)


def test_77_rows_without_any_time_are_not_judged():
    pts = [pt(0, 30), pt(1, 60)]
    for p in pts:
        p.start_time_sec = None
    text = _text(_report_html(pts))
    assert "판정 불가 (시각 없음" in text and "0건 (없음)" not in text


def test_85_slack_notes():
    pts = [pt(0, 30), pt(1, 30)]
    base = {"mode": "none", "summary": [], "interval_lines": []}
    assert "슬랙 확인은" not in _text(_report_html(pts, damage=dict(base)))
    text = _text(_report_html(pts, damage=dict(base, slack_checked_on="repaired", slack_available=True, slack_carved=False)))
    assert "슬랙 확인은 헤더 이식 사본 기준" in text and "슬랙 있음(추출 안 함)" in text
    text = _text(_report_html(pts, damage=dict(base, slack_checked_on="repaired", slack_available=True, slack_carved=True)))
    assert "슬랙 있음(추출 안 함)" not in text


def test_101_131_rear_damage_and_pair_check_lines():
    pts = [pt(0, 30), pt(1, 30)]
    assert "후방 영상 손상" not in _text(_report_html(pts, settings={}))
    settings = {"rear_damage": {"damaged": True, "mode": "none", "summary": ["idx1 없음"]},
                "pair_check": {"status": "damaged", "note": "손상된 영상이라 전·후방 대조를 하지 못함",
                               "damaged_files": {"front": ["F.avi"]}}}
    text = _text(_report_html(pts, settings=settings, rear_copy_path="/c/R.avi"))
    assert "후방 영상 손상 복구 안 함" in text and "idx1 없음" in text
    assert "전·후방 대조 대조 불가(손상)" in text and "front: F.avi" in text
