"""손상 파일 검사 - 분석에 들어가기 전에 파일 전체를 훑어 어디가 어떻게 깨졌는지 알아낸다.

사용자 결정(2026-10-11): 손상이 있으면 분석 전에 알리고 '임의 복구 / 복구하지 않음(추천)'을 고르게 한다.
여기서는 판정만 한다. 실제 파싱은 엔진(engine/vendor)이 손상을 건너뛰며 하고, 구간 표시·임의 복구는
core/damage_marks.py, 헤더 이식은 core/damage_repair.py가 맡는다.

판정 항목
  - 시그니처 손상: 앞 8바이트가 RIFF/ftyp가 아니지만 내용(hdrl/movi/idx1, Box 사슬)으로 형식을 알 수 있음
  - 헤더 소실: AVI hdrl / MP4 moov가 없음(같은 기기 정상 영상의 헤더를 이식해야 영상 설정을 안다)
  - 색인 소실: AVI idx1이 없음(녹화 중단) - 엔진이 movi를 걸어 색인을 다시 만든다
  - 잘림: 선언 크기가 파일보다 큼, 마지막 Box가 끝에서 잘림
  - 손상 구간: 구조를 읽을 수 없어 건너뛴 바이트 구간, 미디어 안의 긴 0 구간(≥256 KB)
"""
from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from core.paths import ensure_vendor_importable

ensure_vendor_importable()

import integration_avi as _avi  # noqa: E402
import integration_blackbox as _blackbox  # noqa: E402
import integration_mp4 as _mp4  # noqa: E402

ZERO_RUN_MIN = 256 * 1024       # 이보다 긴 0 구간은 덮어쓰기 손상으로 본다
_BLOCK = 64 * 1024


@dataclass
class DamageReport:
    path: str = ""
    container: str = ""                 # "avi" / "mp4" / "" (판별 불가)
    size: int = 0
    signature_damaged: bool = False
    header_missing: bool = False        # AVI hdrl / MP4 moov 없음
    index_missing: bool = False         # AVI idx1 없음(색인 재구성 필요)
    fragmented: bool = False            # MP4 moof 구조
    truncated: bool = False
    gaps: List[List[int]] = field(default_factory=list)         # 구조 순회에서 건너뛴 [시작, 끝)
    zero_regions: List[List[int]] = field(default_factory=list) # 미디어 안의 긴 0 구간 [시작, 끝)
    media_range: Optional[List[int]] = None                     # 미디어 영역 [시작, 끝)
    media_end_basis: str = ""           # 미디어 끝을 어떻게 정했는지
    size_known: bool = True             # 미디어 범위를 확정할 수 있는가
    recoverable: bool = True            # 파싱을 시도할 수 있는가
    reasons: List[str] = field(default_factory=list)
    sniff_note: str = ""
    avi_movi_fourcc: Optional[int] = None
    avi_idx1_pos: Optional[int] = None
    mp4_first_moof: Optional[int] = None

    @property
    def damaged(self) -> bool:
        return bool(self.signature_damaged or self.header_missing or self.index_missing
                    or self.truncated or self.gaps or self.zero_regions or not self.recoverable)

    @property
    def needs_reference(self) -> bool:
        return self.header_missing

    @property
    def damaged_bytes(self) -> int:
        return sum(b - a for a, b in self.gaps) + sum(b - a for a, b in self.zero_regions)

    def regions(self) -> List[List[int]]:
        """건너뛴 구간과 0 구간을 합쳐 정렬·병합한 [시작, 끝) 목록."""
        items = sorted([list(r) for r in self.gaps] + [list(r) for r in self.zero_regions])
        merged: List[List[int]] = []
        for a, b in items:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return merged

    def summary_lines(self) -> List[str]:
        lines = []
        if self.signature_damaged:
            lines.append("파일 시그니처(앞부분)가 손상됐습니다. 내용으로 형식을 판별했습니다.")
        if self.header_missing:
            lines.append("헤더(영상 설정)가 사라졌습니다. 같은 기기의 정상 영상에서 설정을 이식해야 영상·시간축을 복구할 수 있습니다."
                         if self.container == "avi" or self.fragmented else
                         "헤더(moov)가 사라졌습니다. 샘플 표가 없어 영상은 복구할 수 없고 GPS만 UTC 기준으로 회수합니다.")
        if self.index_missing:
            lines.append("색인(idx1)이 없습니다(녹화 중단). 영상 데이터를 직접 걸어 색인을 다시 만듭니다.")
        if self.truncated:
            lines.append("파일 끝이 잘렸습니다(선언된 크기보다 짧음).")
        if self.gaps:
            lines.append(f"구조를 읽을 수 없는 구간 {len(self.gaps)}곳 ({sum(b - a for a, b in self.gaps) / 1048576:.1f} MB)")
        if self.zero_regions:
            lines.append(f"0으로 채워진 구간 {len(self.zero_regions)}곳 ({sum(b - a for a, b in self.zero_regions) / 1048576:.1f} MB)")
        if not self.recoverable:
            lines.append("복구 불가: " + ("; ".join(self.reasons) if self.reasons else "미디어 구조를 찾을 수 없습니다."))
        return lines or ["손상이 발견되지 않았습니다."]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DamageReport":
        known = {k: data.get(k) for k in cls.__dataclass_fields__ if k in data}
        report = cls(**known)
        report.gaps = [list(g) for g in (report.gaps or [])]
        report.zero_regions = [list(g) for g in (report.zero_regions or [])]
        return report


