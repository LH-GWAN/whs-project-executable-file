"""손상 파일 검사·표시·임의 복구 (core/damage.py, core/damage_marks.py, 엔진 손상 허용)."""
from __future__ import annotations

import os
import struct

import pytest

from core.damage import DamageReport, scan_damage
from core.damage_marks import (MODE_INTERPOLATE, MODE_NONE, DamageInterval, apply_damage_marks,
                               compute_intervals, estimate_times_from_utc)
from core.paths import ensure_vendor_importable
from engine.engine_adapter import TrackPoint

ensure_vendor_importable()
import integration_blackbox as bb  # noqa: E402
import integration_mp4 as M  # noqa: E402


# ---------------------------------------------------------------------------
# 합성 파일 만들기
# ---------------------------------------------------------------------------

def _nmea(sec: int, lat: float = 37.5, lon: float = 127.0, speed_kn: float = 20.0) -> bytes:
    body = f"GPRMC,{8:02d}{24:02d}{sec:02d}.00,A,{lat * 100:.4f},N,{lon * 100:.4f},E,{speed_kn:.1f},84.4,160924,,,A"
    csum = 0
    for ch in body:
        csum ^= ord(ch)
    return f"${body}*{csum:02X}".encode("ascii")


def _chunk(cid: bytes, payload: bytes) -> bytes:
    return cid + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def _avi_bytes(seconds: int = 20, odd_pad: bool = True) -> bytes:
    """영상 청크(00dc)·GPS 텍스트 청크(02tx)가 초마다 하나씩 든 작은 AVI(hdrl 포함, idx1 포함)."""
    # strh(vids): fccType vids, handler MJPG, flags, prio, lang, initframes, scale, rate, start, length ...
    strh_v = (b"vids" + b"MJPG" + struct.pack("<IHHIIIIIIII", 0, 0, 0, 0, 1, 1, 0, seconds, 0, 0, 0)
              + struct.pack("<hhhh", 0, 0, 0, 0))
    strf_v = struct.pack("<IiiHHIIiiII", 40, 64, 48, 1, 24, 0x47504A4D, 64 * 48 * 3, 0, 0, 0, 0)
    strl_v = _chunk(b"LIST", b"strl" + _chunk(b"strh", strh_v) + _chunk(b"strf", strf_v))
    strh_t = (b"txts" + b"\0\0\0\0" + struct.pack("<IHHIIIIIIII", 0, 0, 0, 0, 1, 1, 0, seconds, 0, 0, 0)
              + struct.pack("<hhhh", 0, 0, 0, 0))
    strl_t = _chunk(b"LIST", b"strl" + _chunk(b"strh", strh_t) + _chunk(b"strf", b"\0" * 8))
    avih = struct.pack("<14I", 1_000_000, 0, 0, 0x10, seconds, 0, 2, 0, 64, 48, 0, 0, 0, 0)
    hdrl = _chunk(b"LIST", b"hdrl" + _chunk(b"avih", avih) + strl_v + strl_t)
    movi = b"movi"
    entries = []
    for sec in range(seconds):
        frame = b"\xff\xd8\xff" + bytes([sec]) * (1000 + sec) + b"\xff\xd9"
        text = _nmea(sec) + b"\r\n"
        for cid, payload in ((b"00dc", frame), (b"02tx", text)):
            entries.append((cid, len(movi), len(payload)))
            movi += cid + struct.pack("<I", len(payload)) + payload
            if len(payload) & 1 and odd_pad:
                movi += b"\0"
    movi_list = _chunk(b"LIST", movi)
    idx1 = b"".join(cid + struct.pack("<III", 0x10, off, size) for cid, off, size in entries)
    idx1 = _chunk(b"idx1", idx1)
    body = b"AVI " + hdrl + movi_list + idx1
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _mp4_fragmented_bytes(fragments: int = 6) -> bytes:
    """ftyp + moov(trak text) + moof/mdat 조각. 조각마다 GPS 텍스트 샘플 하나."""
    def box(kind, payload):
        return struct.pack(">I4s", 8 + len(payload), kind) + payload
    def full(kind, payload, version=0, flags=0):
        return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)
    mvhd = full(b"mvhd", struct.pack(">IIII", 0, 0, 1000, 0) + bytes(80))
    tkhd = full(b"tkhd", struct.pack(">IIII", 0, 0, 3, 0) + bytes(76), flags=7)
    mdhd = full(b"mdhd", struct.pack(">IIIIHH", 0, 0, 1000, 0, 0, 0))
    hdlr = full(b"hdlr", bytes(4) + b"text" + bytes(12) + b"text\0")
    stsd = full(b"stsd", struct.pack(">I", 1) + box(b"tx3g", bytes(6) + struct.pack(">H", 1) + bytes(30)))
    stbl = box(b"stbl", stsd + full(b"stts", struct.pack(">I", 0)) + full(b"stsc", struct.pack(">I", 0))
               + full(b"stsz", struct.pack(">II", 0, 0)) + full(b"stco", struct.pack(">I", 0)))
    dinf = box(b"dinf", full(b"dref", struct.pack(">I", 1) + full(b"url ", b"", flags=1)))
    minf = box(b"minf", full(b"nmhd", b"") + dinf + stbl)
    trak = box(b"trak", tkhd + box(b"mdia", mdhd + hdlr + minf))
    trex = full(b"trex", struct.pack(">IIIII", 3, 1, 1000, 0, 0))
    moov = box(b"moov", mvhd + trak + box(b"mvex", trex))
    out = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso6") + moov
    for n in range(fragments):
        sample = _nmea(n) + b"\r\n"
        mdat = box(b"mdat", sample)
        tfhd = full(b"tfhd", struct.pack(">I", 3), flags=0x020000)
        tfdt = full(b"tfdt", struct.pack(">I", n * 1000))
        trun_payload = struct.pack(">II", 1, 0) + struct.pack(">II", 1000, len(sample))
        trun = full(b"trun", trun_payload, flags=0x301)
        traf_tmp = box(b"traf", tfhd + tfdt + trun)
        moof_size = 8 + 16 + len(traf_tmp)
        trun = full(b"trun", struct.pack(">Ii", 1, moof_size + 8) + struct.pack(">II", 1000, len(sample)), flags=0x301)
        traf = box(b"traf", tfhd + tfdt + trun)
        moof = box(b"moof", full(b"mfhd", struct.pack(">I", n + 1)) + traf)
        out += moof + mdat
    return out


