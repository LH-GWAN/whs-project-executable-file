from __future__ import annotations

import base64
import html
import os
from core.acceleration import _distinct_fix_indices
from typing import List, Optional

from PySide6.QtCore import QMarginsF, QObject, QUrl
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtWebEngineWidgets import QWebEngineView

from core import gpstime
from core.driving_events import DrivingEvent, criteria_lines, events_by_row, summarize_counts, vehicle_label
from core.location_table import (COL_EVENT, COL_G, COL_LAT, COL_LON, COL_SPEED, COL_TIME, gps_slot_rows,
                                 has_frame_detail, row_texts)
from core.pipeline import PipelineResult
from engine.engine_adapter import TrackPoint

MAX_TABLE_ROWS = 200


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "알 수 없음"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _select_row_indices(records: List[TrackPoint], events: List[DrivingEvent],
                          max_rows: int = MAX_TABLE_ROWS) -> List[int]:
    if max_rows <= 0 or not records:
        return []
    # 프레임·G센서 단위로 행을 쓰는 영상은 화면 표의 기본과 같이 1초(GPS 기록)마다 한 행만 싣는다.
    pool = gps_slot_rows(records) if has_frame_detail(records) else list(range(len(records)))
    if len(pool) <= max_rows:
        return pool
    def sample(values, count):
        if count <= 0:
            return []
        if count == 1:
            return values[:1]
        return [values[round(i * (len(values) - 1) / (count - 1))] for i in range(count)]
    pool_set = set(pool)
    flagged = sorted({i for ev in events for i in range(max(0, ev.start_index),
                       min(len(records), ev.end_index + 1))} & pool_set)
    if len(flagged) >= max_rows:
        return sample(flagged, max_rows)
    flagged_set = set(flagged)
    others = [i for i in pool if i not in flagged_set]
    return sorted(flagged + sample(others, max_rows - len(flagged)))


def _image_section(title: str, png_bytes: Optional[bytes], caption: str) -> str:
    if not png_bytes:
        return ""
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return (f"  <h2>{_esc(title)}</h2>\n"
            f'  <figure><img src="data:image/png;base64,{encoded}">'
            f"<figcaption>{_esc(caption)}</figcaption></figure>\n")