def _zero_runs(path: str, start: int, end: int, cancel_event=None) -> List[List[int]]:
    """[start, end) 안에서 ZERO_RUN_MIN 이상 이어지는 0 구간. 64 KB 블록 단위로 본다."""
    out: List[List[int]] = []
    if end - start < ZERO_RUN_MIN:
        return out
    zero_block = bytes(_BLOCK)
    with open(path, "rb") as f:
        f.seek(start)
        pos = start
        run_start = None
        while pos < end:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("cancelled")
            block = f.read(min(_BLOCK, end - pos))
            if not block:
                break
            is_zero = block == zero_block[:len(block)]
            if is_zero and run_start is None:
                run_start = pos
            elif not is_zero and run_start is not None:
                if pos - run_start >= ZERO_RUN_MIN:
                    out.append([run_start, pos])
                run_start = None
            pos += len(block)
        if run_start is not None and pos - run_start >= ZERO_RUN_MIN:
            out.append([run_start, pos])
    return out


def _scan_avi(path: str, report: DamageReport, cancel_event=None) -> None:
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        size = len(mm)
        hdrl, movi_list, idx1, _avix = _avi.find_top_level_sections(mm)
        report.header_missing = hdrl is None
        if bytes(mm[0:4]) == b"RIFF":
            declared = struct.unpack_from("<I", mm, 4)[0] + 8
            if declared > size + 1:
                report.truncated = True
                report.reasons.append(f"RIFF 선언 크기 {declared:,}바이트 > 실제 {size:,}바이트")
        movi = movi_list[0] if movi_list else None
        if movi is None:
            pos = _avi.find_movi_fallback(mm)
            if pos is None:
                report.recoverable = False
                report.size_known = False
                report.reasons.append("movi(영상 데이터 영역)를 찾을 수 없음")
                return
            fourcc = pos
            movi_end = size
        else:
            fourcc = movi.data_start
            movi_end = movi.data_end
        report.avi_movi_fourcc = fourcc
        if idx1 is None:
            fb = _avi.find_idx1_fallback(mm)
            if fb is None:
                report.index_missing = True
                basis = "movi 청크 걷기"
            else:
                report.avi_idx1_pos = fb[0]
                movi_end = min(movi_end, fb[0])
                basis = "idx1(바이트 스캔)"
        else:
            report.avi_idx1_pos = idx1.pos
            movi_end = min(movi_end, idx1.pos)
            basis = "idx1"
        # 청크를 걸어 읽을 수 없는 구간을 찾는다. idx1이 없으면 엔진과 같은 규칙으로 녹화 끝(옛 녹화 잔재
        # 시작)을 UTC 연속성으로 정한다.
        gaps: List[tuple] = []
        walked = []
        for pos, cid, csize in _avi.walk_movi_chunks(mm, fourcc + 4, movi_end, gaps):
            walked.append((pos, cid, csize))
            if cancel_event is not None and len(walked) % 2000 == 0 and cancel_event.is_set():
                raise InterruptedError("cancelled")
        gaps = sorted(gaps)
        if report.index_missing:
            cut = _avi.recording_cut_at(mm, walked, gaps)
            if cut is not None:
                gaps = [g for g in gaps if g[0] < cut]
                movi_end = cut
                basis = "movi 청크 걷기 (GPS UTC 연속성으로 끝 판정)"
            elif walked:
                last = walked[-1]
                movi_end = min(movi_end, last[0] + 8 + last[2] + (last[2] & 1))
        else:
            # idx1이 있으면 참조된 마지막 청크 끝까지가 현재 녹화다(사전 할당 파일의 뒤쪽 잔재 제외).
            entries = _avi.parse_idx1(mm, (idx1.data_start if idx1 else fb[0] + 8),
                                      (idx1.ck_size if idx1 else fb[1]))
            base = _avi.infer_base_from_entries(mm, entries) if entries else None
            if base is None:
                base = fourcc
            last_end = 0
            for e in entries:
                stop = base + e["idx_offset"] + 8 + e["length"]
                if 0 < stop <= movi_end and bytes(mm[base + e["idx_offset"]:base + e["idx_offset"] + 4]) == e["chunk_id"]:
                    last_end = max(last_end, stop)
            if last_end:
                movi_end = min(movi_end, last_end + 1)
                gaps = [g for g in gaps if g[0] < last_end]
        report.gaps = [[a, min(b, movi_end)] for a, b in gaps if a < movi_end]
        report.media_range = [fourcc + 4, movi_end]
        report.media_end_basis = basis
        report.zero_regions = _zero_runs(path, fourcc + 4, movi_end, cancel_event)
        if report.header_missing:
            report.reasons.append("hdrl(스트림 헤더) 없음")
        if report.index_missing:
            report.reasons.append("idx1(색인) 없음")


