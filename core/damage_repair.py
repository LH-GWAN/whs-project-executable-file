"""헤더 이식 - 헤더(AVI hdrl / fMP4 moov)가 사라진 손상 파일에 같은 기기 정상 영상의 헤더를 붙인 작업
사본을 만든다. 원본·분석 사본은 손대지 않고 `source/<이름>_repaired.<확장자>`를 따로 만든다.

참조 영상에서는 설정(스트림 구성·코덱·해상도·FPS)만 가져오고 영상·GPS 데이터는 손상 파일 것만 쓴다.
  - AVI: 참조의 RIFF/hdrl(movi 앞까지) + 손상 파일의 movi 본문(옛 녹화 잔재 포함) + 손상 파일의 idx1 + 그 뒤 꼬리.
    프레임 수(avih dwTotalFrames, strh dwLength)는 손상 파일의 색인을 스트림별로 세어 바꾼다(참조 값이면 시간축이 틀어진다).
  - fMP4: 참조의 ftyp + moov(길이 필드는 0으로) + 손상 파일의 첫 미디어 Box(mdat/moof)부터 끝까지.
    moov에 샘플 표가 든 혼합 fMP4는 참조로 받지 않는다(참조 영상의 샘플이 증거에 섞인다).
참조는 구조·헤더 내용을 검사하고, 손상 파일에서 얻을 수 있는 근거(청크 ID 집합, 코덱 종류, GPS UTC로 잰 FPS,
MJPG 해상도 / 조각의 tfdt·sample duration)와 대조해 맞지 않으면 거부한다.
사용자 결정(2026-10-11): 같은 폴더에서 같은 기기의 정상 영상을 자동으로 찾고, 없으면 사용자에게 묻는다.
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from core.damage import (DamageReport, _infer_base, _mp4_moov_ok, _read_at, avi_header_streams_from_bytes,
                         avi_sections, chunk_rmc_utc, parse_idx1, walk_movi_chunks)
from core.paths import ensure_vendor_importable
from engine.engine_adapter import CancelledError

ensure_vendor_importable()

import integration_avi as _avi  # noqa: E402
import integration_blackbox as _blackbox  # noqa: E402
import integration_mp4 as _mp4  # noqa: E402

_NAME_PREFIX_RE = re.compile(r"^[A-Za-z_]+")
_CHANNEL_RE = re.compile(r"(\D*)$")
_COPY_BLOCK = 8 * 1024 * 1024
_IGNORED_CHUNK_IDS = (b"JUNK", b"LIST", b"rec ", b"idx1")
_FPS_TOLERANCE = 0.15
_TIMESCALE_TOLERANCE = 0.10
_CODEC_FAMILY = {
    b"H264": "avc", b"h264": "avc", b"AVC1": "avc", b"avc1": "avc", b"X264": "avc", b"x264": "avc",
    b"HEVC": "hevc", b"hevc": "hevc", b"HVC1": "hevc", b"hvc1": "hevc", b"HEV1": "hevc", b"hev1": "hevc",
    b"H265": "hevc", b"h265": "hevc", b"X265": "hevc", b"x265": "hevc",
    b"MJPG": "mjpg", b"mjpg": "mjpg", b"JPEG": "mjpg", b"jpeg": "mjpg", b"dmb1": "mjpg",
}
_CODEC_LABEL = {"avc": "H.264", "hevc": "H.265", "mjpg": "MJPG"}


def _name_prefix(path: str) -> str:
    m = _NAME_PREFIX_RE.match(os.path.basename(path))
    return (m.group(0) if m else "").upper()


def _digits(path: str) -> int:
    digits = re.sub(r"\D", "", os.path.basename(path))
    return int(digits[:14] or "0")


def _channel_tag(path: str) -> str:
    """파일 이름 끝의 채널 표시(_F/_R/_S/_N, …D/…E 등 - 마지막 숫자 뒤의 글자). 없으면 ''."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = _CHANNEL_RE.search(stem)
    return (m.group(1) if m else "").strip("_- ").casefold()


def _check_cancel(cancel_event) -> None:
    """참조 탐색·검증·이식은 파일을 끝까지 읽으니 취소 신호가 서면 CancelledError로 멈춘다(리뷰 #48, #50)."""
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("참조 영상 처리가 취소되었습니다.")


def _same_file(a: str, b: str) -> bool:
    try:
        if os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ---------------------------------------------------------------------------
# AVI 참조 정보 / 손상 파일 근거
# ---------------------------------------------------------------------------

@dataclass
class _AviRef:
    head: bytes                              # RIFF 머리 ~ movi LIST 직전
    streams: list                            # 엔진 StreamInfo 목록(hdrl 순서 = 스트림 번호)
    resolution: Dict[int, Tuple[int, int]]   # vids 스트림 번호 → (너비, 높이)
    compression: Dict[int, bytes]            # vids 스트림 번호 → strf biCompression
    chunk_ids: Set[bytes]
    strh_pos: Dict[int, int]                 # 스트림 번호 → head 안 strh 데이터 시작
    avih_pos: Optional[int]

    def video_streams(self) -> list:
        return [s for s in self.streams if s.fcc_type == b"vids"]

    def fps(self) -> Optional[float]:
        for s in self.video_streams():
            if s.dw_rate and s.dw_scale:
                return s.dw_rate / s.dw_scale
        return None

    def codec_family(self) -> Optional[str]:
        for s in self.video_streams():
            fam = _CODEC_FAMILY.get(s.fcc_handler or b"") or _CODEC_FAMILY.get(self.compression.get(s.index, b""))
            if fam:
                return fam
        return None


