from __future__ import annotations

import base64
import glob
import html
import os
import tempfile
import time
from typing import List, Optional

from PySide6.QtCore import QCoreApplication, QMarginsF, QObject, QUrl
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtWebEngineWidgets import QWebEngineView

from core import appinfo, gpstime
from core.acceleration import _distinct_fix_indices, is_synthetic, judged_pair_count
from core.damage_marks import TIME_ESTIMATED_WARNING
from core.driving_events import DrivingEvent, criteria_lines, events_by_row, summarize_counts, vehicle_label
from core.location_table import (COL_EVENT, COL_G, COL_LAT, COL_LON, COL_SPEED, COL_TIME, IMPACT_G, gps_slot_rows,
                                 has_frame_detail, row_texts)
from core.pipeline import PipelineResult
from engine.engine_adapter import TrackPoint

MAX_TABLE_ROWS = 200
# 리포트 임시 HTML을 두는 앱 데이터 폴더 아래 하위 폴더(리뷰 #102). 사건번호·좌표가 든 파일이라 %TEMP%에 두지 않는다.
REPORT_TMP_DIRNAME = "report-tmp"
# 이보다 오래된 임시 HTML은 지난 실행이 비정상 종료하며 남긴 것이다 - 새 리포트를 만들 때 치운다.
STALE_TMP_SEC = 3600


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "알 수 없음"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _impact_rows(records: List[TrackPoint]) -> List[int]:
    """충격(합력 IMPACT_G 이상) 행 번호 - 센 순. 합성 행은 G값이 없다."""
    hits = [(p.g_magnitude, i) for i, p in enumerate(records)
            if p.g_magnitude is not None and p.g_magnitude >= IMPACT_G]
    return [i for _g, i in sorted(hits, key=lambda h: (-h[0], h[1]))]