def render_report_html(pipeline_result: PipelineResult, case_number: str, examiner: str,
                        memo: str, chart_png: Optional[bytes] = None,
                        map_png: Optional[bytes] = None) -> str:
    records = pipeline_result.extraction.points
    events = pipeline_result.driving_events
    row_indices = _select_row_indices(records, events)
    row_events = events_by_row(events, len(records))

    sequence = pipeline_result.is_sequence
    seg_labels = [seg.label for seg in pipeline_result.segments]
    rows_html = []
    for idx in row_indices:
        rec = records[idx]
        here = row_events[idx]
        row_class = "outlier" if rec.is_outlier else ""
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

    body_rows = "".join(rows_html) if rows_html else '<tr><td colspan="{8 if sequence else 7}">추출된 좌표가 없습니다.</td></tr>'
    extraction = pipeline_result.extraction
    routing = extraction.routing
    video_filename = os.path.basename(pipeline_result.source_copy_path or "")
    fix_count = extraction.fix_count
    dropout_count = extraction.dropout_count

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
    outlier_count = extraction.outlier_count
    warning_html = "".join(f"<li>{_esc(w)}</li>" for w in extraction.warnings)
    failed_checks = sum(p.gps_checksum_ok is False or p.gps_trusted is False for p in records)
    ok_checks = sum(p.gps_checksum_ok is True and p.gps_trusted is not False for p in records)
    unknown_checks = len(records) - failed_checks - ok_checks
    speeds = [records[i].speed_kmh for i in _distinct_fix_indices(records)]
    speed_summary = f"평균 {sum(speeds)/len(speeds):.1f} / 최고 {max(speeds):.1f} km/h" if speeds else "-"
    avi_repair_html = (f'<div class="kv"><b>AVI 복구</b>{"적용" if extraction.avi_repaired else "없음"}</div>'
                       if (routing.container or "").lower() == "avi" else "")
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
    vehicle_type = pipeline_result.vehicle_type
    criteria_html = "".join(f"<li>{_esc(line)}</li>" for line in criteria_lines(vehicle_type)[1:])
    event_rows = "".join(
        f'<tr style="background: {ev.color}33"><td>{_esc(ev.label)}</td>'
        f"<td>{_esc(f'{ev.start_time_sec:.1f} ~ {ev.end_time_sec:.1f}')}</td>"
        f"<td>{_esc(ev.detail)}</td></tr>" for ev in events
    ) or '<tr><td colspan="3">해당 기준에 걸린 위험운전 행동이 없습니다.</td></tr>'

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
  ul.criteria {{ font-size: 11px; color: #333; margin: 4px 0 6px; padding-left: 18px; }}
  tr.outlier td {{ color: #b36b00; }}
  tr.gap td {{ text-align: center; color: #999; border: none; }}
  /* 긴 경로·해시가 종이 폭을 넘지 않게 아무 데서나 줄을 바꾼다(예전엔 경로가 잘려 나갔다). */
  .kv, p, td {{ overflow-wrap: anywhere; word-break: break-all; }}
  .kv {{ font-size: 12px; margin: 2px 0; }}
  .kv b {{ display: inline-block; width: 120px; vertical-align: top; }}
</style></head><body>
  <h1>Extraction Report</h1>
  <h2>기본 정보</h2>
  <div class="kv"><b>사건번호</b>{_esc(case_number)}</div>
  <div class="kv"><b>담당자</b>{_esc(examiner)}</div>
  <div class="kv"><b>메모</b>{_esc(memo)}</div>
  {files_html}
  <div class="kv"><b>재생시간</b>{_esc(_fmt_duration(pipeline_result.duration_sec))}</div>
  <div class="kv"><b>탐지 컨테이너</b>{_esc(routing.container.upper())}</div>
  <div class="kv"><b>시간축 근거</b>{_esc(extraction.time_source or "-")}</div>
  <div class="kv"><b>추출 지점</b>{_esc(len(records))}개 (GPS 수신 {_esc(fix_count)}개 /
      수신 끊김 {_esc(dropout_count)}개 / GPS 미기록 {_esc(len(records) - fix_count - dropout_count)}개)</div>
  <div class="kv"><b>차종 기준</b>{_esc(vehicle_label(vehicle_type))} (국토교통부 DTG 위험운전행동 판별 기준, 2022)</div>
  <div class="kv"><b>위험운전 행동</b>{_esc(len(events))}건 ({_esc(summarize_counts(events))})</div>
  <div class="kv"><b>이상치 제외</b>{_esc(outlier_count)}개 지점 (좌표 급변·비정상 속도, 원본 CSV에는 보존)</div>

  <div class="kv"><b>분석 상태</b>{_esc(extraction.status)} — {_esc(extraction.status_detail)}</div>
  {avi_repair_html}
  <div class="kv"><b>GPS 검증(행)</b>정상 {ok_checks} / 실패 {failed_checks} / 미제공 {unknown_checks}</div>
  <p>검증 실패·이상치·비유한 수치는 지도·속도 계산에서 제외합니다. 미제공은 검증 성공을 뜻하지 않습니다.</p>
  <div class="kv"><b>속도 통계</b>{speed_summary} (유효 GPS 측정 산술평균, 반복 기록 제외)</div>
  <div class="kv"><b>슬랙 별도 좌표</b>{len(extraction.slack_points)}개 (현재 영상 궤적에 합치지 않음)</div>
  <div class="kv"><b>분석(추출) 일시</b>{_esc(analyzed)}</div>
  <div class="kv"><b>보고서 생성 일시</b>{_esc(generated)}</div>
  <h2>분석 경고 ({len(extraction.warnings)}건)</h2><ul>{warning_html}</ul>
  <h2>위험운전 행동 ({_esc(vehicle_label(vehicle_type))} 기준, {len(events)}건)</h2>
  <ul class="criteria">{criteria_html}</ul>
  <p>과속·장기과속(도로 제한속도 필요)과 급앞지르기는 판정하지 않습니다. 승용차는 택시 기준을 적용합니다.</p>
  <table>
    <thead><tr><th>종류</th><th>영상 시각(초)</th><th>판정 근거</th></tr></thead>
    <tbody>{event_rows}</tbody>
  </table>
  {visuals_html}
  <h2>추출 목록 (전체 {len(records)}개 지점 중 {len(row_indices)}개 표시 - 위험운전 구간 우선, 나머지 전체 구간 균등 표본)</h2>
  <p>전체 원자료 CSV: {_esc(extraction.primary_source_file or "없음")}</p>
  <table>
    <thead><tr><th>#</th>{"<th>영상</th>" if sequence else ""}<th>시각(초)</th><th>위도</th><th>경도</th><th>속도(km/h)</th><th>위험운전</th><th>충격(g)</th></tr></thead>
    <tbody>{body_rows}</tbody>
  </table>
</body></html>
"""


class ReportExporter(QObject):

    def __init__(self, html_str: str, out_path: str, on_done, parent=None):
        super().__init__(parent)
        self._out_path = out_path
        self._on_done = on_done
        self._view = QWebEngineView()
        self._view.loadFinished.connect(self._on_load_finished)
        self._view.page().pdfPrintingFinished.connect(self._on_pdf_finished)
        self._view.setHtml(html_str, QUrl("about:blank"))

    # A4에 글 쓸 때처럼 양쪽에 여백을 둔다. printToPdf의 기본 레이아웃은 여백 0이다.
    PAGE_LAYOUT = QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                              QMarginsF(20, 18, 20, 18), QPageLayout.Millimeter)

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self._on_done(False, "리포트 HTML 로드 실패")
            return
        self._view.page().printToPdf(self._out_path, self.PAGE_LAYOUT)

    def _on_pdf_finished(self, file_path: str, success: bool) -> None:
        self._on_done(success, "" if success else "PDF 저장 실패")
