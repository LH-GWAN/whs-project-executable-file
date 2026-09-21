from __future__ import annotations

import os
import struct
from typing import Optional

from core.paths import ensure_vendor_importable

ensure_vendor_importable()

import integration_avi as _avi  # noqa: E402
import integration_mp4 as _mp4  # noqa: E402

from core.format_sniffer import CONTAINER_AVI, CONTAINER_MP4  # noqa: E402


def get_duration_sec(path: str, container: str,
                      engine_output_dir: Optional[str] = None) -> Optional[float]:
    try:
        if container == CONTAINER_AVI:
            return _avi_duration(path)
        if container == CONTAINER_MP4:
            duration = _mp4_mvhd_duration(path)
            if duration:
                return duration
            # 조각(moof/mdat) MP4는 mvhd duration이 0이다(INAVI QXD8000 등). 조각을 훑어 계산한다.
            duration = _mp4_fragment_duration(path)
            if duration:
                return duration
            if engine_output_dir:
                return _timeline_fallback(engine_output_dir)
    except (OSError, struct.error, ValueError, IndexError):
        return None
    return None


def _avi_duration(path: str) -> Optional[float]:
    import mmap

    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            hdrl, _movi, _idx1, _avix = _avi.find_top_level_sections(mm)
            if hdrl is None:
                return None
            dw_streams, streams = _avi.parse_hdrl(mm, hdrl)
            stream_table = _avi.build_stream_table(dw_streams, streams, [])
            duration, _source = _avi.compute_video_duration(mm, hdrl, stream_table)
            return duration
        finally:
            mm.close()


def _mp4_mvhd_payload(path: str) -> Optional[bytes]:
    filesize = os.path.getsize(path)
    with open(path, "rb") as f:
        top_boxes = _mp4.scan_top_level(f, filesize)
        moov = _mp4.find_box(top_boxes, b"moov")
        if moov is None:
            return None
        moov_children = list(_mp4.iter_boxes(f, moov.payload_start, moov.end,
                                              context="duration-moov"))
        mvhd = _mp4.find_box(moov_children, b"mvhd")
        if mvhd is None:
            return None
        f.seek(mvhd.payload_start)
        return f.read(mvhd.size - mvhd.header_size)


def _mp4_mvhd_fields(payload: bytes):
    """(creation_time, timescale, duration). mvhd 시각은 1904-01-01 UTC 기준 초."""
    if len(payload) < 4:
        return None
    version = payload[0]
    if version == 1:
        if len(payload) < 32:
            return None
        ctime = struct.unpack_from(">Q", payload, 4)[0]
        timescale = struct.unpack_from(">I", payload, 20)[0]
        duration = struct.unpack_from(">Q", payload, 24)[0]
    else:
        if len(payload) < 20:
            return None
        ctime = struct.unpack_from(">I", payload, 4)[0]
        timescale = struct.unpack_from(">I", payload, 12)[0]
        duration = struct.unpack_from(">I", payload, 16)[0]
    return ctime, timescale, duration


def _mp4_mvhd_duration(path: str) -> Optional[float]:
    payload = _mp4_mvhd_payload(path)
    fields = _mp4_mvhd_fields(payload) if payload is not None else None
    if fields is None:
        return None
    _ctime, timescale, duration = fields
    if not timescale or not duration:
        return None
    return duration / timescale


def _mp4_fragment_duration(path: str) -> Optional[float]:
    """fragmented MP4의 길이: 트랙별로 (마지막 조각의 tfdt + 그 조각 sample duration 합)의 최대를
    트랙 timescale로 나눈다. 비디오 트랙을 우선하고, 없으면 가장 긴 트랙. 엔진(vendor)의 박스
    파서를 그대로 쓴다. mdat은 읽지 않으므로 파일 크기와 무관하게 빠르다."""
    filesize = os.path.getsize(path)
    tracks = {}
    end_times = {}
    with open(path, "rb") as f:
        top_boxes = _mp4.scan_top_level(f, filesize)
        moov = _mp4.find_box(top_boxes, b"moov")
        if moov is None:
            return None
        moov_children = list(_mp4.iter_boxes(f, moov.payload_start, moov.end, context="duration-moov"))
        for trak in _mp4.find_all(moov_children, b"trak"):
            try:
                track_id, handler_type, _name, timescale, _stsd = _mp4.parse_trak(f, trak)
            except Exception:  # noqa: BLE001
                continue
            tracks[track_id] = (handler_type, timescale)
        mvex = _mp4.find_box(moov_children, b"mvex")
        trex_defaults = _mp4.parse_mvex(f, mvex) if mvex is not None else {}
        for moof in _mp4.find_all(top_boxes, b"moof"):
            moof_children = list(_mp4.iter_boxes(f, moof.payload_start, moof.end, context="duration-moof"))
            for traf in _mp4.find_all(moof_children, b"traf"):
                traf_children = list(_mp4.iter_boxes(f, traf.payload_start, traf.end, context="duration-traf"))
                tfhd_box = _mp4.find_box(traf_children, b"tfhd")
                tfdt_box = _mp4.find_box(traf_children, b"tfdt")
                if tfhd_box is None or tfdt_box is None:
                    continue
                tfhd = _mp4.parse_tfhd(f, tfhd_box)
                base = _mp4.parse_tfdt(f, tfdt_box)
                if tfhd is None or base is None:
                    continue
                trex = trex_defaults.get(tfhd.track_id)
                total = 0
                for trun_box in _mp4.find_all(traf_children, b"trun"):
                    trun = _mp4.parse_trun(f, trun_box)
                    if trun is None:
                        continue
                    for sample in trun.samples:
                        try:
                            d = _mp4.resolve_sample_duration(sample, tfhd, trex)
                        except Exception:  # noqa: BLE001
                            d = None
                        total += d or 0
                end = base + total
                if end > end_times.get(tfhd.track_id, 0):
                    end_times[tfhd.track_id] = end
    best = None
    for track_id, end in end_times.items():
        handler, timescale = tracks.get(track_id, (None, None))
        if not timescale:
            continue
        sec = end / timescale
        if handler in (b"vide", "vide"):
            return sec
        best = sec if best is None else max(best, sec)
    return best


# mvhd creation_time의 기준(1904-01-01)과 Unix epoch(1970-01-01) 차이
_MP4_EPOCH_OFFSET = 2082844800


def mp4_recorded_at_epoch(path: str) -> Optional[float]:
    """MP4 컨테이너에 적힌 녹화(생성) 시각을 Unix epoch 초로. 없거나 0이면 None.
    전방/후방 파일이 같은 녹화인지 볼 때 쓴다(같은 사건이면 몇 초 안에 만들어진다)."""
    try:
        payload = _mp4_mvhd_payload(path)
    except (OSError, struct.error, ValueError, IndexError):
        return None
    fields = _mp4_mvhd_fields(payload) if payload is not None else None
    if fields is None or not fields[0]:
        return None
    return float(fields[0] - _MP4_EPOCH_OFFSET)


def _timeline_fallback(engine_output_dir: str) -> Optional[float]:
    import csv
    import glob

    best_end = None
    pattern = os.path.join(engine_output_dir, "**", "timeline.csv")
    for csv_path in glob.glob(pattern, recursive=True):
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    raw = (row.get("end_time_sec") or "").strip()
                    if not raw:
                        continue
                    try:
                        value = float(raw)
                    except ValueError:
                        continue
                    if best_end is None or value > best_end:
                        best_end = value
        except OSError:
            continue
    return best_end