def _point(t, lat=None, lon=None, speed=None, offset=None, utc=""):
    return TrackPoint(start_time_sec=t, end_time_sec=(t + 1 if t is not None else None), latitude=lat,
                      longitude=lon, speed_kmh=speed,
                      source_offset=offset, gps_utc_time=utc, gps_checksum_ok=True)


# ---------------------------------------------------------------------------
# 검사
# ---------------------------------------------------------------------------

def test_intact_avi_is_not_damaged(tmp_path):
    p = tmp_path / "ok.avi"
    p.write_bytes(_avi_bytes())
    r = scan_damage(str(p))
    assert r.container == "avi" and not r.damaged and r.recoverable
    assert r.media_end_basis == "idx1"


def test_avi_signature_and_header_loss_detected(tmp_path):
    data = bytearray(_avi_bytes())
    data[0:8] = bytes(8)
    p = tmp_path / "sig.avi"
    p.write_bytes(bytes(data))
    assert bb.detect_container(str(p))[0] == "avi"
    r = scan_damage(str(p))
    assert r.signature_damaged and r.container == "avi" and r.recoverable
    # hdrl까지 지우면 헤더 소실
    hdrl = data.find(b"hdrl")
    data[0:hdrl + 4] = bytes(hdrl + 4)
    p2 = tmp_path / "head.avi"
    p2.write_bytes(bytes(data))
    r2 = scan_damage(str(p2))
    assert r2.header_missing and r2.needs_reference and r2.avi_idx1_pos is not None


