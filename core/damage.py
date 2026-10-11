"""손상 파일 검사 - 분석에 들어가기 전에 파일 전체를 훑어 어디가 어떻게 깨졌는지 알아낸다.

사용자 결정(2026-10-11): 손상이 있으면 분석 전에 알리고 '임의 복구 / 복구하지 않음(추천)'을 고르게 한다.
여기서는 판정만 한다. 실제 파싱은 엔진(engine/vendor)이 손상을 건너뛰며 하고, 구간 표시·임의 복구는
core/damage_marks.py, 헤더 이식은 core/damage_repair.py가 맡는다.

판정 항목
  - 시그니처 손상: 앞 8바이트가 RIFF/ftyp가 아니지만 내용(hdrl/movi/idx1, Box 사슬)으로 형식을 알 수 있음
  - 헤더 소실·손상: AVI hdrl / MP4 moov가 없거나, 태그는 있는데 내용(avih/strh, trak/샘플 표)을 읽을 수 없음
    (같은 기기 정상 영상의 헤더를 이식해야 영상 설정을 안다)
  - 색인 소실·손상: AVI idx1이 없음(녹화 중단) - 엔진이 movi를 걸어 색인을 다시 만든다 / idx1 엔트리 일부가 깨짐
  - 잘림: 선언 크기가 파일보다 큼, 마지막 Box가 끝에서 잘림
  - 손상 구간: 구조를 읽을 수 없어 건너뛴 바이트 구간, 미디어 안의 긴 0 구간(≥256 KB)

파일은 mmap이 아니라 read()로 읽는다(리뷰 2026-10-11 #83): 매체(SD 카드) 오류가 나면 mmap은 페이지
오류로 프로세스가 그냥 죽지만 read()는 OSError가 나서 안내할 수 있다. 8 MB 창 단위로 읽어 속도는 유지한다.
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field, asdict
from typing import Iterator, List, Optional, Tuple

from core.paths import ensure_vendor_importable

ensure_vendor_importable()

import integration_avi as _avi  # noqa: E402
import integration_blackbox as _blackbox  # noqa: E402
import integration_mp4 as _mp4  # noqa: E402

ZERO_RUN_MIN = 256 * 1024       # 이보다 긴 0 구간은 덮어쓰기 손상으로 본다
_BLOCK = 64 * 1024              # 0 구간 판정 단위
_WINDOW = 8 * 1024 * 1024       # read() 창
_HDRL_SCAN_LIMIT = 4 * 1024 * 1024
_BASE_INFER_LIMIT = 16 * 1024 * 1024
_AVI_STREAM_TYPES = (b"vids", b"auds", b"txts", b"mids", b"iavs")
_MP4_SLACK_BOUNDARIES = {
    "ftyp": "두 번째 ftyp", "moov": "두 번째 moov", "moof": "일반 MP4 뒤의 moof",
    "tfdt": "tfdt 되감김", "mfhd": "mfhd 순번 불연속", "gps": "GPS 날짜·UTC 불연속",
}


@dataclass
class DamageReport:
    path: str = ""
    container: str = ""                 # "avi" / "mp4" / "" (판별 불가)
    size: int = 0
    signature_damaged: bool = False
    header_missing: bool = False        # AVI hdrl / MP4 moov 없음(또는 내용 손상: header_damaged)
    index_missing: bool = False         # AVI idx1 없음(색인 재구성 필요) / MP4 moov 없는 일반 파일
    fragmented: bool = False            # MP4 moof 구조
    truncated: bool = False
    gaps: List[List[int]] = field(default_factory=list)         # 구조 순회에서 건너뛴 [시작, 끝)
    zero_regions: List[List[int]] = field(default_factory=list) # 미디어 안의 긴 0 구간 [시작, 끝)
    media_range: Optional[List[int]] = None                     # 미디어 영역 [시작, 끝)
    media_end_basis: str = ""           # 미디어 끝을 어떻게 정했는지
    size_known: bool = True             # 미디어 범위를 확정할 수 있는가
    recoverable: bool = True            # 파싱을 시도할 수 있는가(형식 판별 실패·미디어 없음이면 False)
    reasons: List[str] = field(default_factory=list)
    sniff_note: str = ""
    avi_movi_fourcc: Optional[int] = None
    avi_idx1_pos: Optional[int] = None
    mp4_first_moof: Optional[int] = None
    # 2026-10-11 리뷰 반영으로 추가(의미 추가만, 기존 필드 뜻은 그대로)
    header_damaged: bool = False        # 헤더 태그(hdrl/moov)는 있으나 내용을 읽을 수 없음 (header_missing도 True)
    index_damaged: bool = False         # AVI idx1 엔트리 일부가 깨짐(참조되지 않는 구간이 손상 구간으로 들어감)
    avi_movi_end: Optional[int] = None  # movi LIST가 선언한(또는 idx1 앞까지의) 끝 - 옛 녹화 잔재(슬랙) 포함
    mp4_media_start: Optional[int] = None  # 헤더 손상 뒤 처음 나오는 미디어 Box(mdat/moof) 시작
    tail_residue: Optional[List[int]] = None  # MP4: 마지막 Box 뒤 ~ 파일 끝의 읽을 수 없는 잔재(옛 녹화 흔적 가능)

    @property
    def damaged(self) -> bool:
        return bool(self.signature_damaged or self.header_missing or self.index_missing or self.index_damaged
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
        is_avi = self.container == "avi"
        if self.signature_damaged:
            lines.append("파일 시그니처(앞부분)가 손상됐습니다. 내용으로 형식을 판별했습니다.")
        if self.header_missing:
            what = "헤더(영상 설정) 내용이 손상됐습니다" if self.header_damaged else "헤더(영상 설정)가 사라졌습니다"
            if is_avi or self.fragmented:
                lines.append(f"{what}. 같은 기기의 정상 영상에서 설정을 이식해야 영상·시간축을 복구할 수 있습니다.")
            else:
                lines.append(("헤더(moov) 내용이 손상됐습니다" if self.header_damaged else "헤더(moov)가 사라졌습니다")
                             + ". 샘플 표를 쓸 수 없어 영상은 복구할 수 없고 GPS만 UTC 기준으로 회수합니다.")
        if self.index_missing and is_avi:
            # MP4의 index_missing은 'moov 없는 일반 MP4'라는 뜻이라 idx1 문구를 넣지 않는다(리뷰 #121).
            lines.append("색인(idx1)이 없습니다(녹화 중단). 영상 데이터를 직접 걸어 색인을 다시 만듭니다.")
        if self.index_damaged:
            lines.append("색인(idx1) 일부가 손상됐습니다. 색인이 가리키지 못하는 뒷부분은 손상 구간으로 표시합니다.")
        if self.truncated:
            lines.append("파일 끝이 잘렸습니다(선언된 크기보다 짧음).")
        gaps = [g for g in self.gaps if not (self.tail_residue and list(g) == list(self.tail_residue))]
        if gaps:
            lines.append(f"구조를 읽을 수 없는 구간 {len(gaps)}곳 ({sum(b - a for a, b in gaps) / 1048576:.1f} MB)")
        if self.tail_residue:
            lines.append(f"파일 끝 잔재(옛 녹화 흔적 가능) {(self.tail_residue[1] - self.tail_residue[0]) / 1048576:.1f} MB"
                         " - 현재 녹화 뒤의 읽을 수 없는 꼬리")
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


# ---------------------------------------------------------------------------
# read() 기반 파일 접근 도우미
# ---------------------------------------------------------------------------

def _read_at(f, pos: int, n: int) -> bytes:
    """pos에서 n바이트. 파일 끝이면 짧게 돌아온다. 매체 오류는 OSError로 올라온다."""
    if n <= 0 or pos < 0:
        return b""
    f.seek(pos)
    return f.read(n)


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("cancelled")


def _find_bytes(f, needle: bytes, start: int, end: int, cancel_event=None) -> int:
    """[start, end)에서 needle이 처음 나오는 절대 위치. 없으면 -1. 창을 겹쳐 읽어 경계에 걸친 것도 찾는다."""
    overlap = max(len(needle) - 1, 0)
    pos = start
    while pos < end:
        _check_cancel(cancel_event)
        buf = _read_at(f, pos, min(_WINDOW, end - pos))
        if not buf:
            return -1
        i = buf.find(needle)
        if i >= 0:
            return pos + i
        if pos + len(buf) >= end:
            return -1
        pos += max(len(buf) - overlap, 1)
    return -1


def _rfind_iter(f, needle: bytes, start: int, end: int, cancel_event=None) -> Iterator[int]:
    """[start, end)에서 needle이 나오는 절대 위치를 뒤에서부터 차례로 낸다."""
    overlap = max(len(needle) - 1, 0)
    hi = end
    last_yielded = None
    while hi > start:
        _check_cancel(cancel_event)
        lo = max(start, hi - _WINDOW)
        buf = _read_at(f, lo, hi - lo)
        if not buf:
            return
        i = len(buf)
        while True:
            i = buf.rfind(needle, 0, i)
            if i < 0:
                break
            p = lo + i
            if last_yielded is None or p < last_yielded:
                last_yielded = p
                yield p
            i += len(needle) - 1
        if lo <= start:
            return
        hi = lo + overlap


def _zero_runs(path: str, start: int, end: int, cancel_event=None) -> List[List[int]]:
    """[start, end) 안에서 ZERO_RUN_MIN 이상 이어지는 0 구간. 64 KB 블록 단위로 보고 8 MB씩 읽는다."""
    out: List[List[int]] = []
    if end - start < ZERO_RUN_MIN:
        return out
    zero_block = bytes(_BLOCK)
    with open(path, "rb") as f:
        pos = start
        run_start = None
        while pos < end:
            _check_cancel(cancel_event)
            buf = _read_at(f, pos, min(_WINDOW, end - pos))
            if not buf:
                break
            for i in range(0, len(buf), _BLOCK):
                block = buf[i:i + _BLOCK]
                bpos = pos + i
                is_zero = block == zero_block[:len(block)]
                if is_zero and run_start is None:
                    run_start = bpos
                elif not is_zero and run_start is not None:
                    if bpos - run_start >= ZERO_RUN_MIN:
                        out.append([run_start, bpos])
                    run_start = None
            pos += len(buf)
        if run_start is not None and pos - run_start >= ZERO_RUN_MIN:
            out.append([run_start, pos])
    return out


def _subtract_ranges(runs: List[List[int]], excluded: List[Tuple[int, int]], min_len: int) -> List[List[int]]:
    """runs에서 excluded 구간을 뺀 나머지 가운데 min_len 이상인 것."""
    if not excluded:
        return runs
    excluded = sorted(excluded)
    out: List[List[int]] = []
    for a, b in runs:
        pieces = [[a, b]]
        for ea, eb in excluded:
            if eb <= a or ea >= b:
                continue
            nxt = []
            for pa, pb in pieces:
                if eb <= pa or ea >= pb:
                    nxt.append([pa, pb])
                    continue
                if pa < ea:
                    nxt.append([pa, ea])
                if eb < pb:
                    nxt.append([eb, pb])
            pieces = nxt
        out.extend(p for p in pieces if p[1] - p[0] >= min_len)
    return out


# ---------------------------------------------------------------------------
# AVI 구조 (read() 기반)
#   - movi 걷기(청크 규칙·재동기화·'LIST rec ' 그룹·0 꼬리·잘린 꼬리 판정)는 엔진 walk_movi_chunks를 8 MB
#     바이트 창 위에서 그대로 돌리고, 창 경계만 여기서 잇는다. 규칙이 엔진과 어긋나지 않게 하기 위해서다.
#   - 색인 검증·첫 청크 사슬·녹화 경계(recording_cut_at)는 파일 곳곳을 조금씩 읽어야 해서 엔진과 같은 규칙으로
#     read() 위에서 다시 썼고, 순수 판정 함수(_entry_target_state·_score_base·_spread_sample·_chunk_rmc_anchor·
#     _is_zero_region 등)는 엔진 것을 그대로 부른다. 동등성은 tests/test_damage_recovery.py가 엔진과 비교한다.
# ---------------------------------------------------------------------------

@dataclass
class AviSection:
    pos: int            # 청크 머리 위치
    data_start: int     # 머리 뒤(LIST면 listType 위치)
    data_end: int       # 선언(또는 clamp된) 끝
    ck_size: int
    truncated: bool = False


class _FileView:
    """len()과 슬라이스만 지원하는 파일 창. 그 둘만 쓰는 엔진 판정 함수(_entry_target_state, _is_zero_region,
    _zero_fraction …)에 mmap 대신 넘긴다. 슬라이스마다 read()한다."""
    __slots__ = ("f", "size")

    def __init__(self, f, size: int):
        self.f = f
        self.size = size

    def __len__(self) -> int:
        return self.size

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(self.size)
            if step != 1 or stop <= start:
                return b""
            return _read_at(self.f, start, stop - start)
        if key < 0:
            key += self.size
        b = _read_at(self.f, key, 1)
        if not b:
            raise IndexError(key)
        return b[0]


class _QuietEngine:
    """엔진 walk를 창마다 돌리면 창 끝에서 '마지막 청크가 잘림' 같은 경고가 창 수만큼 찍힌다. 검사 중엔 엔진
    경고를 WARNINGS에만 쌓고 출력하지 않는다(WARNINGS는 scan_damage가 비운다)."""

    def __enter__(self):
        self._orig = _avi.warn
        _avi.warn = lambda msg: _avi.WARNINGS.append(msg)
        return self

    def __exit__(self, *exc):
        _avi.warn = self._orig
        return False


def _chunk_header_at(f, pos: int, end: int, buf: Optional[bytes] = None, base: int = 0):
    """pos(절대)에 그럴듯한 movi 청크 머리가 있는가(엔진 _chunk_header_ok와 같은 규칙: 규격 청크 ID 또는
    'LIST....rec ' 그룹, 크기 ≤ 64 MB, 끝이 end 안). buf가 pos를 덮으면 buf에서, 아니면 파일에서 읽는다."""
    if pos < 0 or pos + 8 > end:
        return None
    if buf is not None and base <= pos and pos + 12 <= base + len(buf):
        head = buf[pos - base:pos - base + 12]
    else:
        head = _read_at(f, pos, 12)
        if len(head) < 8:
            return None
    cid = head[:4]
    size = struct.unpack("<I", head[4:8])[0]
    if cid == b"LIST":
        if pos + 12 > end or head[8:12] != b"rec ":
            return None
        if size < 4 or size > _avi.MAX_CHUNK_BYTES or pos + 8 + size > end:
            return None
        return cid, size
    if not _avi.CHUNK_ID_RE.fullmatch(cid):
        return None
    if size > _avi.MAX_CHUNK_BYTES or pos + 8 + size > end:
        return None
    return cid, size


def _byte_at(f, pos: int, buf: Optional[bytes] = None, base: int = 0) -> bytes:
    if buf is not None and base <= pos < base + len(buf):
        return buf[pos - base:pos - base + 1]
    return _read_at(f, pos, 1)


def _next_header_fits(f, nxt: int, end: int, buf=None, base=0) -> bool:
    """재동기화 후보 뒤에 다음 머리가 맞는가(또는 끝에 닿는가). 홀수 패딩 1바이트도 허용(엔진 _next_header_ok)."""
    if nxt >= end:
        return True
    if _chunk_header_at(f, nxt, end, buf, base) is not None:
        return True
    return _byte_at(f, nxt, buf, base) == b"\0" and _chunk_header_at(f, nxt + 1, end, buf, base) is not None


def first_chunk_chain(f, size: int, start: int = 0, end: Optional[int] = None, cancel_event=None) -> Optional[int]:
    """start부터 스트림 청크 머리 두 개가 연달아 맞는 첫 위치(엔진 first_chunk_chain). 없으면 None."""
    end = size if end is None else min(end, size)
    pos = start
    while pos + 8 <= end:
        _check_cancel(cancel_event)
        buf = _read_at(f, pos, min(_WINDOW, end - pos))
        if len(buf) < 8:
            return None
        scan = 0
        while scan + 8 <= len(buf):
            m = _avi.STREAM_CHUNK_ID_RE.search(buf, scan)
            if m is None or m.start() + 8 > len(buf):
                break
            cand = pos + m.start()
            h = _chunk_header_at(f, cand, end, buf, pos)
            if h is not None:
                nxt = cand + 8 + h[1] + (h[1] & 1)
                if nxt < end and _next_header_fits(f, nxt, end, buf, pos):
                    return cand
            scan = m.start() + 1
        if pos + len(buf) >= end:
            return None
        pos += max(len(buf) - 8, 1)
    return None


def _classify_tail(f, size: int, pos: int, end: int, gaps, tail_info) -> None:
    """재동기화가 안 되는 마지막 구간 [pos, end)의 성격(엔진 walk_movi_chunks 끝부분과 같은 규칙):
    파일 끝에서 잘린 마지막 청크 → truncated_tail, 0 꼬리(미사용 할당) → zero_tail, 아니면 손상 구간."""
    head = _read_at(f, pos, 8)
    if end >= size and len(head) == 8 and _avi.STREAM_CHUNK_ID_RE.fullmatch(head[:4]) \
            and pos + 8 + struct.unpack("<I", head[4:8])[0] > end:
        if tail_info is not None:
            tail_info["truncated_tail"] = pos
    elif _avi._is_zero_region(_FileView(f, size), pos, min(end, pos + _avi.ZERO_TAIL_PROBE)):
        if tail_info is not None:
            tail_info["zero_tail"] = (pos, end)
    elif gaps is not None:
        gaps.append((pos, end))


def _resync_from(f, start: int, end: int, cancel_event=None) -> Optional[int]:
    """start부터 '머리가 맞고 그 다음 머리도 맞는(또는 끝에 닿는)' 청크 위치(엔진 walk_movi_chunks의 재동기화
    규칙, 파일 기준). 없으면 None."""
    pos = start
    while pos + 8 <= end:
        _check_cancel(cancel_event)
        buf = _read_at(f, pos, min(_WINDOW, end - pos))
        if len(buf) < 8:
            return None
        scan = 0
        while scan + 8 <= len(buf):
            m = _avi._RESYNC_RE.search(buf, scan)
            if m is None or m.start() + 8 > len(buf):
                break
            cand = pos + m.start()
            h = _chunk_header_at(f, cand, end, buf, pos)
            if h is not None:
                nxt = cand + 12 if h[0] == b"LIST" else cand + 8 + h[1] + (h[1] & 1)
                if _next_header_fits(f, nxt, end, buf, pos):
                    return cand
            scan = m.start() + 1
        if pos + len(buf) >= end:
            return None
        pos += max(len(buf) - 8, 1)
    return None


def walk_movi_chunks(f, start: int, end: int, gaps: Optional[list] = None, cancel_event=None,
                     tail_info: Optional[dict] = None) -> Iterator[Tuple[int, bytes, int]]:
    """movi 영역을 청크 머리로 걷는다. 엔진 walk_movi_chunks를 8 MB 창(bytes) 위에서 돌리고 창 경계를 잇는다:
    창 끝에 걸린 청크·rec 그룹은 파일 기준으로 다시 보고, 읽을 수 없는 머리를 만나면 파일 기준 재동기화
    (_resync_from, 엔진과 같은 '머리 두 개 연속' 규칙)로 손상 구간을 정한다. 건너뛴 구간은 gaps에 (시작, 끝),
    0 꼬리·잘린 마지막 청크는 tail_info에(zero_tail=(pos, end) / truncated_tail=pos).
    yield (절대 위치, 청크 ID, 크기)."""
    f.seek(0, 2)
    size = f.tell()
    pos = start

    def resolve(gap_pos: int):
        """gap_pos의 머리를 읽을 수 없다: 재동기화 위치(없으면 None, 꼬리 판정은 tail_info/gaps에)."""
        found = _resync_from(f, gap_pos + 1, end, cancel_event)
        if found is None:
            _classify_tail(f, size, gap_pos, end, gaps, tail_info)
        elif gaps is not None:
            gaps.append((gap_pos, found))
        return found

    with _QuietEngine():
        while pos + 8 <= end:
            _check_cancel(cancel_event)
            # 창 머리의 패딩 바이트(홀수 청크 뒤)와 창보다 큰 청크·rec 그룹은 엔진이 창 안에서 판정할 수 없다 -
            # 엔진 규칙 그대로 파일 끝 기준으로 직접 본다.
            head = _chunk_header_at(f, pos, end)
            if head is None and _byte_at(f, pos) == b"\0" and _chunk_header_at(f, pos + 1, end) is not None:
                pos += 1
                continue
            if head is not None and pos + 8 + head[1] > pos + _WINDOW - 16:
                cid, csize = head
                if cid == b"LIST":
                    pos += 12
                    continue
                yield pos, cid, csize
                nxt = pos + 8 + csize
                if csize & 1 and _chunk_header_at(f, nxt, end) is None and _byte_at(f, nxt) == b"\0" \
                        and _chunk_header_at(f, nxt + 1, end) is not None:
                    nxt += 1
                pos = nxt
                continue
            if head is None:
                found = resolve(pos)
                if found is None:
                    return
                pos = found
                continue
            win_len = min(_WINDOW, end - pos)
            buf = _read_at(f, pos, win_len)
            if len(buf) < 8:
                break
            if len(buf) < win_len:
                end = pos + len(buf)
            local_gaps: list = []
            local_tail: dict = {}
            last_end = None
            rewind = None
            finished = False
            n_seen = 0
            for lpos, cid, csize in _avi.walk_movi_chunks(buf, 0, len(buf), local_gaps, local_tail):
                # 엔진이 이 창 안에서 재동기화한 구간: 창에 다 들어가지 않아 머리를 못 읽은 것(파일 기준으로는 멀쩡한
                # 청크·rec 그룹)이면 거기서 창을 다시 잡고, 진짜 손상이면 파일 기준 재동기화 위치로 구간을 정한다.
                while n_seen < len(local_gaps):
                    ga = pos + local_gaps[n_seen][0]
                    gb = pos + local_gaps[n_seen][1]
                    n_seen += 1
                    if _chunk_header_at(f, ga, end) is not None or \
                            (_byte_at(f, ga, buf, pos) == b"\0" and _chunk_header_at(f, ga + 1, end) is not None):
                        rewind = ga
                        break
                    found = resolve(ga)
                    if found is None:
                        finished = True
                        break
                    if found != gb:
                        rewind = found
                        break
                if rewind is not None or finished:
                    break
                yield pos + lpos, cid, csize
                last_end = pos + lpos + 8 + csize
            if finished:
                return
            if rewind is not None:
                pos = rewind
                continue
            # 창 끝에서 끝나지 않은 구간(재동기화 실패·0 꼬리·잘린 청크): 그 자리부터 파일 기준으로 다시 본다.
            stop = None
            if local_tail.get("truncated_tail") is not None:
                stop = local_tail["truncated_tail"]
            elif local_tail.get("zero_tail"):
                stop = local_tail["zero_tail"][0]
            elif len(local_gaps) > n_seen:
                stop = local_gaps[-1][0]
            if stop is not None:
                pos = pos + stop
                continue
            if last_end is None:
                break
            pos = last_end


def _scan_list(f, size: int, list_type: bytes, start: int, limit: Optional[int] = None,
               cancel_event=None) -> Optional[AviSection]:
    """'LIST....<list_type>' 머리를 바이트 스캔으로 찾는다(엔진 _scan_list). 선언 크기는 파일 끝으로 clamp."""
    limit = size if limit is None else min(limit, size)
    pos = start
    while True:
        p = _find_bytes(f, list_type, pos, limit, cancel_event)
        if p < 0:
            return None
        if p >= 8:
            head = _read_at(f, p - 8, 8)
            if head[:4] == b"LIST":
                ck_size = struct.unpack("<I", head[4:8])[0]
                if ck_size >= 4:
                    return AviSection(pos=p - 8, data_start=p, data_end=min(p + ck_size, size), ck_size=ck_size,
                                      truncated=p + ck_size > size)
        pos = p + 4


def _infer_base(f, size: int, entries) -> Optional[int]:
    """엔진 infer_base_from_entries와 같은 규칙: 표본 엔트리의 (id, size) 머리를 파일 앞 16 MB에서 찾아 base
    후보를 투표하고, 표본과 과반 일치하는 후보만 받는다."""
    if not entries:
        return None
    sample = _avi._spread_sample(entries)
    prefix = _read_at(f, 0, min(size, _BASE_INFER_LIMIT))
    votes: dict = {}
    for e in sample:
        cid, _flags, off, length = _avi._entry_tuple(e)
        if cid == b"rec ":
            continue
        needle = cid + struct.pack("<I", length)
        p = prefix.find(needle)
        hops = 0
        while p >= 0 and hops < 64:
            base = p - off
            if base >= 0:
                votes[base] = votes.get(base, 0) + 1
            p = prefix.find(needle, p + 1)
            hops += 1
    if not votes:
        return None
    view = _FileView(f, size)
    best = None
    for base, _v in sorted(votes.items(), key=lambda kv: -kv[1])[:8]:
        hits, decided = _avi._score_base(view, base, sample)
        if hits >= 3 and hits * 2 >= decided and (best is None or hits > best[1]):
            best = (base, hits)
    return best[0] if best else None


def _index_reaches_chain(f, size: int, base: int, entries, chain_start: Optional[int], max_chunks: int = 256) -> bool:
    """색인이 현재 녹화의 첫 청크 사슬과 맞물리는가(엔진 _index_reaches_chain)."""
    if chain_start is None:
        return True
    targets = [base + _avi._entry_tuple(e)[2] for e in entries]
    if not targets:
        return False
    first = min(targets)
    if first <= chain_start:
        return True
    pos = chain_start
    for _ in range(max_chunks):
        if pos == first:
            return True
        if pos > first:
            return False
        head = _chunk_header_at(f, pos, size)
        if head is None:
            if _byte_at(f, pos) == b"\0" and _chunk_header_at(f, pos + 1, size) is not None:
                pos += 1
                continue
            return False
        pos = pos + 12 if head[0] == b"LIST" else pos + 8 + head[1] + (head[1] & 1)
    return False


def _idx1_plausible(f, size: int, pos: int, idx_size: int, chain_start: Optional[int] = None) -> Optional[int]:
    """바이트 스캔으로 찾은 idx1이 현재 녹화의 것인지(엔진 _idx1_entries_plausible과 같은 규칙). 맞으면 base."""
    n_total = idx_size // 16
    if n_total == 0:
        return None
    n_head = min(n_total, 16)
    raw = _read_at(f, pos + 8, n_head * 16)
    if len(raw) < n_head * 16:
        return None
    head_entries = [struct.unpack_from("<4sIII", raw, i * 16) for i in range(n_head)]

    def bad(entries):
        return any(not (_avi.CHUNK_ID_RE.fullmatch(cid) or cid == b"rec ") or length > _avi.MAX_CHUNK_BYTES
                   for cid, _f, _o, length in entries)
    if bad(head_entries):
        return None
    if n_total > n_head:
        n_sample = _avi.BASE_SAMPLE_ENTRIES
        picks = sorted({round(i * (n_total - 1) / (n_sample - 1)) for i in range(n_sample)} - set(range(n_head)))
        extra = []
        for i in picks:
            raw_i = _read_at(f, pos + 8 + i * 16, 16)
            if len(raw_i) < 16:
                return None
            extra.append(struct.unpack("<4sIII", raw_i))
        if bad(extra):
            return None
        sample = head_entries + extra
    else:
        sample = head_entries
    movi = _find_bytes(f, b"movi", 0, pos)
    bases = [0]
    if movi >= 0:
        bases += [movi, movi + 4]
    inferred = _infer_base(f, size, sample)
    if inferred is not None and inferred not in bases:
        bases.append(inferred)
    # 엔진과 같은 규칙: 첫 청크 사슬에 엔트리가 닿는다고 보고 역산한 base 후보 추가(앞부분 손상 파일).
    if chain_start is not None:
        for e in head_entries:
            cand = chain_start - _avi._entry_tuple(e)[2]
            if cand >= 0 and cand not in bases:
                bases.append(cand)
    view = _FileView(f, size)
    best = None
    for base in bases:
        # 표본이 idx1 뒤를 가리키는 base는 옛 녹화 것(현재 색인은 자기 movi 뒤에 있다).
        if any(base + _avi._entry_tuple(e)[2] >= pos for e in sample):
            continue
        states = [_avi._entry_target_state(view, base, e) for e in sample]
        decided = [st for st in states if st in ("match", "mismatch")]
        hits = decided.count("match")
        if hits == 0 or hits * 2 < len(decided):
            continue
        lead = decided[:8]
        if lead.count("match") * 2 < len(lead):
            continue
        if not _index_reaches_chain(f, size, base, sample, chain_start):
            continue
        if best is None or hits > best[1]:
            best = (base, hits)
    return best[0] if best else None


def _find_idx1(f, size: int, chain_start: Optional[int] = None, cancel_event=None) -> Optional[Tuple[int, int]]:
    """idx1 바이트 스캔(rfind) fallback(엔진 find_idx1_fallback): 현재 녹화의 것만 받는다. (위치, 쓸 수 있는 크기)."""
    if chain_start is None:
        chain_start = first_chunk_chain(f, size, cancel_event=cancel_event)
    # 엔진 find_idx1_fallback과 같은 규칙: 검증을 통과한 후보 가운데 자기 영역 뒤에 있는 가장 앞의 idx1.
    candidates = []
    for pos in _rfind_iter(f, b"idx1", 0, size, cancel_event):
        if pos + 8 > size or (chain_start is not None and pos <= chain_start):
            continue
        idx_size = struct.unpack("<I", _read_at(f, pos + 4, 4))[0]
        usable = None
        if idx_size % 16 == 0 and pos + 8 + idx_size <= size:
            usable = idx_size
        elif idx_size % 16 == 0 and idx_size > 0 and pos + 8 < size and pos + 8 + idx_size > size:
            usable = ((size - pos - 8) // 16) * 16
        if usable:
            base = _idx1_plausible(f, size, pos, usable, chain_start)
            if base is not None:
                candidates.append((pos, usable, _index_region_end(f, base, pos, usable)))
    if not candidates:
        return None
    after = [c for c in candidates if c[2] <= c[0]]
    pos, usable, _end = min(after or candidates, key=lambda c: c[0])
    return pos, usable


def _index_region_end(f, base: int, pos: int, size: int) -> int:
    """엔진 _index_region_end: 표본 엔트리(마지막 포함)가 가리키는 가장 뒤 청크의 끝."""
    n = size // 16
    picks = {n - 1} | {round(i * (n - 1) / 7) for i in range(8)} if n > 0 else set()
    end = 0
    for i in picks:
        e = struct.unpack("<4sIII", _read_at(f, pos + 8 + i * 16, 16))
        end = max(end, base + e[2] + 8 + e[3])
    return end


def avi_sections(f, size: int, cancel_event=None):
    """(hdrl, movi, idx1, avix_count, riff_truncated). 엔진 find_top_level_sections와 같은 규칙을 read()로.
    idx1은 현재 movi의 첫 청크들과 맞물리는 것만 받는다(사전 할당 파일 뒤쪽 옛 idx1 제외)."""
    head = _read_at(f, 0, 12)
    if len(head) < 12 or head[:4] != b"RIFF":
        return _avi_sections_damaged(f, size, cancel_event)
    hdrl = movi = idx1 = None
    avix = 0
    riff_truncated = False
    pos = 0
    while pos + 8 <= size:
        _check_cancel(cancel_event)
        ck = _read_at(f, pos, 12)
        if len(ck) < 8:
            break
        cid = ck[:4]
        ck_size = struct.unpack("<I", ck[4:8])[0]
        data_end = pos + 8 + ck_size
        truncated = False
        if data_end > size:
            data_end = size
            truncated = True
        if cid == b"RIFF" and len(ck) >= 12:
            form = ck[8:12]
            if form == b"AVIX":
                avix += 1
            elif form == b"AVI ":
                if truncated:
                    riff_truncated = True
                cpos = pos + 12
                movi_here = False
                while cpos + 8 <= data_end:
                    ch = _read_at(f, cpos, 12)
                    if len(ch) < 8:
                        break
                    ch_id = ch[:4]
                    ch_size = struct.unpack("<I", ch[4:8])[0]
                    ch_end = cpos + 8 + ch_size
                    ch_trunc = False
                    if ch_end > data_end:
                        if not truncated:
                            break
                        ch_end = data_end
                        ch_trunc = True
                    if ch_id == b"LIST" and len(ch) >= 12:
                        ltype = ch[8:12]
                        sec = AviSection(pos=cpos, data_start=cpos + 8, data_end=ch_end, ck_size=ch_size, truncated=ch_trunc)
                        if ltype == b"hdrl" and hdrl is None:
                            hdrl = sec
                        elif ltype == b"movi" and not movi_here:
                            if movi is None:
                                movi = sec
                            movi_here = True
                    elif ch_id == b"idx1" and idx1 is None:
                        idx1 = AviSection(pos=cpos, data_start=cpos + 8, data_end=ch_end, ck_size=ch_size, truncated=ch_trunc)
                    if ch_trunc:
                        break
                    cpos = ch_end + (ch_size & 1)
        if truncated:
            break
        pos = data_end + (ck_size & 1)
    if idx1 is not None and movi is not None:
        usable = min(idx1.ck_size, max(0, size - idx1.data_start)) // 16 * 16
        base = _idx1_plausible(f, size, idx1.pos, usable, chain_start=movi.data_start + 4) if usable else None
        if base is None:
            idx1 = None
    return hdrl, movi, idx1, avix, riff_truncated


def _avi_sections_damaged(f, size: int, cancel_event=None):
    """RIFF 머리가 없는(앞부분 손상) 파일(엔진 find_top_level_sections_damaged): 기준점은 현재 녹화의 첫 청크
    사슬. hdrl은 movi보다 앞의 것만, idx1은 사슬과 맞물리는 것만."""
    chain = first_chunk_chain(f, size, cancel_event=cancel_event)
    idx1 = None
    base = None
    fb = _find_idx1(f, size, chain_start=chain, cancel_event=cancel_event)
    if fb is not None:
        pos, idx_size = fb
        idx1 = AviSection(pos=pos, data_start=pos + 8, data_end=min(pos + 8 + idx_size, size), ck_size=idx_size)
        base = _idx1_plausible(f, size, pos, idx_size, chain_start=chain)
    movi = None
    if base is not None and base > 0:
        # 'LIST....movi' 머리가 0으로 덮였어도 idx1 엔트리가 가리키는 기준점이 movi FourCC 위치다.
        content_start = chain if (chain is not None and base <= chain <= base + 8) else base + 4
        movi = AviSection(pos=base - 8, data_start=content_start - 4, data_end=idx1.pos, ck_size=idx1.pos - base - 4)
    elif chain is not None:
        movi_end = idx1.pos if (idx1 is not None and idx1.pos > chain) else size
        head = _read_at(f, chain - 12, 12) if chain >= 12 else b""
        if len(head) == 12 and head[:4] == b"LIST" and head[8:12] == b"movi":
            ck_size = struct.unpack("<I", head[4:8])[0]
            declared_end = chain - 4 + ck_size
            if 4 <= ck_size and declared_end <= movi_end:
                movi_end = declared_end
            movi = AviSection(pos=chain - 12, data_start=chain - 4, data_end=movi_end, ck_size=ck_size,
                              truncated=declared_end > size)
        else:
            # movi 머리까지 지워진 파일: 첫 청크 사슬 바로 앞을 가상 FourCC 위치로 둔다(색인 재구성용 base).
            movi = AviSection(pos=chain - 12, data_start=chain - 4, data_end=movi_end, ck_size=movi_end - (chain - 4))
    hdrl_limit = movi.pos if movi is not None else _HDRL_SCAN_LIMIT
    hdrl = _scan_list(f, size, b"hdrl", 0, max(hdrl_limit, 0), cancel_event) if hdrl_limit > 0 else None
    return hdrl, movi, idx1, 0, False


def parse_idx1(f, idx1_pos: int, idx_size: int) -> list:
    """idx1 엔트리(엔진 parse_idx1 형식의 dict 목록)."""
    raw = _read_at(f, idx1_pos, idx_size)
    return _avi.parse_idx1(raw, 0, len(raw))


def avi_header_streams(f, hdrl: AviSection):
    """hdrl을 실제로 파싱한다. (avih 있음?, 스트림 목록[StreamInfo]) - 헤더 내용 손상 판정과 참조 대조용."""
    length = min(hdrl.data_end - hdrl.pos, _BASE_INFER_LIMIT)
    buf = _read_at(f, hdrl.pos, length)
    return avi_header_streams_from_bytes(buf)


def avi_header_streams_from_bytes(buf: bytes):
    """buf가 'LIST....hdrl'로 시작하는 바이트일 때 (avih 있음?, 스트림 목록)."""
    if len(buf) < 12 or buf[:4] != b"LIST" or buf[8:12] != b"hdrl":
        return False, []
    chunk = _avi.ChunkInfo(ck_id=b"LIST", ck_size=len(buf) - 8, pos=0, data_start=8, data_end=len(buf),
                           is_list=True, list_type=b"hdrl", content_start=12, content_end=len(buf))
    has_avih = any(c.ck_id == b"avih" and not c.is_list and c.ck_size >= 28
                   for c in _avi.iter_chunks(buf, 12, len(buf)))
    _dw, streams = _avi.parse_hdrl(buf, chunk)
    return has_avih, streams


def _avi_header_ok(f, hdrl: AviSection) -> bool:
    """avih가 있고 규격 스트림(vids/auds/txts…)이 하나라도 파싱되면 헤더 내용이 살아 있는 것으로 본다."""
    try:
        has_avih, streams = avi_header_streams(f, hdrl)
    except (struct.error, ValueError):
        return False
    return has_avih and any(s.fcc_type in _AVI_STREAM_TYPES for s in streams)


_RMC_UTC_RE = re.compile(rb"\$[A-Z]{2}RMC,(\d{2})(\d{2})(\d{2}(?:\.\d+)?),")


def chunk_rmc_utc(f, pos: int, size: int) -> Optional[float]:
    """청크 페이로드 안의 첫 RMC UTC(초). 작은(≤4 KB) 텍스트 청크에만 쓴다."""
    payload = _read_at(f, pos + 8, min(size, 4096))
    m = _RMC_UTC_RE.search(payload)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _rmc_anchor(f, pos: int, size: int):
    """청크 안의 근거 RMC(체크섬 OK·A 상태·날짜 있음): (pos, 절대초). 판정은 엔진 _chunk_rmc_anchor를 페이로드
    바이트 위에서 그대로 쓴다(페이로드만 넘기므로 pos=-8)."""
    payload = _read_at(f, pos + 8, size)
    hit = _avi._chunk_rmc_anchor(payload, -8, size)
    return (pos, hit[1]) if hit is not None else None


def _last_anchor(f, chunks, limit=None):
    limit = _avi.ANCHOR_SCAN_CHUNKS if limit is None else limit
    for pos, _cid, size in reversed(chunks[-limit:]):
        if size <= _avi.ANCHOR_MAX_CHUNK_BYTES:
            a = _rmc_anchor(f, pos, size)
            if a is not None:
                return a
    return None


def _first_anchor(f, chunks, limit=None):
    limit = _avi.ANCHOR_SCAN_CHUNKS if limit is None else limit
    for pos, _cid, size in chunks[:limit]:
        if size <= _avi.ANCHOR_MAX_CHUNK_BYTES:
            a = _rmc_anchor(f, pos, size)
            if a is not None:
                return a
    return None


def _bytes_per_second(f, chunks, window=2 * 1024 * 1024):
    """손상 구간 직전 window 바이트 안의 앵커 두 개로 기록 속도(바이트/초)(엔진 _bytes_per_second)."""
    if not chunks:
        return None
    g0 = chunks[-1][0] + 8 + chunks[-1][2]
    recent = [c for c in chunks[-_avi.ANCHOR_SCAN_CHUNKS:] if c[0] >= g0 - window]
    last = _last_anchor(f, recent)
    first = _first_anchor(f, recent)
    if last is None or first is None or last[1] - first[1] < 5:
        return None
    return (last[0] - first[0]) / (last[1] - first[1])


def recording_cut_at(f, size: int, walked, gaps, info: Optional[dict] = None) -> Optional[int]:
    """손상 구간 앞뒤의 GPS 연속성으로 녹화 끝(옛 녹화 잔재 시작)을 정한다(엔진 recording_cut_at과 같은 규칙):
    근거는 체크섬 OK·A 상태·날짜 있는 RMC. 뒤가 과거면 경계, 손상 바이트로 추정한 경과의 3배+600초보다 더
    뛰면 경계, 근거가 없으면 0으로 덮인 구간은 같은 녹화·아니면 '경계 불확실'로 자른다."""
    view = _FileView(f, size)
    for g0, g1 in sorted(gaps):
        before_chunks = [w for w in walked if w[0] < g0]
        after_chunks = [w for w in walked if w[0] >= g1]
        before = _last_anchor(f, before_chunks)
        after = _first_anchor(f, after_chunks)
        if before is not None and after is not None:
            du = after[1] - before[1]
            rate = _bytes_per_second(f, before_chunks)
            expected = (g1 - g0) / rate if rate else None
            allow = (expected * 3 + 600) if expected is not None else 600
            if du < -1:
                reason = f"손상 구간 0x{g0:X} 뒤의 GPS 시각이 과거(앞 {_avi._fmt_anchor(before)} → 뒤 {_avi._fmt_anchor(after)})"
            elif du > allow:
                reason = (f"손상 구간 0x{g0:X} 뒤의 GPS 시각이 손상 길이로 설명되지 않게 뜀"
                          f"(앞 {_avi._fmt_anchor(before)} → 뒤 {_avi._fmt_anchor(after)}, {du:.0f}초"
                          + (f", 손상 바이트로 추정한 경과 {expected:.0f}초" if expected is not None else "") + ")")
            else:
                continue
            if info is not None:
                info.update({"reason": reason, "uncertain": False, "before": before, "after": after})
            return g0
        if _avi._is_zero_region(view, g0, min(g1, g0 + _avi.ZERO_GAP_PROBE)) \
                and _avi._zero_fraction(view, g0, g1) >= _avi.ZERO_GAP_MIN_FRACTION:
            continue
        reason = (f"손상 구간 0x{g0:X} 앞뒤에 날짜 있는 A 상태 RMC가 없어 녹화 경계를 확인할 수 없음"
                  f"(앞 {_avi._fmt_anchor(before)}, 뒤 {_avi._fmt_anchor(after)})")
        if info is not None:
            info.update({"reason": reason, "uncertain": True, "before": before, "after": after})
        return g0
    return None


def _scan_avi(path: str, report: DamageReport, cancel_event=None) -> None:
    size = report.size
    with open(path, "rb") as f:
        hdrl, movi, idx1, _avix, riff_truncated = avi_sections(f, size, cancel_event)
        if hdrl is None:
            report.header_missing = True
        elif not _avi_header_ok(f, hdrl):
            report.header_missing = True
            report.header_damaged = True
        if _read_at(f, 0, 4) == b"RIFF":
            declared = struct.unpack("<I", _read_at(f, 4, 4))[0] + 8
            if declared > size + 1 or riff_truncated:
                report.truncated = True
                report.reasons.append(f"RIFF 선언 크기 {declared:,}바이트 > 실제 {size:,}바이트")
        if movi is None:
            pos = _find_bytes(f, b"movi", 0, size, cancel_event)
            if pos < 0:
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
        fb = None
        if idx1 is None:
            fb = _find_idx1(f, size, chain_start=(movi.data_start + 4 if movi is not None else None),
                            cancel_event=cancel_event)
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
            if idx1.truncated:
                report.truncated = True
        report.avi_movi_end = movi_end
        # 청크를 걸어 읽을 수 없는 구간을 찾는다(엔진 walk). 0 꼬리·잘린 마지막 청크는 손상이 아니라 tail로 받는다.
        gaps: List[tuple] = []
        tail: dict = {}
        walked = []
        junk: List[Tuple[int, int]] = []
        for pos, cid, csize in walk_movi_chunks(f, fourcc + 4, movi_end, gaps, cancel_event, tail):
            if cid == b"JUNK":
                junk.append((pos + 8, pos + 8 + csize))   # 정상 패딩 청크 - 0 구간 검사에서 뺀다(리뷰 #84)
                continue
            walked.append((pos, cid, csize))
        gaps = sorted(gaps)
        walked_end = (walked[-1][0] + 8 + walked[-1][2] + (walked[-1][2] & 1)) if walked else fourcc + 4
        if tail.get("truncated_tail") is not None:
            report.truncated = True
            if not any("잘림" in r for r in report.reasons):
                report.reasons.append(f"마지막 청크가 파일 끝에서 잘림(0x{tail['truncated_tail']:X})")
        if report.index_missing:
            cut_info: dict = {}
            cut = recording_cut_at(f, size, walked, gaps, cut_info)
            if cut is not None:
                gaps = [g for g in gaps if g[0] < cut]
                movi_end = cut
                basis = "movi 청크 걷기 (GPS 연속성으로 끝 판정" + (" - 경계 불확실" if cut_info.get("uncertain") else "") + ")"
                report.reasons.append(cut_info.get("reason", ""))
            elif walked:
                movi_end = min(movi_end, walked_end)
        else:
            # idx1이 있으면 참조된 마지막 청크 끝까지가 현재 녹화다(사전 할당 파일의 뒤쪽 잔재 제외).
            # 엔트리 자체가 규격이면 가리키는 청크가 깨졌어도 녹화 범위에 넣는다(리뷰 #26). 엔트리가 깨진
            # (0·쓰레기) idx1은 색인 손상이다. 'rec ' 그룹 엔트리는 정상 구조다(리뷰 #16).
            entries = parse_idx1(f, (idx1.data_start if idx1 else fb[0] + 8), (idx1.ck_size if idx1 else fb[1]))
            base = _infer_base(f, size, entries) if entries else None
            if base is None:
                base = fourcc
            last_end = 0
            invalid = 0
            for e in entries:
                cid = e["chunk_id"]
                if cid in (b"idx1", b"rec ", b"LIST"):
                    continue
                stop = base + e["idx_offset"] + 8 + e["length"]
                if not _avi.CHUNK_ID_RE.fullmatch(cid) or e["length"] > _avi.MAX_CHUNK_BYTES \
                        or stop > movi_end or base + e["idx_offset"] < fourcc:
                    invalid += 1
                    continue
                last_end = max(last_end, stop)
            if entries and invalid:
                report.index_damaged = True
                report.reasons.append(f"idx1 엔트리 {invalid:,}개 손상(전체 {len(entries):,}개)")
            if last_end:
                rec_end = last_end
                if report.index_damaged and walked_end > last_end + 1:
                    # 색인이 가리키지 못하는 뒷부분: 옛 녹화 잔재일 수도 있으니 GPS 연속성으로 끝을 다시 잡는다.
                    cut = recording_cut_at(f, size, walked, [(last_end, walked_end)])
                    unref_end = cut if cut is not None else walked_end
                    if unref_end > last_end + 1:
                        gaps = sorted(gaps + [(last_end, unref_end)])
                        rec_end = unref_end
                        basis = "idx1 (손상 - 걸어 읽은 끝까지)"
                # 색인이 가리키는 범위 안의 0 꼬리는 미사용 할당이 아니라 덮어쓰기 손상이다(리뷰 #26).
                zt = tail.get("zero_tail")
                if zt and zt[0] < rec_end:
                    gaps = sorted(gaps + [(zt[0], min(zt[1], rec_end))])
                # 녹화 끝에서 시작하는 gap은 뒤쪽 잔재(옛 녹화)의 머리를 못 읽은 것이지 손상이 아니다.
                gaps = [g for g in gaps if g[0] < rec_end]
                movi_end = min(movi_end, rec_end + 1)
        report.gaps = [[a, min(b, movi_end)] for a, b in gaps if a < movi_end]
        report.media_range = [fourcc + 4, movi_end]
        report.media_end_basis = basis
    zeros = _zero_runs(path, fourcc + 4, movi_end, cancel_event)
    report.zero_regions = _subtract_ranges(zeros, [(a, min(b, movi_end)) for a, b in junk if a < movi_end], ZERO_RUN_MIN)
    if report.header_missing:
        report.reasons.append("hdrl(스트림 헤더) 내용 손상" if report.header_damaged else "hdrl(스트림 헤더) 없음")
    if report.index_missing:
        report.reasons.append("idx1(색인) 없음")


# ---------------------------------------------------------------------------
# MP4
# ---------------------------------------------------------------------------

def _mp4_moov_ok(f, moov, filesize: int, fragmented: bool) -> bool:
    """moov 내용이 살아 있는가: 자식 Box가 끝까지 읽히고, 트랙이 하나라도 파싱되며, 일반 MP4면 샘플 표
    (stco/co64·stsz)의 개수가 Box 안에 들어가고 chunk offset이 파일 안을 가리켜야 한다(리뷰 #28)."""
    try:
        children = list(_mp4.iter_child_boxes(f, moov.payload_start, moov.end, context="damage-moov"))
        if not children or children[-1].end < moov.end - 8:
            return False
        traks = _mp4.find_all(children, b"trak")
        if not traks:
            return False
        good_track = False
        for trak in traks:
            track_id, _handler, _name, timescale, _stsd = _mp4.parse_trak(f, trak)
            if track_id is None or not timescale:
                continue
            tchildren = list(_mp4.iter_child_boxes(f, trak.payload_start, trak.end, context="damage-trak"))
            mdia = _mp4.find_box(tchildren, b"mdia")
            if mdia is None:
                return False
            mchildren = list(_mp4.iter_child_boxes(f, mdia.payload_start, mdia.end, context="damage-mdia"))
            minf = _mp4.find_box(mchildren, b"minf")
            if minf is None:
                return False
            ichildren = list(_mp4.iter_child_boxes(f, minf.payload_start, minf.end, context="damage-minf"))
            stbl = _mp4.find_box(ichildren, b"stbl")
            if stbl is None:
                return False
            schildren = list(_mp4.iter_child_boxes(f, stbl.payload_start, stbl.end, context="damage-stbl"))
            if not schildren or schildren[-1].end < stbl.end - 8:
                return False
            if not fragmented and not _mp4_sample_table_ok(f, schildren, filesize):
                return False
            good_track = True
        return good_track
    except (OSError, struct.error, ValueError):
        return False


def _mp4_sample_table_ok(f, stbl_children, filesize: int) -> bool:
    for box in stbl_children:
        if box.box_type in (b"stco", b"co64"):
            payload = _mp4.read_payload(f, box)
            if len(payload) < 8:
                return False
            count = struct.unpack(">I", payload[4:8])[0]
            width = 4 if box.box_type == b"stco" else 8
            if 8 + count * width > len(payload):
                return False
            fmt = ">I" if width == 4 else ">Q"
            for i in range(count):
                off = struct.unpack_from(fmt, payload, 8 + i * width)[0]
                if off == 0 or off >= filesize:
                    return False
        elif box.box_type == b"stsz":
            payload = _mp4.read_payload(f, box)
            if len(payload) < 12:
                return False
            sample_size, count = struct.unpack(">II", payload[4:12])
            if sample_size == 0 and 12 + count * 4 > len(payload):
                return False
    return True


def _mdat_at(f, pos: int, filesize: int):
    """pos에 mdat 머리가 있으면 (payload_start, declared_end 또는 None(자리표시값·비정상)). 없으면 None.
    기록 중 전원이 끊긴 파일은 size 자리에 0·8·1+largesize 0·'????' 같은 자리표시값이 남는다(리뷰 #18)."""
    head = _read_at(f, pos, 16)
    if len(head) < 8 or head[4:8] != b"mdat":
        return None
    size32 = struct.unpack(">I", head[:4])[0]
    if size32 == 1 and len(head) >= 16:
        large = struct.unpack(">Q", head[8:16])[0]
        if 16 <= large and pos + large <= filesize:
            return pos + 16, pos + large
        return pos + 16, None
    if size32 >= 16 and pos + size32 <= filesize:
        return pos + 8, pos + size32
    return pos + 8, None


def _scan_mp4(path: str, report: DamageReport, cancel_event=None) -> None:
    size = report.size
    with open(path, "rb") as f:
        boxes, boundary, kind = _mp4.scan_top_level_bounded(
            f, size, cancel_check=(cancel_event.is_set if cancel_event is not None else None))
        gaps = [list(g) for g in _mp4.DAMAGE_GAPS]
        types = [b.box_type for b in boxes]
        if kind in _MP4_SLACK_BOUNDARIES:
            # 현재 녹화 뒤에 이어붙은 옛 녹화(슬랙) 경계 - 손상이 아니다. 어떤 근거로 잘랐는지만 남긴다.
            report.reasons.append(f"현재 녹화 끝 0x{boundary:X} - 뒤는 옛 녹화(슬랙): {_MP4_SLACK_BOUNDARIES[kind]}")
        elif kind == "corrupt" and gaps and boxes and gaps[-1][0] >= boxes[-1].end and gaps[-1][1] >= size:
            # 마지막 Box 뒤 ~ 파일 끝이 재동기화도 안 되는 0이 아닌 잔재: 옛 녹화 흔적일 수 있어 따로 적는다.
            report.tail_residue = [int(gaps[-1][0]), int(size)]
        report.fragmented = b"moof" in types
        moov = next((b for b in boxes if b.box_type == b"moov"), None)
        moov_ok = moov is not None and _mp4_moov_ok(f, moov, size, report.fragmented)
        report.header_missing = not moov_ok
        report.header_damaged = moov is not None and not moov_ok
        report.index_missing = report.header_missing and not report.fragmented
        report.truncated = kind == "truncated"
        if kind == "truncated":
            report.reasons.append("마지막 Box가 파일 끝에서 잘림")
        mdats = [b for b in boxes if b.box_type == b"mdat" and b.end > b.payload_start]
        moofs = [b for b in boxes if b.box_type == b"moof"]
        if moofs:
            report.mp4_first_moof = moofs[0].start
        media_boxes = [b for b in boxes if b.box_type in (b"mdat", b"moof")]
        if media_boxes:
            report.mp4_media_start = media_boxes[0].start
        placeholder_note = ""
        if mdats:
            media_start = mdats[0].payload_start
            media_end = min(max(b.end for b in mdats), size)
            report.media_end_basis = "Box 사슬" + (f" (경계: {_MP4_SLACK_BOUNDARIES[kind]})" if kind in _MP4_SLACK_BOUNDARIES
                                                 else " (끝까지 0인 꼬리 제외)" if kind == "padding" else "")
        else:
            # 실제 페이로드가 있는 mdat가 없다: 빈 mdat(size 8)·자리표시 크기·머리 손상·잘린 mdat·0으로 덮인
            # mdat 머리. 마지막 정상 Box 끝(또는 mdat 머리 뒤)부터 다음 정상 Box(없으면 파일 끝)까지를 미디어로
            # 본다. 예전엔 범위가 비거나 '복구 불가'가 되어 분석이 막혔다(리뷰 #18, #81).
            media_start = None
            box_start = None
            empty = [b for b in boxes if b.box_type == b"mdat"]
            if boundary is not None:
                found = _mdat_at(f, boundary, size)
                if found is not None:
                    media_start, box_start = found[0], boundary
                    placeholder_note = ("mdat 크기가 자리표시값(기록 중 중단)" if kind != "truncated"
                                        else "mdat가 파일 끝에서 잘림")
            if media_start is None and empty:
                media_start, box_start = empty[0].end, empty[0].start
                placeholder_note = "mdat 크기가 자리표시값(size 8)"
            if media_start is None and gaps:
                media_start = box_start = gaps[0][0]
                placeholder_note = "mdat 머리를 읽을 수 없음"
            if media_start is None and boundary is not None and kind in ("corrupt", "padding"):
                # padding: 엔진은 끝까지 0인 꼬리를 사전 할당 잔여로 보지만, mdat가 하나도 없으면 그 자리가 미디어
                # (0으로 덮인 mdat)다. 0 구간 검사가 손상으로 잡는다.
                media_start = box_start = boundary
                placeholder_note = "mdat 머리를 읽을 수 없음"
            if media_start is None:
                report.recoverable = False
                report.size_known = False
                report.reasons.append("mdat(영상 데이터 Box)를 찾을 수 없음")
                return
            later = [b.start for b in boxes if b.start >= media_start and b.box_type != b"mdat"]
            media_end = min(later) if later else size
            if media_end <= media_start:
                media_end = size
            report.media_end_basis = ("moov 샘플 표" if moov_ok and not report.fragmented
                                      else "마지막 정상 Box 끝 ~ 다음 Box(없으면 파일 끝)")
            if report.mp4_media_start is None or box_start < report.mp4_media_start:
                report.mp4_media_start = box_start
            report.reasons.append(placeholder_note + f" - 미디어 범위를 {media_start:,}~{media_end:,}바이트로 잡음")
            # 미디어가 통째로 '읽을 수 없는 구간'으로 잡혀 있으면 그건 구조 손상이 아니라 색인 없는 mdat 본문이다.
            gaps = [g for g in gaps if not (g[0] <= media_start and g[1] >= media_end)]
        if media_end <= media_start:
            report.recoverable = False
            report.size_known = False
            report.reasons.append("미디어 영역이 비어 있음")
            return
        report.media_range = [media_start, media_end]
        if moov_ok and not report.fragmented:
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
        what = "moov(헤더) 내용 손상" if report.header_damaged else "moov(헤더) 없음"
        report.reasons.append(what + (" - 조각(moof) 구조" if report.fragmented else " - 샘플 표 소실"))


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