def _select_row_indices(records: List[TrackPoint], events: List[DrivingEvent],
                          max_rows: int = MAX_TABLE_ROWS) -> List[int]:
    if max_rows <= 0 or not records:
        return []
    # 프레임·G센서 단위로 행을 쓰는 영상은 화면 표의 기본과 같이 1초(GPS 기록)마다 한 행만 싣는다.
    # 충격 행은 1초 보기에 없는 G센서 전용 행이라도 싣는다(리뷰 #47·#113).
    impacts = _impact_rows(records)
    pool = sorted(set(gps_slot_rows(records)) | set(impacts)) if has_frame_detail(records) else list(range(len(records)))
    if len(pool) <= max_rows:
        return pool
    def sample(values, count):
        if count <= 0:
            return []
        if count == 1:
            return values[:1]
        return [values[round(i * (len(values) - 1) / (count - 1))] for i in range(count)]
    pool_set = set(pool)
    event_rows = {i for ev in events for i in range(max(0, ev.start_index), min(len(records), ev.end_index + 1))}
    # 우선 대상: 위험운전 행과 충격 행. 둘이 표를 넘치면 센 충격부터 자리를 잡고 나머지를 균등 표본한다.
    flagged = sorted((event_rows | set(impacts)) & pool_set)
    if len(flagged) >= max_rows:
        keep = impacts[:max(1, max_rows // 4)]
        rest = [i for i in flagged if i not in set(keep)]
        return sorted(set(keep) | set(sample(rest, max_rows - len(keep))))
    flagged_set = set(flagged)
    others = [i for i in pool if i not in flagged_set]
    return sorted(flagged + sample(others, max_rows - len(flagged)))


def _impact_summary(records: List[TrackPoint]) -> str:
    """'최대 충격 3.65 g (321.0초, 행 #322) · 2 g 이상 3행' - G센서 값이 없으면 '-'."""
    best = None
    for i, p in enumerate(records):
        g = p.g_magnitude
        if g is not None and (best is None or g > best[0]):
            best = (g, i)
    if best is None:
        return "- (G센서 기록 없음)"
    g, i = best
    t = records[i].start_time_sec
    when = f"{t:.1f}초, " if t is not None else ""
    return f"최대 충격 {g:.2f} g ({when}행 #{i + 1}) · {IMPACT_G:g} g 이상 {len(_impact_rows(records))}행"


def _image_section(title: str, png_bytes: Optional[bytes], caption: str) -> str:
    if not png_bytes:
        return ""
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return (f"  <h2>{_esc(title)}</h2>\n"
            f'  <figure><img src="data:image/png;base64,{encoded}">'
            f"<figcaption>{_esc(caption)}</figcaption></figure>\n")


def render_report_html(pipeline_result: PipelineResult, case_number: str, examiner: str,
                        memo: str, chart_png: Optional[bytes] = None,
                        map_png: Optional[bytes] = None,
                        analysis_settings: Optional[dict] = None) -> str:
    """analysis_settings는 사건 설정(case.analysis_settings) - 후방 손상·전후방 대조 기록이 든다(리뷰 #101·#131)."""
    records = pipeline_result.extraction.points
    settings = analysis_settings if isinstance(analysis_settings, dict) else {}
    events = pipeline_result.driving_events
    row_indices = _select_row_indices(records, events)
    row_events = events_by_row(events, len(records))

    sequence = pipeline_result.is_sequence
    seg_labels = [seg.label for seg in pipeline_result.segments]
    rows_html = []
    for idx in row_indices:
        rec = records[idx]
        here = row_events[idx]
        row_class = ("outlier" if rec.is_outlier else "damaged" if rec.recovery_state == "damaged"
                     else "interpolated" if rec.recovery_state == "interpolated" else "")
        # 위험운전 행은 종류 색을 옅게 깐다(화면 Location 표와 같은 색). 칸 글자도 화면 표와 같다.
        row_style = f' style="background: {here[0].color}33"' if here else ""
        cells = row_texts(rec, here)
        seg_cell = ""
        if sequence:
            seg_label = seg_labels[rec.segment_index] if 0 <= rec.segment_index < len(seg_labels) else ""
            seg_cell = f"<td>{_esc(seg_label)}</td>"
        rows_html.append(
            f'<tr class="{row_class}"{row_style}>'
            f"<td>{idx + 1}</td>{seg_cell}"
            + "".join(f"<td>{_esc(cells[c])}</td>"
                      for c in (COL_TIME, COL_LAT, COL_LON, COL_SPEED, COL_EVENT, COL_G))
            + "</tr>"
        )

    body_rows = "".join(rows_html) if rows_html else f'<tr><td colspan="{8 if sequence else 7}">추출된 좌표가 없습니다.</td></tr>'
    extraction = pipeline_result.extraction
    routing = extraction.routing
    video_filename = os.path.basename(pipeline_result.source_copy_path or "")

    # 그래프와 지도는 캡처 시점의 정적 이미지로 넣는다. 리포트 안에서 지도를 다시
    # 살려 그리면 타일 로딩 타이밍에 따라 결과가 달라져, 증거 문서로 쓰기 어렵다.
    visuals_html = (
        _image_section("속도 분석", chart_png,
                        "속도 선의 색이 바뀐 곳은 급가속(빨강)·급출발(분홍)·급감속(주황)·급정지(보라) 구간입니다. "
                        + ("세로 점선은 이어 붙인 영상의 경계입니다. " if pipeline_result.is_sequence else "") +
                        "점은 실제 GPS 측정값이고 선은 점을 지나는 보간선입니다.")
        + _image_section("이동 경로", map_png,
                          "초록 실선은 주행 경로, 색 선과 이름표는 위험운전 행동(범례 참고), "
                          "회색 점선은 GPS 수신이 끊긴 구간입니다. 분석 완료 시점의 전체 경로입니다.")
    )
    warning_html = "".join(f"<li>{_esc(w)}</li>" for w in extraction.warnings)
    # 집계는 실측 행만 센다. 손상 처리로 끼운 합성 행(빈 행·임의 복구 추정 좌표)은 측정이 아니라 따로 적는다 -
    # 섞으면 손상 구간이 'GPS 미기록'·'미제공'으로 집계됐다(리뷰 #89, 2026-10-11 #20).
    real = [p for p in records if not is_synthetic(p)]
    damaged_count = sum(1 for p in records if p.recovery_state == "damaged")
    interpolated_count = sum(1 for p in records if p.recovery_state == "interpolated")
    fix_count = sum(1 for p in real if p.has_fix)
    outlier_count = sum(1 for p in real if p.is_outlier)
    # 끊김(측위 실패 V) 판정이 검증 실패보다 먼저다 - 표(location_table.validation_text)·Tracker와 같은 순서(리뷰 #116).
    dropout_rows = [p for p in real if p.is_dropout and not p.has_coords]
    dropout_count = len(dropout_rows)
    checked = [p for p in real if not (p.is_dropout and not p.has_coords)]
    failed_checks = sum(p.gps_checksum_ok is False or p.gps_trusted is False for p in checked)
    # 미기록은 GPS 기록이 전혀 없는 행만 직접 센다. 예전엔 전체-수신-끊김으로 계산해 이상치·검증 실패가
    # 미기록에 섞여 이중으로 집계됐다(리뷰 #89).
    unrecorded_count = sum(1 for p in real if not p.has_gps_record and not p.has_coords)
    ok_checks = sum(p.gps_checksum_ok is True and p.gps_trusted is not False for p in checked)
    unknown_checks = len(checked) - failed_checks - ok_checks
    synthetic_note = (f"; 손상 구간 {damaged_count}행 / 임의 복구(추정) {interpolated_count}행 - 집계 제외"
                      if damaged_count or interpolated_count else "")
    speeds = [records[i].speed_kmh for i in _distinct_fix_indices(records)]
    speed_summary = f"평균 {sum(speeds)/len(speeds):.1f} / 최고 {max(speeds):.1f} km/h" if speeds else "-"
    # 판정 표본(1초 간격 속도 변화)이 하나도 없으면 '0건(없음)'이 아니라 판정 불가다(리뷰 2026-10-11 #4).
    judged_pairs = judged_pair_count(records)
    no_times = bool(records) and all(p.start_time_sec is None for p in records)
    if judged_pairs:
        events_summary = f"{len(events)}건 ({summarize_counts(events)})"
        no_event_text = "해당 기준에 걸린 위험운전 행동이 없습니다."
    elif no_times:
        # 시간축이 없는 영상(hdrl 손상 AVI 등)은 속도 변화율을 잴 수 없다(리뷰 #77).
        events_summary = "판정 불가 (시각 없음 - 시간축이 없어 속도 변화율을 잴 수 없음)"
        no_event_text = "판정 불가 - 기록에 시각이 없어 위험운전 여부를 판정하지 않았습니다."
    else:
        events_summary = "판정 불가 (1초 간격 속도 표본이 없어 판정하지 않음)"
        no_event_text = "판정 불가 - 1초 간격의 유효 속도 표본이 없어 위험운전 여부를 판정하지 않았습니다."
    impact_summary = _impact_summary(records)
    avi_repair_html = (f'<div class="kv"><b>AVI 복구</b>{"적용" if extraction.avi_repaired else "없음"}</div>'
                       if (routing.container or "").lower() == "avi" else "")
    damage = pipeline_result.damage or {}
    damage_html = ""
    if damage:
        from core.damage_marks import MODE_LABELS
        mode = MODE_LABELS.get(str(damage.get("mode")), str(damage.get("mode")))
        items = "".join(f"<li>{_esc(line)}</li>" for line in (damage.get("summary") or []))
        items += "".join(f"<li>{_esc(line)}</li>" for line in (damage.get("interval_lines") or []))
        if damage.get("repair_note"):
            items += f"<li>{_esc(damage['repair_note'])}</li>"
        damage_html = (f'<div class="kv"><b>손상 파일</b>{_esc(mode)} - 손상된 파일을 분석했습니다. '
                       f'임의 복구 좌표는 추정값이며 측정값이 아닙니다.</div>\n<ul class="criteria">{items}</ul>')
    # 재생 시각 추정은 손상 사건이 아니어도 생긴다(경과 초만 있는 기기 등) - extraction의 판정을 따른다(리뷰 #74).
    time_estimated = bool(extraction.time_estimated)
    time_header = "시각(초, 추정)" if time_estimated else "시각(초)"
    time_note_html = (f'<div class="kv"><b>재생 시각</b>{_esc(TIME_ESTIMATED_WARNING)}</div>'
                      if time_estimated else "")
    # 슬랙 항목: 이식 사본 기준으로 확인했으면 그렇게 적고, 있는데 추출하지 않았으면 그대로 적는다(리뷰 #85).
    slack_note = ""
    if damage.get("slack_checked_on") == "repaired":
        slack_note += " · 슬랙 확인은 헤더 이식 사본 기준"
    if damage.get("slack_available") and not damage.get("slack_carved"):
        slack_note += " · 슬랙 있음(추출 안 함)"
    # 후방 손상·전후방 대조 기록(ui/main_window가 사건 설정에 남긴다, 리뷰 #101·#131).
    rear_damage = settings.get("rear_damage")
    rear_damage_html = ""
    if isinstance(rear_damage, dict) and rear_damage.get("damaged"):
        from core.damage_marks import MODE_LABELS
        rear_mode = MODE_LABELS.get(str(rear_damage.get("mode")), str(rear_damage.get("mode") or ""))
        rear_lines = "".join(f"<li>{_esc(line)}</li>" for line in (rear_damage.get("summary") or []))
        rear_damage_html = (f'<div class="kv"><b>후방 영상 손상</b>복구 안 함({_esc(rear_mode)}) - 후방은 원본을 그대로 '
                            f'재생하며 GPS 분석에 쓰지 않습니다.</div>\n<ul class="criteria">{rear_lines}</ul>')
    pair_check = settings.get("pair_check")
    pair_check_html = ""
    if isinstance(pair_check, dict) and pair_check.get("status") == "damaged":
        files = pair_check.get("damaged_files") or {}
        names = "; ".join(f"{k}: {', '.join(str(x) for x in v)}" for k, v in files.items() if v) if isinstance(files, dict) else ""
        pair_check_html = (f'<div class="kv"><b>전·후방 대조</b>대조 불가(손상) - {_esc(pair_check.get("note") or "")}'
                           + (f" ({_esc(names)})" if names else "") + "</div>")
    # 일시는 한국 시간(UTC+9)으로 적는다. 분석 일시는 사건을 만든(엔진으로 추출한) 시각이다.
    generated = gpstime.now_display()
    analyzed = gpstime.format_local_iso(pipeline_result.analyzed_at) if pipeline_result.analyzed_at else "-"
    if sequence:
        file_rows = []
        for seg in pipeline_result.segments:
            for role, path, sha in (("전방", seg.front_copy_path, seg.front_sha256),
                                    ("후방", seg.rear_copy_path, seg.rear_sha256)):
                if path:
                    file_rows.append(
                        f"<tr><td>{_esc(seg.label)}</td><td>{role}</td><td>{_esc(os.path.basename(path))}</td>"
                        f"<td>{_esc(_fmt_duration(seg.duration_sec))}</td><td>{_esc(sha)}</td></tr>")
        files_html = (
            f'<div class="kv"><b>원본 파일</b>연속 영상 이어보기 {len(pipeline_result.segments)}개 '
            "(파일마다 원본·사본 SHA-256 대조, 이어 붙인 영상 파일은 만들지 않음)</div>\n"
            '  <table class="files"><thead><tr><th>영상</th><th>구분</th><th>파일</th><th>길이</th>'
            "<th>SHA-256</th></tr></thead><tbody>" + "".join(file_rows) + "</tbody></table>")
    else:
        files_html = (f'<div class="kv"><b>원본 파일</b>{_esc(video_filename)}</div>\n'
                      f'  <div class="kv"><b>SHA-256</b>{_esc(pipeline_result.sha256)}</div>')
        if pipeline_result.rear_copy_path:
            files_html += (
                f'\n  <div class="kv"><b>후방 파일</b>{_esc(os.path.basename(pipeline_result.rear_copy_path))}</div>'
                f'\n  <div class="kv"><b>후방 SHA-256</b>{_esc(pipeline_result.rear_sha256 or "-")}</div>')
    # 헤더 이식 사본(재생·분석에 쓴 파일)과 참조 영상의 해시 - 있을 때만(리뷰 #42). 원본 해시와 구분해 적는다.
    repaired_sha = str(damage.get("repaired_sha256") or pipeline_result.repaired_sha256 or "")
    if repaired_sha:
        repaired_name = str(damage.get("repaired_file") or os.path.basename(pipeline_result.repaired_path or "") or "")
        files_html += (f'\n  <div class="kv"><b>이식 사본 SHA-256</b>{_esc(repaired_sha)}'
                       + (f" ({_esc(repaired_name)}, 헤더 이식 직후 측정 - 원본이 아닌 작업 사본)" if repaired_name
                          else " (헤더 이식 직후 측정 - 원본이 아닌 작업 사본)") + "</div>")
    reference_sha = str(damage.get("reference_sha256") or "")
    if reference_sha:
        ref_name = os.path.basename(str(damage.get("reference_path") or ""))
        files_html += (f'\n  <div class="kv"><b>참조 영상 SHA-256</b>{_esc(reference_sha)}'
                       + (f" ({_esc(ref_name)})" if ref_name else "") + "</div>")
    vehicle_type = pipeline_result.vehicle_type
    criteria_html = "".join(f"<li>{_esc(line)}</li>" for line in criteria_lines(vehicle_type)[1:])
    seg_starts = []
    base = 0
    for seg in pipeline_result.segments:
        seg_starts.append((base, seg))
        base += len(seg.points)

    def _event_where(ev) -> str:
        """이어보기면 '영상 N (영상 기준 시각)'까지 적는다(리뷰 #90)."""
        if not sequence:
            return ""
        seg = next((s for b, s in reversed(seg_starts) if ev.start_index >= b), None)
        if seg is None:
            return "<td>-</td>"
        return (f"<td>{_esc(seg.label)} "
                f"{_esc(f'{ev.start_time_sec - seg.offset_sec:.1f} ~ {ev.end_time_sec - seg.offset_sec:.1f}')}</td>")
    event_rows = "".join(
        f'<tr style="background: {ev.color}33"><td>{_esc(ev.label)}</td>'
        f"<td>{_esc(f'{ev.start_time_sec:.1f} ~ {ev.end_time_sec:.1f}')}</td>{_event_where(ev)}"
        f"<td>{_esc(ev.detail)}</td></tr>" for ev in events
    ) or f'<tr><td colspan="{4 if sequence else 3}">{_esc(no_event_text)}</td></tr>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  /* 종이 여백은 ReportExporter가 printToPdf에 주는 QPageLayout(A4, 좌우 20mm·상하 18mm)이
     정한다. CSS @page 여백은 QtWebEngine이 인자로 받은 레이아웃에 눌려 반영되지 않았고,
     기본 레이아웃은 여백 0이라 내용이 종이 왼쪽 끝에 붙어 나왔다. */
  body {{ font-family: -apple-system, "Malgun Gothic", sans-serif; color: #111; margin: 0; }}
  h2 {{ break-after: avoid; page-break-after: avoid; }}
  figure {{ margin: 0 0 14px; break-inside: avoid; page-break-inside: avoid; }}
  figure img {{ width: 100%; border: 1px solid #ccc; }}
  figcaption {{ font-size: 11px; color: #666; padding-top: 4px; }}
  thead {{ display: table-header-group; }}
  tr {{ break-inside: avoid; page-break-inside: avoid; }}
  h1 {{ font-size: 18px; border-bottom: 2px solid #111; padding-bottom: 6px; }}
  h2 {{ font-size: 14px; margin-top: 24px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
  th, td {{ border: 1px solid #ccc; padding: 4px 6px; text-align: left; }}
  th {{ background: #f2f2f2; }}
  table.files {{ margin: 4px 0 6px; }}
  table.files td {{ word-break: break-all; }}
  .memo {{ white-space: pre-wrap; display: inline-block; max-width: calc(100% - 130px); vertical-align: top; }}
  ul.criteria {{ font-size: 11px; color: #333; margin: 4px 0 6px; padding-left: 18px; }}
  tr.outlier td {{ color: #b36b00; }}
  tr.gap td {{ text-align: center; color: #999; border: none; }}
  tr.damaged td {{ color: #c92a2a; }}
  tr.interpolated td {{ color: #9c36b5; }}
  /* 긴 경로·해시가 종이 폭을 넘지 않게 아무 데서나 줄을 바꾼다(예전엔 경로가 잘려 나갔다). */
  .kv, p, td {{ overflow-wrap: anywhere; word-break: break-all; }}
  .kv {{ font-size: 12px; margin: 2px 0; }}
  .kv b {{ display: inline-block; width: 120px; vertical-align: top; }}
</style></head><body>
  <h1>Extraction Report</h1>
  <h2>기본 정보</h2>
  <div class="kv"><b>사건번호</b>{_esc(case_number)}</div>
  <div class="kv"><b>담당자</b>{_esc(examiner)}</div>
  <div class="kv"><b>메모</b><span class="memo">{_esc(memo)}</span></div>
  {files_html}
  <div class="kv"><b>재생시간</b>{_esc(_fmt_duration(pipeline_result.duration_sec))}</div>
  <div class="kv"><b>탐지 컨테이너</b>{_esc(routing.container.upper())}</div>
  <div class="kv"><b>시간축 근거</b>{_esc(extraction.time_source or "-")}</div>
  <div class="kv"><b>추출 지점</b>{_esc(len(records))}개 (실측 {_esc(len(real))}개: GPS 수신 {_esc(fix_count)}개 /
      수신 끊김 {_esc(dropout_count)}개 / GPS 미기록 {_esc(unrecorded_count)}개 / 이상치 {_esc(outlier_count)}개 /
      검증 실패 {_esc(failed_checks)}개{_esc(synthetic_note)})</div>
  <div class="kv"><b>차종 기준</b>{_esc(vehicle_label(vehicle_type))} (국토교통부 DTG 위험운전행동 판별 기준, 2022)</div>
  <div class="kv"><b>위험운전 행동</b>{_esc(events_summary)}</div>
  <div class="kv"><b>이상치 제외</b>{_esc(outlier_count)}개 지점 (좌표 급변·비정상 속도·검증 필요, 원본 CSV에는 보존)</div>
  <div class="kv"><b>충격(G센서)</b>{_esc(impact_summary)}</div>

  <div class="kv"><b>분석 상태</b>{_esc(extraction.status)} — {_esc(extraction.status_detail)}</div>
  <div class="kv"><b>산출물 검증</b>{_esc({True: "엔진 산출물 manifest 일치", False: "불일치 - 산출물이 분석 때와 다름(변조·손상 의심)", None: "manifest 없음(새 분석 직후 또는 옛 사건)"}[pipeline_result.artifacts_verified])}</div>
  {avi_repair_html}
  {damage_html}
  {time_note_html}
  {rear_damage_html}
  {pair_check_html}
  <div class="kv"><b>GPS 검증(행)</b>정상 {ok_checks} / 실패 {failed_checks} / 끊김 {dropout_count} / 미제공 {unknown_checks}{(" (합성 행 " + str(damaged_count + interpolated_count) + "행 제외)") if damaged_count or interpolated_count else ""}</div>
  <p>검증 실패·이상치·비유한 수치는 지도·속도 계산에서 제외합니다. 끊김은 측위 실패(V) 기록, 미제공은 검증 성공을 뜻하지 않습니다.</p>
  <div class="kv"><b>속도 통계</b>{speed_summary} (유효 GPS 측정 산술평균, 반복 기록 제외)</div>
  <div class="kv"><b>슬랙 별도 좌표</b>{len(extraction.slack_points)}개 (현재 영상 궤적에 합치지 않음{_esc(slack_note)})</div>
  <div class="kv"><b>분석(추출) 일시</b>{_esc(analyzed)}</div>
  <div class="kv"><b>보고서 생성 일시</b>{_esc(generated)}</div>
  <h2>분석 경고 ({len(extraction.warnings)}건)</h2><ul>{warning_html}</ul>
  <h2>위험운전 행동 ({_esc(vehicle_label(vehicle_type))} 기준, {_esc(f"{len(events)}건" if judged_pairs else "판정 불가")})</h2>
  <ul class="criteria">{criteria_html}</ul>
  <p>속도 변화로 정해지는 네 유형(급가속·급출발·급감속·급정지)만 판정합니다. 방향 계열(급진로변경·급앞지르기·급좌우회전·급U턴)과
  과속·장기과속(도로 제한속도 필요)은 판정하지 않습니다. 승용차는 택시 기준을 적용합니다.</p>
  <table>
    <thead><tr><th>종류</th><th>{"누적 시각(초)" if sequence else "영상 시각(초)"}</th>{"<th>영상 (영상 기준 초)</th>" if sequence else ""}<th>판정 근거</th></tr></thead>
    <tbody>{event_rows}</tbody>
  </table>
  {visuals_html}
  <h2>추출 목록 (전체 {len(records)}개 지점 중 {len(row_indices)}개 표시 - 위험운전 구간·충격 행 우선, 나머지 전체 구간 균등 표본)</h2>
  <p>전체 원자료 CSV: {_esc(extraction.primary_source_file or "없음")}</p>
  <table>
    <thead><tr><th>#</th>{"<th>영상</th>" if sequence else ""}<th>{time_header}</th><th>위도</th><th>경도</th><th>속도(km/h)</th><th>위험운전</th><th>충격(g)</th></tr></thead>
    <tbody>{body_rows}</tbody>
  </table>
</body></html>
"""


def report_tmp_dir() -> str:
    """리포트 임시 HTML 폴더(앱 데이터 폴더 아래). 만들지 못하면 시스템 임시 폴더."""
    try:
        path = os.path.join(appinfo.app_data_dir(), REPORT_TMP_DIRNAME)
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return tempfile.gettempdir()


def sweep_stale_report_files(folder: str, max_age_sec: float = STALE_TMP_SEC) -> int:
    """지난 실행이 비정상 종료하며 남긴 idas-report-*.html을 지운다. 지운 수를 돌려준다."""
    removed = 0
    now = time.time()
    for path in glob.glob(os.path.join(folder, "idas-report-*.html")):
        try:
            if now - os.path.getmtime(path) >= max_age_sec:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


class ReportExporter(QObject):
    """리포트 HTML을 QtWebEngine으로 PDF로 만든다. 임시 HTML(사건번호·좌표가 든다)은 앱 데이터 폴더 아래에
    두고, PDF가 끝나거나 close()·앱 종료·객체 소멸 때 지운다(리뷰 #22·#102). 창을 닫는 쪽(ui/main_window)은
    closeEvent에서 close()를 부른다."""

    def __init__(self, html_str: str, out_path: str, on_done, parent=None):
        super().__init__(parent)
        self._out_path = out_path
        self._on_done = on_done
        self._view = QWebEngineView()
        self._view.loadFinished.connect(self._on_load_finished)
        self._view.page().pdfPrintingFinished.connect(self._on_pdf_finished)
        # setHtml은 data: URL 2MiB 한도가 있어 고해상도 지도 PNG가 든 리포트는 로드가 실패했다
        # (리뷰 #22). 임시 파일에 써서 file:// 로 연다. 파일은 PDF가 끝나면 지운다.
        self._tmp_path = ""
        self._closed = False
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)
        try:
            folder = report_tmp_dir()
            sweep_stale_report_files(folder)
            fd, self._tmp_path = tempfile.mkstemp(prefix="idas-report-", suffix=".html", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(html_str)
            self._view.load(QUrl.fromLocalFile(self._tmp_path))
        except OSError:
            self._cleanup()
            self._view.setHtml(html_str, QUrl("about:blank"))

    @property
    def temp_path(self) -> str:
        return self._tmp_path

    def _cleanup(self) -> None:
        if self._tmp_path:
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass
            self._tmp_path = ""

    def close(self) -> None:
        """임시 HTML을 지우고 더 이상 콜백을 부르지 않는다. 창을 닫거나 앱이 끝날 때 부른다(리뷰 #102)."""
        self._closed = True
        self._cleanup()

    def __del__(self):
        try:
            self._cleanup()
        except Exception:
            pass

    # A4에 글 쓸 때처럼 양쪽에 여백을 둔다. printToPdf의 기본 레이아웃은 여백 0이다.
    PAGE_LAYOUT = QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                              QMarginsF(20, 18, 20, 18), QPageLayout.Millimeter)

    def _on_load_finished(self, ok: bool) -> None:
        if self._closed:
            return
        if not ok:
            self._cleanup()
            self._on_done(False, "리포트 HTML 로드 실패")
            return
        self._view.page().printToPdf(self._out_path, self.PAGE_LAYOUT)

    def _on_pdf_finished(self, file_path: str, success: bool) -> None:
        self._cleanup()
        if self._closed:
            return
        self._on_done(success, "" if success else "PDF 저장 실패")
