"""헤더 이식 - 헤더(AVI hdrl / fMP4 moov)가 사라진 손상 파일에 같은 기기 정상 영상의 헤더를 붙인 작업
사본을 만든다. 원본·분석 사본은 손대지 않고 `source/<이름>_repaired.<확장자>`를 따로 만든다.

참조 영상에서는 설정(스트림 구성·코덱·해상도·FPS)만 가져오고 영상·GPS 데이터는 손상 파일 것만 쓴다.
사용자 결정(2026-10-11): 같은 폴더에서 같은 기기의 정상 영상을 자동으로 찾고, 없으면 사용자에게 묻는다.
"""
from __future__ import annotations

import mmap
import os
import re
import struct
from typing import List, Optional, Tuple

from core.damage import DamageReport
from core.paths import ensure_vendor_importable

ensure_vendor_importable()

import integration_avi as _avi  # noqa: E402
import integration_blackbox as _blackbox  # noqa: E402
import integration_mp4 as _mp4  # noqa: E402

_NAME_PREFIX_RE = re.compile(r"^[A-Za-z_]+")


def _name_prefix(path: str) -> str:
    m = _NAME_PREFIX_RE.match(os.path.basename(path))
    return (m.group(0) if m else "").upper()


def _digits(path: str) -> int:
    digits = re.sub(r"\D", "", os.path.basename(path))
    return int(digits[:14] or "0")


def _reference_ok(path: str, container: str, fragmented: bool) -> bool:
    try:
        found, _note = _blackbox.detect_container(path)
        if found != container:
            return False
        if container == _blackbox.CONTAINER_AVI:
            with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                if bytes(mm[0:4]) != b"RIFF":
                    return False
                hdrl, movi, idx1, _ = _avi.find_top_level_sections(mm)
                return hdrl is not None and bool(movi) and idx1 is not None
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            boxes, _b, kind = _mp4.scan_top_level_bounded(f, size)
        types = [b.box_type for b in boxes]
        if b"moov" not in types or kind == "corrupt" or _mp4.DAMAGE_GAPS:
            return False
        return (b"moof" in types) == fragmented
    except (OSError, ValueError, struct.error):
        return False
    finally:
        _avi.WARNINGS.clear()
        _mp4.WARNINGS.clear()


def find_reference_candidates(path: str, report: DamageReport, limit: int = 5) -> List[str]:
    """같은 폴더에서 같은 확장자·같은 이름 접두(REC_/EVT_ 등)·구조가 정상인 파일. 파일명 숫자(녹화 시각)가
    가까운 순. 손상 파일 자신은 제외."""
    folder = os.path.dirname(os.path.abspath(path))
    ext = os.path.splitext(path)[1].casefold()
    prefix = _name_prefix(path)
    me = os.path.normcase(os.path.abspath(path))
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    candidates = []
    for name in sorted(names):
        full = os.path.join(folder, name)
        if os.path.normcase(os.path.abspath(full)) == me or not os.path.isfile(full):
            continue
        if os.path.splitext(name)[1].casefold() != ext or os.path.getsize(full) == 0:
            continue
        if prefix and _name_prefix(full) != prefix:
            continue
        candidates.append(full)
    candidates.sort(key=lambda p: abs(_digits(p) - _digits(path)))
    out = []
    for cand in candidates:
        if _reference_ok(cand, report.container, report.fragmented):
            out.append(cand)
            if len(out) >= limit:
                break
    return out


def _avi_chunk_ids(mm, start: int, end: int, limit: int = 300) -> set:
    ids = set()
    gaps: list = []
    for n, (_pos, cid, _size) in enumerate(_avi.walk_movi_chunks(mm, start, end, gaps)):
        ids.add(cid)
        if n >= limit:
            break
    return ids