def test_avi_idx1_cut_and_zeroed_chunk(tmp_path):
    data = _avi_bytes()
    cut = data.rfind(b"idx1")
    p = tmp_path / "cut.avi"
    p.write_bytes(data[:cut])
    r = scan_damage(str(p))
    assert r.index_missing and r.truncated and r.recoverable and r.media_range
    # 중간 청크 머리 손상 → 건너뛴 구간
    broken = bytearray(data)
    pos = broken.find(b"02tx", broken.find(b"02tx") + 4)
    broken[pos - 8:pos + 40] = bytes(48)
    p2 = tmp_path / "mid.avi"
    p2.write_bytes(bytes(broken))
    r2 = scan_damage(str(p2))
    assert r2.gaps, r2


def test_mp4_signature_resync_and_gap(tmp_path):
    data = _mp4_fragmented_bytes()
    p = tmp_path / "ok.mp4"
    p.write_bytes(data)
    assert not scan_damage(str(p)).damaged
    sig = bytearray(data)
    sig[4:8] = bytes(4)
    p2 = tmp_path / "sig.mp4"
    p2.write_bytes(bytes(sig))
    assert bb.detect_container(str(p2))[0] == "mp4"
    r = scan_damage(str(p2))
    assert r.signature_damaged and r.recoverable and r.fragmented
    # 세 번째 moof를 0으로 덮으면 건너뛴 구간이 생기고 그 뒤는 다시 읽힌다
    moofs = [i for i in range(len(data) - 4) if data[i:i + 4] == b"moof"]
    broken = bytearray(data)
    start = moofs[2] - 4
    broken[start:start + 16] = bytes(16)
    p3 = tmp_path / "gap.mp4"
    p3.write_bytes(bytes(broken))
    with open(p3, "rb") as f:
        boxes, boundary, kind = M.scan_top_level_bounded(f, len(broken))
    assert M.DAMAGE_GAPS and sum(1 for b in boxes if b.box_type == b"moof") == 5
    r3 = scan_damage(str(p3))
    assert r3.gaps and r3.recoverable


def test_moov_loss_without_fragments_marks_index_missing(tmp_path):
    data = _mp4_fragmented_bytes()
    moov = data.find(b"moov") - 4
    ftyp = data[:moov]
    rest = data[moov:]
    # moov를 떼고 moof도 없는 형태로 만든다: moov 자리를 free로 바꾸고 moof 이름을 깨뜨리면 조각 없는 파일처럼 보인다
    p = tmp_path / "nomoov.mp4"
    body = bytearray(rest)
    body[4:8] = b"free"
    p.write_bytes(ftyp + bytes(body))
    r = scan_damage(str(p))
    assert r.header_missing


def test_report_round_trip():
    r = DamageReport(path="x", container="avi", gaps=[[1, 2]], zero_regions=[[5, 9]], truncated=True)
    d = r.to_dict()
    back = DamageReport.from_dict(d)
    assert back.gaps == [[1, 2]] and back.truncated and back.damaged
    assert back.regions() == [[1, 2], [5, 9]]


# ---------------------------------------------------------------------------
# 시간축·구간·임의 복구
# ---------------------------------------------------------------------------

def test_estimate_times_from_utc_handles_midnight():
    pts = [_point(None, 1, 1, utc="23:59:58.00"), _point(None, 1, 1, utc="23:59:59.00"),
           _point(None, 1, 1, utc="00:00:01.00")]
    assert estimate_times_from_utc(pts)
    assert [p.start_time_sec for p in pts] == [0.0, 1.0, 3.0]
    assert all(p.time_estimated for p in pts)