def _avi_ref_info(path: str) -> Tuple[Optional[_AviRef], str]:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if _read_at(f, 0, 4) != b"RIFF":
            return None, "참조 영상이 RIFF로 시작하지 않습니다."
        hdrl, movi, idx1, _avix, _trunc = avi_sections(f, size)
        if hdrl is None or movi is None or idx1 is None:
            return None, "참조 영상의 구조(hdrl/movi/idx1)가 온전하지 않습니다."
        if hdrl.pos >= movi.pos or movi.pos > 64 * 1024 * 1024:
            return None, "참조 영상의 헤더 구조가 비정상입니다."
        head = _read_at(f, 0, movi.pos)
        entries = parse_idx1(f, idx1.data_start, min(idx1.ck_size, 16 * 4000))
    has_avih, streams = avi_header_streams_from_bytes(head[hdrl.pos:hdrl.data_end])
    if not has_avih or not any(s.fcc_type == b"vids" and s.dw_rate and s.dw_scale for s in streams):
        return None, "참조 영상의 헤더 내용(avih/strh)을 읽을 수 없습니다."
    resolution: Dict[int, Tuple[int, int]] = {}
    compression: Dict[int, bytes] = {}
    strh_pos: Dict[int, int] = {}
    avih_pos = None
    sidx = 0
    for child in _avi.iter_chunks(head, hdrl.pos + 12, hdrl.data_end):
        if child.ck_id == b"avih" and not child.is_list:
            avih_pos = child.data_start
        elif child.is_list and child.list_type == b"strl":
            for sc in _avi.iter_chunks(head, child.content_start, child.content_end):
                if sc.ck_id == b"strh" and not sc.is_list and sc.ck_size >= 36:
                    strh_pos[sidx] = sc.data_start
                elif sc.ck_id == b"strf" and not sc.is_list and sc.ck_size >= 20 \
                        and sidx < len(streams) and streams[sidx].fcc_type == b"vids":
                    w, h = struct.unpack_from("<ii", head, sc.data_start + 4)
                    resolution[sidx] = (abs(w), abs(h))
                    compression[sidx] = bytes(head[sc.data_start + 16:sc.data_start + 20])
            sidx += 1
    ids = {e["chunk_id"] for e in entries} - set(_IGNORED_CHUNK_IDS)
    ids = {i for i in ids if not i.endswith(b"ix")}
    return _AviRef(head=head, streams=streams, resolution=resolution, compression=compression, chunk_ids=ids,
                   strh_pos=strh_pos, avih_pos=avih_pos), ""


@dataclass
class _AviEvidence:
    chunk_ids: Set[bytes] = field(default_factory=set)
    walked: list = field(default_factory=list)       # (pos, cid, size) - JUNK 제외
    gaps: list = field(default_factory=list)
    frames: Dict[int, int] = field(default_factory=dict)  # 스트림 번호 → 걸어 읽은 영상 청크 수
    codec: Optional[str] = None
    mjpg_size: Optional[Tuple[int, int]] = None
    fps: Optional[float] = None                       # GPS UTC 간격으로 잰 첫 영상 스트림 FPS
    utc_span: Optional[float] = None                  # 첫 RMC ~ 마지막 RMC (초)


def _nal_family(payload: bytes) -> Optional[str]:
    """영상 청크 앞부분으로 코덱 종류를 추정한다: JPEG SOI / H.264·H.265 NAL 머리."""
    if payload[:2] == b"\xff\xd8":
        return "mjpg"
    if payload[:4] == b"\0\0\0\1":
        nal = payload[4:6]
    elif payload[:3] == b"\0\0\1":
        nal = payload[3:5]
    else:
        return None
    if len(nal) < 2 or nal[0] & 0x80:
        return None
    hevc_type = (nal[0] >> 1) & 0x3F
    if nal[0] & 0x01 == 0 and nal[1] == 0x01 and hevc_type in (0, 1, 19, 20, 21, 32, 33, 34, 35, 39, 40):
        return "hevc"
    if nal[0] & 0x1F in (1, 5, 6, 7, 8, 9):
        return "avc"
    return None