def _scan_mp4(path: str, report: DamageReport, cancel_event=None) -> None:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        boxes, boundary, kind = _mp4.scan_top_level_bounded(f, size)
        gaps = [list(g) for g in _mp4.DAMAGE_GAPS]
    types = [b.box_type for b in boxes]
    report.fragmented = b"moof" in types
    report.header_missing = b"moov" not in types
    report.index_missing = report.header_missing and not report.fragmented
    report.truncated = kind == "truncated"
    if kind == "truncated":
        report.reasons.append("마지막 Box가 파일 끝에서 잘림")
    if boundary is not None and kind == "corrupt":
        # 재동기화도 안 되는 꼬리: 손상 구간으로 둔다(DAMAGE_GAPS에 이미 있음).
        pass
    mdats = [b for b in boxes if b.box_type == b"mdat"]
    moov = next((b for b in boxes if b.box_type == b"moov"), None)
    moofs = [b for b in boxes if b.box_type == b"moof"]
    if moofs:
        report.mp4_first_moof = moofs[0].start
    if not mdats and moov is not None and gaps:
        # mdat 머리가 0으로 덮였지만 moov(샘플 표)가 살아 있는 일반 MP4: 샘플은 표가 가리키는 위치로
        # 읽는다. 머리 손상 영역을 미디어로 둔다.
        media_start, media_end = gaps[0][0], moov.start
        report.media_end_basis = "moov 샘플 표"
    elif not mdats:
        report.recoverable = False
        report.size_known = False
        report.reasons.append("mdat(영상 데이터 Box)를 찾을 수 없음")
        return
    else:
        media_start = mdats[0].payload_start
        media_end = min(max(b.end for b in mdats), size)
        report.media_end_basis = "Box 사슬"
    report.media_range = [media_start, media_end]
    if moov is not None and not report.fragmented:
        # 샘플 표가 살아 있으면 Box 순회의 빈 구간은 손상 구간이 아니다(샘플은 표로 찾는다). 샘플이
        # 지워진 곳은 0 구간 검사로 잡는다. 조각(moof) 구조는 moof가 지워지면 그 조각을 못 읽으니 유지.
        report.gaps = []
    else:
        report.gaps = [[a, b] for a, b in gaps if a < media_end + 1 or b > media_start]
    # mdat 안의 긴 0 구간(구조는 멀쩡하나 샘플이 지워진 경우). 손상 구간과 겹치는 건 뺀다.
    zeros: List[List[int]] = []
    scan_ranges = [(b.payload_start, min(b.end, size)) for b in mdats] or [(media_start, media_end)]
    for lo, hi in scan_ranges:
        for a, z in _zero_runs(path, lo, hi, cancel_event):
            if not any(ga <= a and z <= gb for ga, gb in report.gaps):
                zeros.append([a, z])
    report.zero_regions = zeros
    if report.header_missing:
        report.reasons.append("moov(헤더) 없음" + (" - 조각(moof) 구조" if report.fragmented else " - 샘플 표 소실"))


def scan_damage(path: str, cancel_event=None) -> DamageReport:
    report = DamageReport(path=path, size=os.path.getsize(path))
    container, note = _blackbox.detect_container(path)
    report.sniff_note = note or ""
    report.signature_damaged = bool(note and "시그니처 손상" in note)
    if container is None:
        report.container = ""
        report.recoverable = False
        report.size_known = False
        report.reasons.append(note or "형식을 판별할 수 없음")
        return report
    report.container = container
    # 엔진 함수들은 모듈 WARNINGS에 경고를 쌓는다. 검사 중 쌓인 것은 분석 결과와 섞지 않게 비운다.
    try:
        if container == _blackbox.CONTAINER_AVI:
            _scan_avi(path, report, cancel_event)
        else:
            _scan_mp4(path, report, cancel_event)
    finally:
        _avi.WARNINGS.clear()
        _mp4.WARNINGS.clear()
    return report