def validate_reference(report: DamageReport, reference: str) -> Tuple[bool, str]:
    """참조가 손상 파일과 같은 구성인지. AVI: 참조 idx1의 청크 ID 집합이 손상 파일 movi의 청크 ID와 같아야
    한다. fMP4: 손상 파일 moof의 track_ID들이 참조 moov의 trak ID에 모두 있어야 한다."""
    try:
        if report.container == _blackbox.CONTAINER_AVI:
            with open(reference, "rb") as rf, mmap.mmap(rf.fileno(), 0, access=mmap.ACCESS_READ) as rm:
                _h, _m, idx1, _ = _avi.find_top_level_sections(rm)
                if idx1 is None:
                    return False, "참조 영상에 idx1이 없습니다."
                entries = _avi.parse_idx1(rm, idx1.data_start, min(idx1.ck_size, 16 * 2000))
                ref_ids = {e["chunk_id"] for e in entries}
            with open(report.path, "rb") as df, mmap.mmap(df.fileno(), 0, access=mmap.ACCESS_READ) as dm:
                start = (report.avi_movi_fourcc or 0) + 4
                end = report.media_range[1] if report.media_range else len(dm)
                dmg_ids = _avi_chunk_ids(dm, start, end)
            if not dmg_ids:
                return False, "손상 파일에서 청크를 읽을 수 없습니다."
            if ref_ids != dmg_ids:
                return False, (f"스트림 구성이 다릅니다 (참조 {sorted(i.decode('ascii', 'replace') for i in ref_ids)}, "
                               f"손상 파일 {sorted(i.decode('ascii', 'replace') for i in dmg_ids)})")
            return True, "스트림 구성 일치"
        if not report.fragmented:
            return False, "moov가 끝에 있는 일반 MP4는 참조 헤더를 이식할 수 없습니다(샘플 표가 파일마다 다름)."
        size = os.path.getsize(reference)
        with open(reference, "rb") as rf:
            boxes, _b, _k = _mp4.scan_top_level_bounded(rf, size)
            moov = next((b for b in boxes if b.box_type == b"moov"), None)
            if moov is None:
                return False, "참조 영상에 moov가 없습니다."
            _text, _trex, all_tracks = _mp4.parse_moov(rf, moov)
            ref_ids = {(t.get("track_id") if isinstance(t, dict) else getattr(t, "track_id", None))
                       for t in all_tracks}
            ref_ids.discard(None)
        dmg_ids = _moof_track_ids(report.path, report.mp4_first_moof)
        if not dmg_ids:
            return False, "손상 파일의 moof에서 track_ID를 읽을 수 없습니다."
        if not dmg_ids <= ref_ids:
            return False, f"트랙 구성이 다릅니다 (참조 {sorted(ref_ids)}, 손상 파일 {sorted(dmg_ids)})"
        return True, "트랙 구성 일치"
    except (OSError, ValueError, struct.error, AttributeError) as exc:
        return False, f"참조 검사 실패: {exc}"
    finally:
        _avi.WARNINGS.clear()
        _mp4.WARNINGS.clear()


def _moof_track_ids(path: str, first_moof: Optional[int], count: int = 3) -> set:
    ids = set()
    if first_moof is None:
        return ids
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = first_moof
        seen = 0
        while pos + 8 <= size and seen < count:
            f.seek(pos)
            hdr = f.read(8)
            bsize = struct.unpack(">I", hdr[:4])[0]
            if bsize < 8 or hdr[4:8] != b"moof":
                break
            f.seek(pos)
            payload = f.read(min(bsize, 1 << 20))
            for m in re.finditer(rb"tfhd", payload):
                # tfhd: version/flags(4) + track_ID(4)
                p = m.end() + 4
                if p + 4 <= len(payload):
                    ids.add(struct.unpack(">I", payload[p:p + 4])[0])
            seen += 1
            pos += bsize
    return ids