def _jpeg_size(payload: bytes) -> Optional[Tuple[int, int]]:
    """JPEG SOF 마커에서 (너비, 높이)."""
    pos = 2
    while pos + 9 <= len(payload):
        if payload[pos] != 0xFF:
            return None
        marker = payload[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        seg_len = struct.unpack(">H", payload[pos + 2:pos + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", payload[pos + 5:pos + 9])
            return w, h
        pos += 2 + seg_len
    return None


def _avi_evidence(report: DamageReport, cancel_event=None) -> _AviEvidence:
    """손상 파일 movi를 한 번 걸어 참조 대조에 쓸 근거를 모은다."""
    ev = _AviEvidence()
    start = (report.avi_movi_fourcc or 0) + 4
    end = report.media_range[1] if report.media_range else report.size
    utc_points: List[Tuple[int, float]] = []       # (청크 위치, UTC)
    first_video: Dict[int, bytes] = {}
    with open(report.path, "rb") as f:
        for pos, cid, size in walk_movi_chunks(f, start, end, ev.gaps, cancel_event):
            if cid in _IGNORED_CHUNK_IDS or cid.endswith(b"ix"):
                continue
            ev.chunk_ids.add(cid)
            ev.walked.append((pos, cid, size))
            sidx = _avi.stream_index_from_chunk_id(cid)
            if cid[2:4] in (b"dc", b"db") and sidx is not None:
                ev.frames[sidx] = ev.frames.get(sidx, 0) + 1
                if sidx not in first_video and size >= 8:
                    first_video[sidx] = _read_at(f, pos + 8, min(size, 64 * 1024))
            elif size <= 4096 and cid[2:4] in (b"tx", b"st", b"wb", b"pc"):
                utc = chunk_rmc_utc(f, pos, size)
                if utc is not None:
                    utc_points.append((pos, utc))
    if first_video:
        families = {_nal_family(p) for p in first_video.values()} - {None}
        if len(families) == 1:
            ev.codec = families.pop()
        vid0 = first_video.get(min(first_video))
        if vid0 is not None and ev.codec == "mjpg":
            ev.mjpg_size = _jpeg_size(vid0)
    if utc_points:
        # 같은 UTC가 프레임마다 반복되는 기기(VUGERA: 1 Hz GPS를 30 프레임에 복사)는 UTC가 바뀌는 첫 청크만 남긴다.
        deduped: List[Tuple[int, float]] = []
        for pos, utc in utc_points:
            if not deduped or deduped[-1][1] != utc:
                deduped.append((pos, utc))
        utc_points = deduped
        span = utc_points[-1][1] - utc_points[0][1]
        if span < 0:
            span += 86400
        ev.utc_span = span
        # 손상 구간이 끼지 않은 이웃 RMC 사이의 영상 청크 수로 FPS를 잰다(0 < 간격 ≤ 5초만).
        vid0_idx = min(ev.frames) if ev.frames else None
        video_pos = sorted(p for p, c, _s in ev.walked
                           if c[2:4] in (b"dc", b"db") and _avi.stream_index_from_chunk_id(c) == vid0_idx)
        gap_starts = sorted(g[0] for g in ev.gaps)
        import bisect
        frames_total, seconds_total = 0, 0.0
        for (p0, u0), (p1, u1) in zip(utc_points, utc_points[1:]):
            du = u1 - u0
            if du < 0:
                du += 86400
            if not (0 < du <= 5):
                continue
            gi = bisect.bisect_left(gap_starts, p0)
            if gi < len(gap_starts) and gap_starts[gi] < p1:
                continue
            n = bisect.bisect_left(video_pos, p1) - bisect.bisect_left(video_pos, p0)
            frames_total += n
            seconds_total += du
        if seconds_total >= 5 and frames_total:
            ev.fps = frames_total / seconds_total
    return ev


def _compare_avi(ev: _AviEvidence, ref: _AviRef) -> Tuple[bool, str]:
    """손상 파일 근거와 참조 헤더를 대조한다. (통과?, 설명)."""
    if not ev.chunk_ids:
        return False, "손상 파일에서 청크를 읽을 수 없습니다."
    if ref.chunk_ids != ev.chunk_ids:
        return False, (f"스트림 구성이 다릅니다 (참조 {sorted(i.decode('ascii', 'replace') for i in ref.chunk_ids)}, "
                       f"손상 파일 {sorted(i.decode('ascii', 'replace') for i in ev.chunk_ids)})")
    checked = ["스트림 구성 일치"]
    ref_codec = ref.codec_family()
    if ev.codec and ref_codec:
        if ev.codec != ref_codec:
            return False, (f"영상 코덱이 다릅니다 (참조 {_CODEC_LABEL[ref_codec]}, 손상 파일 영상 데이터는 "
                           f"{_CODEC_LABEL[ev.codec]})")
        checked.append(f"코덱 {_CODEC_LABEL[ref_codec]} 일치")
    ref_fps = ref.fps()
    if ev.fps and ref_fps:
        if abs(ev.fps - ref_fps) > _FPS_TOLERANCE * ref_fps:
            return False, (f"FPS가 다릅니다 (참조 {ref_fps:.2f} fps, 손상 파일의 GPS 간격으로 잰 값 {ev.fps:.2f} fps). "
                           "이식하면 재생 시각이 틀어집니다.")
        checked.append(f"FPS {ref_fps:.2f} 일치 (GPS 간격 기준 {ev.fps:.2f})")
    first_vid = min(ref.resolution) if ref.resolution else None
    if ev.mjpg_size and first_vid is not None:
        w, h = ref.resolution[first_vid]
        if abs(w - ev.mjpg_size[0]) > 16 or abs(h - ev.mjpg_size[1]) > 16:
            return False, f"해상도가 다릅니다 (참조 {w}x{h}, 손상 파일 영상 {ev.mjpg_size[0]}x{ev.mjpg_size[1]})"
        checked.append(f"해상도 {w}x{h} 일치")
    return True, "; ".join(checked)


# ---------------------------------------------------------------------------
# fMP4 참조 정보 / 손상 파일 근거
# ---------------------------------------------------------------------------

@dataclass
class _Mp4Ref:
    ftyp: bytes
    moov: bytes
    tracks: list
    trex: dict

    def track_ids(self) -> Set[int]:
        return {t.get("track_id") for t in self.tracks if t.get("track_id") is not None}

    def timescale(self, track_id: int) -> Optional[int]:
        for t in self.tracks:
            if t.get("track_id") == track_id:
                return t.get("timescale")
        return None


def _mp4_ref_info(path: str, fragmented: bool) -> Tuple[Optional[_Mp4Ref], str]:
    size = os.path.getsize(path)
    try:
        with open(path, "rb") as f:
            boxes, _boundary, kind = _mp4.scan_top_level_bounded(f, size)
            gaps = list(_mp4.DAMAGE_GAPS)
            types = [b.box_type for b in boxes]
            moov = next((b for b in boxes if b.box_type == b"moov"), None)
            if moov is None:
                return None, "참조 영상에 moov가 없습니다."
            if kind == "corrupt" or gaps:
                return None, "참조 영상 자체에 손상 구간이 있습니다."
            if (b"moof" in types) != fragmented:
                return None, "참조 영상의 구조(조각 여부)가 손상 파일과 다릅니다."
            if not _mp4_moov_ok(f, moov, size, fragmented):
                return None, "참조 영상의 moov 내용을 읽을 수 없습니다."
            first_moof = next((b.start for b in boxes if b.box_type == b"moof"), None)
            if fragmented:
                if any(b.box_type == b"mdat" and b.start < first_moof for b in boxes):
                    return None, "참조 영상이 혼합 fMP4(첫 조각 앞에 mdat)라 참조 영상의 데이터가 섞일 수 있어 쓸 수 없습니다."
                if _moov_has_samples(f, moov):
                    return None, "참조 영상의 moov에 샘플 표가 들어 있어(혼합 fMP4) 참조 영상의 데이터가 섞일 수 있어 쓸 수 없습니다."
            _text, trex, tracks = _mp4.parse_moov(f, moov)
            ftyp = next((b for b in boxes if b.box_type == b"ftyp"), None)
            ftyp_bytes = _read_at(f, ftyp.start, ftyp.size) if ftyp is not None else b""
            moov_bytes = _read_at(f, moov.start, moov.size)
    finally:
        _mp4.WARNINGS.clear()
    return _Mp4Ref(ftyp=ftyp_bytes, moov=moov_bytes, tracks=tracks, trex=trex), ""


def _moov_has_samples(f, moov) -> bool:
    """trak의 stsz/stco에 샘플이 하나라도 있으면 True(혼합 fMP4)."""
    for trak in _mp4.find_all(list(_mp4.iter_child_boxes(f, moov.payload_start, moov.end, context="ref-moov")), b"trak"):
        mdia = _mp4.find_box(list(_mp4.iter_child_boxes(f, trak.payload_start, trak.end, context="ref-trak")), b"mdia")
        if mdia is None:
            continue
        minf = _mp4.find_box(list(_mp4.iter_child_boxes(f, mdia.payload_start, mdia.end, context="ref-mdia")), b"minf")
        if minf is None:
            continue
        stbl = _mp4.find_box(list(_mp4.iter_child_boxes(f, minf.payload_start, minf.end, context="ref-minf")), b"stbl")
        if stbl is None:
            continue
        for box in _mp4.iter_child_boxes(f, stbl.payload_start, stbl.end, context="ref-stbl"):
            if box.box_type in (b"stsz", b"stco", b"co64"):
                payload = _mp4.read_payload(f, box)
                off = 8 if box.box_type == b"stsz" else 4
                if len(payload) >= off + 4 and struct.unpack(">I", payload[off:off + 4])[0] > 0:
                    return True
    return False


@dataclass
class _Mp4Evidence:
    track_ids: Set[int] = field(default_factory=set)
    # 조각 번호 → {track_id: (tfdt, 샘플 수, trun duration 합 또는 None, tfhd default duration 또는 None)}
    frags: List[dict] = field(default_factory=list)
    utc: List[Optional[float]] = field(default_factory=list)   # 조각 번호 → 뒤따르는 mdat의 첫 RMC UTC


# 조각 mdat 안의 RMC: INAVI(Ambarella)는 '$' 없이 ';GPRMC,…' 형태로 넣는다.
_RMC_RE = re.compile(rb"[A-Z]{2}RMC,(\d{2})(\d{2})(\d{2}(?:\.\d+)?),")
_MDAT_RMC_SCAN_LIMIT = 16 * 1024 * 1024


def _mp4_evidence(path: str, first_moof: Optional[int], count: int = 6, cancel_event=None) -> _Mp4Evidence:
    ev = _Mp4Evidence()
    if first_moof is None:
        return ev
    size = os.path.getsize(path)
    try:
        with open(path, "rb") as f:
            pos = first_moof
            while pos + 8 <= size and len(ev.frags) < count:
                _check_cancel(cancel_event)
                box = _mp4.read_box_header(f, pos, size, context="evidence")
                if box is None or box.size < 8:
                    break
                if box.box_type == b"moof":
                    frag = {}
                    for traf in _mp4.find_all(list(_mp4.iter_child_boxes(f, box.payload_start, box.end, context="ev-moof")), b"traf"):
                        children = list(_mp4.iter_child_boxes(f, traf.payload_start, traf.end, context="ev-traf"))
                        tfhd_box = _mp4.find_box(children, b"tfhd")
                        tfdt_box = _mp4.find_box(children, b"tfdt")
                        if tfhd_box is None:
                            continue
                        tfhd = _mp4.parse_tfhd(f, tfhd_box)
                        if tfhd is None:
                            continue
                        ev.track_ids.add(tfhd.track_id)
                        tfdt = _mp4.parse_tfdt(f, tfdt_box) if tfdt_box is not None else None
                        n_samples, dur_sum, has_dur = 0, 0, True
                        for trun_box in _mp4.find_all(children, b"trun"):
                            trun = _mp4.parse_trun(f, trun_box)
                            if trun is None:
                                continue
                            n_samples += trun.sample_count
                            for s in trun.samples:
                                if s.duration is None:
                                    has_dur = False
                                else:
                                    dur_sum += s.duration
                        frag[tfhd.track_id] = (tfdt, n_samples, dur_sum if (has_dur and n_samples) else None,
                                               tfhd.default_sample_duration)
                    ev.frags.append(frag)
                    ev.utc.append(None)
                elif box.box_type == b"mdat" and ev.frags and ev.utc[-1] is None:
                    payload = _read_at(f, box.payload_start, min(box.end - box.payload_start, _MDAT_RMC_SCAN_LIMIT))
                    m = _RMC_RE.search(payload)
                    if m:
                        ev.utc[-1] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pos = box.end
    finally:
        _mp4.WARNINGS.clear()
    return ev


def _compare_mp4(ev: _Mp4Evidence, ref: _Mp4Ref) -> Tuple[bool, str]:
    if not ev.track_ids:
        return False, "손상 파일의 moof에서 track_ID를 읽을 수 없습니다."
    ref_ids = ref.track_ids()
    if not ev.track_ids <= ref_ids:
        return False, f"트랙 구성이 다릅니다 (참조 {sorted(ref_ids)}, 손상 파일 {sorted(ev.track_ids)})"
    checked = ["트랙 구성 일치"]
    # 조각 사이 벽시계(RMC UTC)와 tfdt 차이로 참조 timescale을, 샘플 수×sample duration으로 참조 trex 기본값을 대조한다.
    timed = [(i, u) for i, u in enumerate(ev.utc) if u is not None]
    if len(timed) >= 2:
        (i0, u0), (i1, u1) = timed[0], timed[-1]
        wall = u1 - u0
        if wall < 0:
            wall += 86400
        if 0 < wall <= 3600:
            for tid in sorted(ev.track_ids):
                a, b = ev.frags[i0].get(tid), ev.frags[i1].get(tid)
                ts = ref.timescale(tid)
                if a is None or b is None or a[0] is None or b[0] is None or not ts:
                    continue
                units = b[0] - a[0]
                if units <= 0:
                    continue
                est = units / wall
                if abs(est - ts) > _TIMESCALE_TOLERANCE * ts:
                    return False, (f"트랙 {tid}의 timescale이 다릅니다 (참조 {ts}, 손상 파일 조각의 tfdt·GPS 간격으로 잰 값 "
                                   f"{est:.0f}). 이식하면 재생 시각이 틀어집니다.")
                # 조각 길이: trun duration 합(있으면) 또는 tfhd/trex 기본값 × 샘플 수가 tfdt 차이와 맞아야 한다.
                dur_units = 0
                known = True
                for k in range(i0, i1):
                    fr = ev.frags[k].get(tid)
                    if fr is None:
                        known = False
                        break
                    if fr[2] is not None:
                        dur_units += fr[2]
                    else:
                        default = fr[3]
                        if default is None and tid in ref.trex:
                            default = ref.trex[tid].default_sample_duration
                        if not default:
                            known = False
                            break
                        dur_units += default * fr[1]
                if known and dur_units and abs(dur_units - units) > _TIMESCALE_TOLERANCE * units:
                    return False, (f"트랙 {tid}의 샘플 길이(참조 기본값 기준)가 조각 간격과 맞지 않습니다 "
                                   f"(조각 {i0}~{i1}: tfdt 차이 {units}, 샘플 길이 합 {dur_units}). "
                                   "참조 영상의 프레임 설정이 손상 파일과 다릅니다.")
            checked.append(f"timescale·샘플 길이 일치 (GPS 간격 {wall:.0f}초 기준)")
    return True, "; ".join(checked)


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

def _reference_ok(path: str, container: str, fragmented: bool) -> bool:
    """후보 참조의 구조·헤더 내용이 온전한가(자동 탐색용)."""
    try:
        found, _note = _blackbox.detect_container(path)
        if found != container:
            return False
        if container == _blackbox.CONTAINER_AVI:
            return _avi_ref_info(path)[0] is not None
        return _mp4_ref_info(path, fragmented)[0] is not None
    except (OSError, ValueError, struct.error):
        return False
    finally:
        _avi.WARNINGS.clear()
        _mp4.WARNINGS.clear()


def find_reference_candidates(path: str, report: DamageReport, limit: int = 5, cancel_event=None) -> List[str]:
    """같은 폴더에서 같은 확장자·같은 이름 접두(REC_/EVT_ 등)·같은 채널 표시(_F/_R 등)·구조가 정상인 파일.
    파일명 숫자(녹화 시각)가 가까운 순. 손상 파일 자신은 제외. 반대 채널(전방↔후방)은 FPS·해상도가 달라도
    청크 구성이 같아 검증을 통과할 수 있으므로 후보에서 뺀다(리뷰 #13)."""
    folder = os.path.dirname(os.path.abspath(path))
    ext = os.path.splitext(path)[1].casefold()
    prefix = _name_prefix(path)
    channel = _channel_tag(path)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    candidates = []
    for name in sorted(names):
        full = os.path.join(folder, name)
        if _same_file(full, path) or not os.path.isfile(full):
            continue
        if os.path.splitext(name)[1].casefold() != ext or os.path.getsize(full) == 0:
            continue
        if prefix and _name_prefix(full) != prefix:
            continue
        if _channel_tag(full) != channel:
            continue
        candidates.append(full)
    candidates.sort(key=lambda p: abs(_digits(p) - _digits(path)))
    out = []
    evidence = None
    for cand in candidates:
        _check_cancel(cancel_event)
        if not _reference_ok(cand, report.container, report.fragmented):
            continue
        # 구조가 맞아도 설정(FPS·코덱·해상도 / timescale)이 손상 파일 근거와 어긋나면 후보에서 뺀다(리뷰 #13).
        try:
            ok, _reason, evidence, _ref = _validate(report, cand, evidence, cancel_event)
        except (OSError, ValueError, struct.error, AttributeError):
            ok = False
        except InterruptedError:
            raise CancelledError("참조 영상 탐색이 취소되었습니다.")
        finally:
            _avi.WARNINGS.clear()
            _mp4.WARNINGS.clear()
        if ok:
            out.append(cand)
            if len(out) >= limit:
                break
    return out


def _validate(report: DamageReport, reference: str, evidence=None, cancel_event=None):
    """(통과?, 설명, 손상 파일 근거, 참조 정보). evidence를 주면 손상 파일을 다시 걷지 않는다."""
    _check_cancel(cancel_event)
    if _same_file(report.path, reference):
        return False, "손상 파일 자신은 참조로 쓸 수 없습니다.", None, None
    found, _note = _blackbox.detect_container(reference)
    if found != report.container:
        return False, "참조 영상의 형식이 손상 파일과 다릅니다.", None, None
    if report.container == _blackbox.CONTAINER_AVI:
        ref, reason = _avi_ref_info(reference)
        if ref is None:
            return False, reason, None, None
        ev = evidence if isinstance(evidence, _AviEvidence) else _avi_evidence(report, cancel_event)
        ok, reason = _compare_avi(ev, ref)
        return ok, reason, ev, ref
    if not report.fragmented:
        return False, "moov가 끝에 있는 일반 MP4는 참조 헤더를 이식할 수 없습니다(샘플 표가 파일마다 다름).", None, None
    ref, reason = _mp4_ref_info(reference, True)
    if ref is None:
        return False, reason, None, None
    ev = evidence if isinstance(evidence, _Mp4Evidence) else _mp4_evidence(report.path, report.mp4_first_moof,
                                                                              cancel_event=cancel_event)
    ok, reason = _compare_mp4(ev, ref)
    return ok, reason, ev, ref


def validate_reference(report: DamageReport, reference: str, cancel_event=None) -> Tuple[bool, str]:
    """참조가 손상 파일과 같은 구성인지. 구조·헤더 내용 검사 → AVI: 청크 ID 집합(JUNK/ix##/LIST 제외)·코덱·FPS·
    (MJPG) 해상도 대조, fMP4: track_ID 포함 + tfdt/GPS 간격으로 timescale·샘플 길이 대조.
    cancel_event가 서면 CancelledError."""
    try:
        ok, reason, _ev, _ref = _validate(report, reference, None, cancel_event)
        return ok, reason
    except InterruptedError:
        raise CancelledError("참조 영상 검증이 취소되었습니다.")
    except (OSError, ValueError, struct.error, AttributeError) as exc:
        return False, f"참조 검사 실패: {exc}"
    finally:
        _avi.WARNINGS.clear()
        _mp4.WARNINGS.clear()


def _moof_track_ids(path: str, first_moof: Optional[int], count: int = 3) -> set:
    return _mp4_evidence(path, first_moof, count).track_ids


def transplant_header(report: DamageReport, reference: str, out_path: str, cancel_event=None) -> str:
    """참조 헤더 + 손상 파일 본문으로 작업 사본을 만든다. 성공하면 설명 문자열을 돌려주고, 못 하면 ValueError.
    cancel_event가 서면 CancelledError(사본은 지운다)."""
    try:
        ok, reason, ev, ref = _validate(report, reference, None, cancel_event)
    except InterruptedError:
        raise CancelledError("헤더 이식이 취소되었습니다.")
    finally:
        _avi.WARNINGS.clear()
        _mp4.WARNINGS.clear()
    if not ok:
        raise ValueError(reason)
    if os.path.exists(out_path):
        raise FileExistsError(out_path)
    try:
        if report.container == _blackbox.CONTAINER_AVI:
            return _transplant_avi(report, reference, out_path, ev, ref, cancel_event)
        return _transplant_fmp4(report, reference, out_path, ref, cancel_event)
    except InterruptedError:
        _remove_quiet(out_path)
        raise CancelledError("헤더 이식이 취소되었습니다.")
    except BaseException:
        _remove_quiet(out_path)
        raise


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _copy_range(src, out, start: int, end: int, cancel_event=None) -> int:
    """src[start:end)를 8 MB씩 out에 쓴다. 블록마다 취소를 확인한다(리뷰 #50)."""
    pos = start
    while pos < end:
        _check_cancel(cancel_event)
        block = _read_at(src, pos, min(_COPY_BLOCK, end - pos))
        if not block:
            break
        out.write(block)
        pos += len(block)
    return pos - start


def _avi_frame_counts(report: DamageReport, ev: _AviEvidence) -> Tuple[Dict[int, int], str]:
    """손상 파일의 스트림별 레코드 수(엔진이 시간축을 나누는 수와 같은 기준). idx1이 있으면 그 엔트리 수
    (손상 구간의 엔트리 포함), 없으면 걸어 읽은 청크 수 + 손상 구간의 빈 자리 수(엔진 synthesize_idx1과 같은 추정)."""
    counts: Dict[int, int] = {}
    if report.avi_idx1_pos is not None:
        with open(report.path, "rb") as f:
            idx_size = struct.unpack("<I", _read_at(f, report.avi_idx1_pos + 4, 4))[0]
            entries = parse_idx1(f, report.avi_idx1_pos + 8, min(idx_size, report.size - report.avi_idx1_pos - 8))
        for e in entries:
            cid = e["chunk_id"]
            if cid in _IGNORED_CHUNK_IDS or not _avi.CHUNK_ID_RE.fullmatch(cid):
                continue
            sidx = _avi.stream_index_from_chunk_id(cid)
            if sidx is not None:
                counts[sidx] = counts.get(sidx, 0) + 1
        return counts, "idx1 엔트리"
    entries = list(ev.walked)
    for gap in sorted(ev.gaps):
        entries.extend((None, e["chunk_id"], 0) for e in _avi._placeholder_entries(ev.walked, gap))
    for _pos, cid, _size in entries:
        sidx = _avi.stream_index_from_chunk_id(cid)
        if sidx is not None:
            counts[sidx] = counts.get(sidx, 0) + 1
    return counts, "movi 청크(손상 구간 빈 자리 포함)"


def _transplant_avi(report: DamageReport, reference: str, out_path: str, ev: _AviEvidence, ref: _AviRef,
                    cancel_event=None) -> str:
    fourcc = report.avi_movi_fourcc or 0
    body_end = report.avi_movi_end or (report.media_range[1] if report.media_range else report.size)
    idx1_pos = report.avi_idx1_pos
    idx1_end = None
    if idx1_pos is not None:
        with open(report.path, "rb") as f:
            idx_size = struct.unpack("<I", _read_at(f, idx1_pos + 4, 4))[0]
        idx1_end = min(report.size, idx1_pos + 8 + idx_size)
        body_end = min(body_end, idx1_pos)
    counts, basis = _avi_frame_counts(report, ev)
    # 참조 헤더의 프레임 수(avih dwTotalFrames, strh dwLength)는 참조 영상 것이다. 엔진은 영상 스트림 길이
    # (dwLength/fps)를 스트림별 레코드 수로 균등 분할해 시간축을 만드니, 스트림별로 손상 파일 수로 바꾼다(리뷰 #2).
    head = bytearray(ref.head)
    video = ref.video_streams()
    first_frames = 0
    for s in video:
        n = counts.get(s.index, 0)
        if n and s.index in ref.strh_pos:
            struct.pack_into("<I", head, ref.strh_pos[s.index] + 32, n)
            if not first_frames:
                first_frames = n
    if first_frames and ref.avih_pos is not None:
        struct.pack_into("<I", head, ref.avih_pos + 16, first_frames)
    warning = ""
    fps = ref.fps()
    if first_frames and fps and ev.utc_span and ev.utc_span >= 5:
        expected = first_frames / fps
        span = ev.utc_span + 1.0
        if abs(expected - span) > max(2.0, 0.1 * span):
            warning = (f" 경고: 이식 헤더 기준 재생 길이 {expected:.1f}초와 GPS UTC 간격 {span:.1f}초가 어긋납니다 - "
                       "재생 시각이 실제와 다를 수 있습니다.")
    body_len = body_end - (fourcc + 4)
    movi_len = 4 + body_len
    pad = movi_len & 1
    with open(report.path, "rb") as src, open(out_path, "xb") as out:
        out.write(bytes(head))
        out.write(b"LIST" + struct.pack("<I", movi_len + pad) + b"movi")
        _copy_range(src, out, fourcc + 4, body_end, cancel_event)
        if pad:
            out.write(b"\0")
        riff_end = out.tell()
        if idx1_pos is not None:
            _copy_range(src, out, idx1_pos, idx1_end, cancel_event)
            riff_end = out.tell()
            if idx1_end < report.size:
                # idx1 뒤 꼬리(옛 녹화 잔재 = 슬랙)는 그대로 둔다. 사본에서도 슬랙 카빙이 되게(리뷰 #85).
                _copy_range(src, out, idx1_end, report.size, cancel_event)
        out.seek(4)
        out.write(struct.pack("<I", riff_end - 8))
    frames_text = ", ".join(f"스트림 {s.index} {counts.get(s.index, 0)}개" for s in video)
    return (f"참조 영상 {os.path.basename(reference)}의 RIFF/hdrl 헤더({len(head):,}바이트)를 이식했습니다. "
            f"영상·GPS 데이터는 손상 파일 것입니다 (프레임 수는 손상 파일의 {basis} 기준으로 보정: {frames_text}).")


def _box_iter(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", buf, pos)[0]
        kind = buf[pos + 4:pos + 8]
        hdr = 8
        if size == 1 and pos + 16 <= end:
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield pos, kind, pos + hdr, pos + size
        pos += size


def _zero_moov_durations(moov: bytearray) -> List[str]:
    """참조 moov의 길이 필드(mvhd/tkhd/mdhd duration, mehd fragment_duration)를 0으로 둔다. 참조 영상의
    길이가 손상 파일 길이로 보이면 안 된다(리뷰 #1)."""
    patched: List[str] = []

    def version_at(p: int) -> int:
        return moov[p]

    def patch(buf_pos: int, name: str, off0: int, off1: int, wide: bool):
        if wide:
            struct.pack_into(">Q", moov, buf_pos + off1, 0)
        else:
            struct.pack_into(">I", moov, buf_pos + off0, 0)
        patched.append(name)

    for _p, kind, ps, pe in _box_iter(moov, 8, len(moov)):
        if kind == b"mvhd" and pe - ps >= 32:
            v1 = version_at(ps) == 1
            patch(ps, "mvhd", 16, 28, v1)
        elif kind == b"mvex":
            for _q, k2, ps2, pe2 in _box_iter(moov, ps, pe):
                if k2 == b"mehd" and pe2 - ps2 >= 8:
                    patch(ps2, "mehd", 4, 4, version_at(ps2) == 1)
        elif kind == b"trak":
            for _q, k2, ps2, pe2 in _box_iter(moov, ps, pe):
                if k2 == b"tkhd" and pe2 - ps2 >= 24:
                    patch(ps2, "tkhd", 20, 32, version_at(ps2) == 1)
                elif k2 == b"mdia":
                    for _r, k3, ps3, pe3 in _box_iter(moov, ps2, pe2):
                        if k3 == b"mdhd" and pe3 - ps3 >= 20:
                            patch(ps3, "mdhd", 16, 28, version_at(ps3) == 1)
    return patched


def _transplant_fmp4(report: DamageReport, reference: str, out_path: str, ref: _Mp4Ref, cancel_event=None) -> str:
    if report.mp4_first_moof is None:
        raise ValueError("손상 파일에서 정상 moof를 찾지 못했습니다.")
    if not ref.moov:
        raise ValueError("참조 영상의 moov를 읽지 못했습니다.")
    moov = bytearray(ref.moov)
    patched = _zero_moov_durations(moov)
    init = ref.ftyp + bytes(moov)
    # 손상 파일 쪽은 헤더 손상 뒤 첫 미디어 Box(moof 앞의 mdat가 있으면 그것)부터 끝까지 - 색인 없는 mdat도 증거라
    # 버리지 않는다(엔진이 손상 구간 카빙으로 GPS를 회수한다).
    start = report.mp4_media_start if report.mp4_media_start is not None else report.mp4_first_moof
    start = min(start, report.mp4_first_moof)
    with open(report.path, "rb") as src, open(out_path, "xb") as out:
        out.write(init)
        _copy_range(src, out, start, report.size, cancel_event)
    return (f"참조 영상 {os.path.basename(reference)}의 ftyp/moov 초기화 헤더({len(init):,}바이트)를 이식했습니다 "
            f"(참조 영상의 길이 필드 {', '.join(sorted(set(patched))) or '없음'}은 0으로 비움). "
            f"조각(moof/mdat)은 손상 파일 것입니다.")
