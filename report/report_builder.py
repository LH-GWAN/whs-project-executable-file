from __future__ import annotations

import base64
import html
import os
from typing import List, Optional

from PySide6.QtCore import QObject, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView

from core.acceleration import FlaggedSegment
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


def _select_row_indices(records: List[TrackPoint], segments: List[FlaggedSegment],
                          max_rows: int = MAX_TABLE_ROWS) -> List[int]:
    flagged_indices = set()
    for seg in segments:
        flagged_indices.update(range(seg.start_index, seg.end_index + 1))

    if len(records) <= max_rows:
        return list(range(len(records)))

    flagged = sorted(flagged_indices)
    if len(flagged) >= max_rows:
        step = len(flagged) / max_rows
        return [flagged[int(i * step)] for i in range(max_rows)]

    remaining = max_rows - len(flagged)
    stride = max(1, len(records) // remaining)
    sampled = set(range(0, len(records), stride))
    combined = sorted(flagged_indices | sampled)
    if len(combined) <= max_rows:
        return combined

    keep = set(flagged_indices)
    for index in combined:
        if len(keep) >= max_rows:
            break
        keep.add(index)
    return sorted(keep)


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
    segments = pipeline_result.flagged_segments
    row_indices = _select_row_indices(records, segments)

    flagged_indices = set()
    for seg in segments:
        flagged_indices.update(range(seg.start_index, seg.end_index + 1))

    rows_html = []
    prev_index: Optional[int] = None
    for idx in row_indices:
        if prev_index is not None and idx != prev_index + 1:
            rows_html.append('<tr class="gap"><td colspan="6">...</td></tr>')
        rec = records[idx]
        row_class = "flagged" if idx in flagged_indices else ""
        time_text = f"{rec.start_time_sec:.2f}" if rec.start_time_sec is not None else "-"
        speed_text = f"{rec.speed_kmh:.1f}" if rec.speed_kmh is not None else "-"
        g_value = rec.g_magnitude
        g_text = f"{g_value:.2f}" if g_value is not None else "-"
        if rec.has_fix:
            lat_text, lon_text = f"{rec.latitude:.6f}", f"{rec.longitude:.6f}"
        elif rec.is_dropout:
            lat_text = lon_text = "(GPS 끊김)"
        else:
            lat_text = lon_text = "(GPS 없음)"
        rows_html.append(
            f'<tr class="{row_class}">'
            f"<td>{idx + 1}</td>"
            f"<td>{_esc(time_text)}</td>"
            f"<td>{_esc(lat_text)}</td>"
            f"<td>{_esc(lon_text)}</td>"
            f"<td>{_esc(speed_text)}</td>"
            f"<td>{_esc(g_text)}</td>"
            "</tr>"
        )
        prev_index = idx

    body_rows = "".join(rows_html) if rows_html else '<tr><td colspan="6">추출된 좌표가 없습니다.</td></tr>'
    extraction = pipeline_result.extraction
    routing = extraction.routing
    video_filename = os.path.basename(pipeline_result.source_copy_path or "")
    fix_count = extraction.fix_count
    dropout_count = extraction.dropout_count

    # 그래프와 지도는 캡처 시점의 정적 이미지로 넣는다. 리포트 안에서 지도를 다시
    # 살려 그리면 타일 로딩 타이밍에 따라 결과가 달라져, 증거 문서로 쓰기 어렵다.
    visuals_html = (
        _image_section("속도 분석", chart_png,
                        "급가속 의심 구간은 붉게 표시됩니다. 점은 실제 GPS 측정 지점입니다.")
        + _image_section("이동 경로", map_png,
                          "초록 실선은 주행 경로, 붉은 구간은 급가속 의심 구간, "
                          "회색 점선은 GPS 수신이 끊긴 구간입니다.")
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  /* 다음 장으로 넘어갈 때 내용이 종이 맨 위에 붙어 인쇄되던 문제 - 페이지마다
     위아래 여백을 준다. printToPdf는 이 @page 여백을 그대로 반영한다. */
  @page {{ margin: 18mm 14mm; }}
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
  tr.flagged {{ background: #ffd9d9; }}
  tr.gap td {{ text-align: center; color: #999; border: none; }}
  .kv {{ font-size: 12px; margin: 2px 0; }}
  .kv b {{ display: inline-block; width: 120px; }}
</style></head><body>
  <h1>Extraction Report</h1>
  <h2>기본 정보</h2>
  <div class="kv"><b>사건번호</b>{_esc(case_number)}</div>
  <div class="kv"><b>담당자</b>{_esc(examiner)}</div>
  <div class="kv"><b>메모</b>{_esc(memo)}</div>
  <div class="kv"><b>원본 파일</b>{_esc(video_filename)}</div>
  <div class="kv"><b>SHA-256</b>{_esc(pipeline_result.sha256)}</div>
  <div class="kv"><b>재생시간</b>{_esc(_fmt_duration(pipeline_result.duration_sec))}</div>
  <div class="kv"><b>탐지 컨테이너</b>{_esc(routing.container.upper())}</div>
  <div class="kv"><b>시간축 근거</b>{_esc(extraction.time_source or "-")}</div>
  <div class="kv"><b>추출 지점</b>{_esc(len(records))}개 (GPS 수신 {_esc(fix_count)}개 /
      수신 끊김 {_esc(dropout_count)}개 / GPS 미기록 {_esc(len(records) - fix_count - dropout_count)}개)</div>
  <div class="kv"><b>급가속 임계값</b>{_esc(pipeline_result.accel_threshold_mps2)} m/s&sup2;</div>
  <div class="kv"><b>급가속 의심 구간</b>{_esc(len(segments))}개</div>

  {visuals_html}
  <h2>추출 목록 (전체 {len(records)}개 지점 중 {len(row_indices)}개 표시 - 급가속 구간 우선)</h2>
  <table>
    <thead><tr><th>#</th><th>시각(초)</th><th>위도</th><th>경도</th><th>속도(km/h)</th><th>충격(g)</th></tr></thead>
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

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self._on_done(False, "리포트 HTML 로드 실패")
            return
        self._view.page().printToPdf(self._out_path)

    def _on_pdf_finished(self, file_path: str, success: bool) -> None:
        self._on_done(success, "" if success else "PDF 저장 실패")