def transplant_header(report: DamageReport, reference: str, out_path: str) -> str:
    """참조 헤더 + 손상 파일 본문으로 작업 사본을 만든다. 성공하면 설명 문자열을 돌려주고, 못 하면 ValueError."""
    ok, reason = validate_reference(report, reference)
    if not ok:
        raise ValueError(reason)
    if os.path.exists(out_path):
        raise FileExistsError(out_path)
    if report.container == _blackbox.CONTAINER_AVI:
        return _transplant_avi(report, reference, out_path)
    return _transplant_fmp4(report, reference, out_path)


def _transplant_avi(report: DamageReport, reference: str, out_path: str) -> str:
    with open(reference, "rb") as rf, mmap.mmap(rf.fileno(), 0, access=mmap.ACCESS_READ) as rm:
        _h, movi_list, _i, _ = _avi.find_top_level_sections(rm)
        head = bytes(rm[0:movi_list[0].pos])
    with open(report.path, "rb") as df, mmap.mmap(df.fileno(), 0, access=mmap.ACCESS_READ) as dm:
        fourcc = report.avi_movi_fourcc or 0
        movi_end = report.media_range[1] if report.media_range else len(dm)
        body = bytes(dm[fourcc + 4:movi_end])
        tail = b""
        if report.avi_idx1_pos is not None:
            idx_size = struct.unpack_from("<I", dm, report.avi_idx1_pos + 4)[0]
            tail = bytes(dm[report.avi_idx1_pos:min(len(dm), report.avi_idx1_pos + 8 + idx_size)])
        # 참조 헤더의 프레임 수(avih dwTotalFrames, strh dwLength)는 참조 영상 것이다. 시간축은
        # 길이/프레임 수로 만드니 손상 파일의 실제 영상 청크 수로 바꿔 둔다.
        frames = 0
        gaps: list = []
        for _pos, cid, _size in _avi.walk_movi_chunks(dm, fourcc + 4, movi_end, gaps):
            if cid[2:4] in (b"dc", b"db"):
                frames += 1
    head = bytearray(head)
    avih = head.find(b"avih")
    if avih >= 0 and frames:
        struct.pack_into("<I", head, avih + 8 + 16, frames)
    for m in re.finditer(rb"strh", bytes(head)):
        p = m.start() + 8
        if head[p:p + 4] == b"vids" and frames:
            struct.pack_into("<I", head, p + 32, frames)   # dwLength
    movi_payload = b"movi" + body
    if len(movi_payload) & 1:
        movi_payload += b"\0"
    with open(out_path, "xb") as out:
        out.write(bytes(head))
        out.write(b"LIST" + struct.pack("<I", len(movi_payload)) + movi_payload)
        out.write(tail)
        total = out.tell()
        out.seek(4)
        out.write(struct.pack("<I", total - 8))
    return (f"참조 영상 {os.path.basename(reference)}의 RIFF/hdrl 헤더({len(head):,}바이트)를 이식했습니다. "
            f"영상·GPS 데이터는 손상 파일 것입니다 (영상 청크 {frames}개 기준으로 프레임 수 보정).")


def _transplant_fmp4(report: DamageReport, reference: str, out_path: str) -> str:
    size = os.path.getsize(reference)
    with open(reference, "rb") as rf:
        boxes, _b, _k = _mp4.scan_top_level_bounded(rf, size)
        first_moof = next((b.start for b in boxes if b.box_type == b"moof"), None)
        if first_moof is None:
            raise ValueError("참조 영상에 moof가 없습니다.")
        rf.seek(0)
        init = rf.read(first_moof)
    if report.mp4_first_moof is None:
        raise ValueError("손상 파일에서 정상 moof를 찾지 못했습니다.")
    with open(report.path, "rb") as df, open(out_path, "xb") as out:
        out.write(init)
        df.seek(report.mp4_first_moof)
        while True:
            block = df.read(4 * 1024 * 1024)
            if not block:
                break
            out.write(block)
    return (f"참조 영상 {os.path.basename(reference)}의 ftyp/moov 초기화 헤더({len(init):,}바이트)를 이식했습니다. "
            f"조각(moof/mdat)은 손상 파일 것입니다.")