def test_intervals_from_byte_regions():
    pts = [_point(float(t), 37.5, 127.0 + t * 1e-4, 40.0, offset=1000 * t) for t in range(20)]
    report = DamageReport(path="x", container="avi", gaps=[[5500, 9500]])
    ivs = compute_intervals(pts, report, 20.0)
    assert len(ivs) == 1 and ivs[0].start_sec == 5.0 and ivs[0].end_sec == 10.0


def test_interpolation_plausible_and_placeholders():
    pts = ([_point(float(t), 37.5, 127.0 + t * 1e-4, 36.0, offset=1000 * t) for t in range(5)]
           + [_point(float(t), 37.5, 127.0 + t * 1e-4, 36.0, offset=1000 * t) for t in range(10, 15)])
    iv = DamageInterval(4.0, 10.0, "gap")
    out = apply_damage_marks(list(pts), [iv], MODE_INTERPOLATE)
    interp = [p for p in out if p.is_interpolated]
    assert iv.outcome == "interpolated" and len(interp) == 5
    assert all(p.has_fix and p.speed_kmh is None for p in interp)
    assert interp[0].longitude > 127.0004 and interp[-1].longitude < 127.001
    # 복구하지 않음: 빈 행
    iv2 = DamageInterval(4.0, 10.0, "gap")
    out2 = apply_damage_marks(list(pts), [iv2], MODE_NONE)
    assert iv2.outcome == "not_recovered" and sum(p.is_damaged for p in out2) == 5
    assert all(not p.has_fix for p in out2 if p.is_damaged)


def test_interpolation_rejected_when_too_far():
    # 6초 동안 36 km/h면 60 m인데 두 좌표가 1 km 떨어져 있다 → 복구 불가
    pts = ([_point(float(t), 37.5, 127.0, 36.0, offset=1000 * t) for t in range(5)]
           + [_point(float(t), 37.5, 127.0113, 36.0, offset=1000 * t) for t in range(10, 15)])
    iv = DamageInterval(4.0, 10.0, "gap")
    out = apply_damage_marks(list(pts), [iv], MODE_INTERPOLATE)
    assert iv.outcome == "implausible" and iv.actual_m and iv.actual_m > 900
    assert not any(p.is_interpolated for p in out) and sum(p.is_damaged for p in out) == 5


def test_interpolation_uses_last_moving_speed_when_stopped():
    pts = ([_point(float(t), 37.5, 127.0 + t * 1e-4, 36.0, offset=1000 * t) for t in range(3)]
           + [_point(float(t), 37.5, 127.0002, 0.0, offset=1000 * t) for t in range(3, 5)]
           + [_point(float(t), 37.5, 127.0005, 36.0, offset=1000 * t) for t in range(10, 12)])
    iv = DamageInterval(4.0, 10.0, "gap")
    apply_damage_marks(list(pts), [iv], MODE_INTERPOLATE)
    assert iv.outcome == "interpolated" and iv.expected_m and iv.expected_m > 50


# ---------------------------------------------------------------------------
# 엔진 손상 허용 (합성 AVI를 실제 엔진으로)
# ---------------------------------------------------------------------------

def test_engine_rebuilds_index_without_idx1(tmp_path):
    import subprocess
    import sys
    import csv
    import glob
    data = _avi_bytes(seconds=12)
    p = tmp_path / "cut.avi"
    p.write_bytes(data[:data.rfind(b"idx1")])
    out = tmp_path / "out"
    engine = os.path.join(os.path.dirname(__file__), "..", "engine", "vendor", "integration_blackbox.py")
    r = subprocess.run([sys.executable, engine, "-o", str(out), str(p)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    timelines = glob.glob(str(out / "**" / "timeline.csv"), recursive=True)
    assert timelines
    rows = list(csv.DictReader(open(timelines[0], encoding="utf-8")))
    assert sum(1 for row in rows if row.get("latitude")) == 12
    assert "source_offset" in rows[0]
