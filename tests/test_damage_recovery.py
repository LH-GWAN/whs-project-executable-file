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
    """moof가 없는 일반 MP4에서 moov가 사라진 입력(리뷰 #124: 예전 테스트는 조각 파일이라 index_missing 경로를
    검증하지 못했다). 미디어 범위가 mdat 전체이고 그 범위 카빙으로 GPS가 전부 나와야 한다."""
    from core.damage_marks import carve_whole_file_gps
    data = _mp4_plain_bytes(30, faststart=False)
    moov = data.rfind(b"moov") - 4
    p = tmp_path / "nomoov.mp4"
    p.write_bytes(data[:moov])
    r = scan_damage(str(p))
    assert r.header_missing and r.index_missing and not r.fragmented and r.recoverable
    assert r.media_range == [data.find(b"mdat") + 4, moov] and not r.gaps
    assert len(carve_whole_file_gps(str(p), *r.media_range)) == 30
    # 조각 파일에서 moov만 빠진 경우는 index_missing이 아니다(헤더 이식 대상)
    frag = _mp4_fragmented_bytes()
    fm = frag.find(b"moof") - 4
    p2 = tmp_path / "nomoov_frag.mp4"
    p2.write_bytes(frag[:frag.find(b"moov") - 4] + frag[fm:])
    r2 = scan_damage(str(p2))
    assert r2.header_missing and r2.fragmented and not r2.index_missing


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
    # 손상 구간(바이트)과 겹치는 이웃 기록 사이에서 기록이 실제로 빠졌을 때(5초 → 10초)만 구간이 된다
    # (2026-10-11 리뷰 #15 반영으로 기대값 변경: 기록이 빠지지 않은 1초 간격은 구간이 아니다).
    pts = [_point(float(t), 37.5, 127.0 + t * 1e-4, 40.0, offset=1000 * t) for t in range(20) if not 5 < t < 10]
    report = DamageReport(path="x", container="avi", gaps=[[5500, 9500]])
    ivs = compute_intervals(pts, report, 20.0)
    assert len(ivs) == 1 and ivs[0].start_sec == 5.0 and ivs[0].end_sec == 10.0
    full = [_point(float(t), 37.5, 127.0 + t * 1e-4, 40.0, offset=1000 * t) for t in range(20)]
    assert compute_intervals(full, report, 20.0) == []


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


# ---------------------------------------------------------------------------
# 2026-10-11 리뷰 회귀 (#1 #2 #13 #18 #26 #28 #39 #50 #81 #83 #84 #85 #121)
# ---------------------------------------------------------------------------

import re  # noqa: E402

import core.damage as damage_mod  # noqa: E402
import core.damage_repair as repair_mod  # noqa: E402
from core.damage_repair import find_reference_candidates, transplant_header, validate_reference  # noqa: E402

ensure_vendor_importable()
import integration_avi as A  # noqa: E402


def _nmea_at(sec: int, lat=37.5, lon=127.0) -> bytes:
    mm, ss = divmod(sec, 60)
    return _nmea(ss, lat, lon + sec * 1e-4).replace(b"0824", f"08{24 + mm:02d}".encode(), 1)


def _avi_file(seconds=20, fps=1, video_streams=1, w=64, h=48, handler=b"MJPG", junk_in_movi=0,
              frame_size=1000, seed=0, gps=True, codec="mjpg") -> bytes:
    """영상 스트림 n개(NNdc) + GPS 텍스트(NNtx) AVI. codec: mjpg(JPEG SOI/SOF 포함) 또는 avc(NAL 머리)."""
    n_frames = seconds * fps
    strls = b""
    for _ in range(video_streams):
        strh_v = (b"vids" + handler + struct.pack("<IHHIIIIIIII", 0, 0, 0, 0, 1, fps, 0, n_frames, 0, 0, 0)
                  + struct.pack("<hhhh", 0, 0, w, h))
        strf_v = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, int.from_bytes(handler, "little"), w * h * 3, 0, 0, 0, 0)
        strls += _chunk(b"LIST", b"strl" + _chunk(b"strh", strh_v) + _chunk(b"strf", strf_v))
    strh_t = (b"txts" + b"\0\0\0\0" + struct.pack("<IHHIIIIIIII", 0, 0, 0, 0, 1, 1, 0, seconds, 0, 0, 0)
              + struct.pack("<hhhh", 0, 0, 0, 0))
    strls += _chunk(b"LIST", b"strl" + _chunk(b"strh", strh_t) + _chunk(b"strf", b"\0" * 8))
    avih = struct.pack("<14I", 1_000_000 // fps, 0, 0, 0x10, n_frames, 0, video_streams + 1, 0, w, h, 0, 0, 0, 0)
    hdrl = _chunk(b"LIST", b"hdrl" + _chunk(b"avih", avih) + strls)
    movi = bytearray(b"movi")
    entries = []
    tx_id = f"{video_streams:02d}tx".encode()
    if codec == "mjpg":
        sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
        head = b"\xff\xd8" + sof
    else:
        head = b"\0\0\0\1\x67\x64\x00\x29"
    for sec in range(seconds):
        for k in range(fps):
            for vs in range(video_streams):
                cid = f"{vs:02d}dc".encode()
                # 프레임 크기를 조금씩 다르게 둔다(실제 영상처럼). 전부 같으면 base 역산이 주기만큼 어긋나도 맞아 버린다.
                frame = head + bytes([(sec + seed + vs) & 0xFF]) * (frame_size + k + (sec * 37 + vs * 11) % 97) + b"\xff\xd9"
                entries.append((cid, len(movi), len(frame)))
                movi += _chunk(cid, frame)
        if gps:
            text = _nmea_at(sec) + b"\r\n"
            entries.append((tx_id, len(movi), len(text)))
            movi += _chunk(tx_id, text)
        if junk_in_movi and sec == seconds // 2:
            movi += _chunk(b"JUNK", bytes(junk_in_movi))
    idx1 = _chunk(b"idx1", b"".join(cid + struct.pack("<III", 0x10, off, size) for cid, off, size in entries))
    body = b"AVI " + hdrl + _chunk(b"LIST", bytes(movi)) + idx1
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _kill_hdrl(data: bytes) -> bytes:
    d = bytearray(data)
    hdrl = d.find(b"hdrl")
    d[0:hdrl + 4] = bytes(hdrl + 4)
    return bytes(d)


def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _full(kind, payload, version=0, flags=0):
    return _box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def _mp4_plain_bytes(n=30, faststart=False, lat=37.5) -> bytes:
    """moov(샘플 표)가 있는 일반 MP4: 텍스트 트랙 샘플 n개. faststart면 moov가 mdat 앞."""
    samples = [struct.pack(">H", len(s)) + s for s in (_nmea_at(i, lat=lat) + b"\r\n" for i in range(n))]
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    mdat = _box(b"mdat", b"".join(samples))

    def moov(offs):
        mvhd = _full(b"mvhd", struct.pack(">IIII", 0, 0, 1000, n * 1000) + bytes(80))
        tkhd = _full(b"tkhd", struct.pack(">IIIII", 0, 0, 1, 0, n * 1000) + bytes(60), flags=7)
        mdhd = _full(b"mdhd", struct.pack(">IIIIHH", 0, 0, 1000, n * 1000, 0, 0))
        hdlr = _full(b"hdlr", bytes(4) + b"text" + bytes(12) + b"text\0")
        stsd = _full(b"stsd", struct.pack(">I", 1) + _box(b"tx3g", bytes(6) + struct.pack(">H", 1) + bytes(30)))
        stts = _full(b"stts", struct.pack(">I", 1) + struct.pack(">II", n, 1000))
        stsc = _full(b"stsc", struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1))
        stsz = _full(b"stsz", struct.pack(">II", 0, n) + b"".join(struct.pack(">I", len(s)) for s in samples))
        stco = _full(b"stco", struct.pack(">I", n) + b"".join(struct.pack(">I", o) for o in offs))
        stbl = _box(b"stbl", stsd + stts + stsc + stsz + stco)
        dinf = _box(b"dinf", _full(b"dref", struct.pack(">I", 1) + _full(b"url ", b"", flags=1)))
        minf = _box(b"minf", _full(b"nmhd", b"") + dinf + stbl)
        return _box(b"moov", mvhd + _box(b"trak", tkhd + _box(b"mdia", mdhd + hdlr + minf)))

    moov_len = len(moov([0] * n))
    mdat_payload_start = len(ftyp) + (moov_len if faststart else 0) + 8
    offs = []
    pos = mdat_payload_start
    for s in samples:
        offs.append(pos)
        pos += len(s)
    return ftyp + (moov(offs) + mdat if faststart else mdat + moov(offs))


def _mp4_hybrid_bytes(n_moov=3, n_frag=10, lat=37.5, utc_min=24) -> bytes:
    """moov 샘플 표에 n_moov개 + 첫 moof 앞 mdat + moof/mdat 조각 n_frag개 (리뷰 #1의 혼합 fMP4)."""
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso6")
    first = [_nmea_at(s, lat=lat).replace(b"0824", f"08{utc_min:02d}".encode(), 1) + b"\r\n" for s in range(n_moov)]

    def mk_moov(offs):
        mvhd = _full(b"mvhd", struct.pack(">IIII", 0, 0, 1000, (n_moov + n_frag) * 1000) + bytes(80))
        tkhd = _full(b"tkhd", struct.pack(">IIII", 0, 0, 3, 0) + bytes(76), flags=7)
        mdhd = _full(b"mdhd", struct.pack(">IIIIHH", 0, 0, 1000, (n_moov + n_frag) * 1000, 0, 0))
        hdlr = _full(b"hdlr", bytes(4) + b"text" + bytes(12) + b"text\0")
        stsd = _full(b"stsd", struct.pack(">I", 1) + _box(b"tx3g", bytes(6) + struct.pack(">H", 1) + bytes(30)))
        stts = _full(b"stts", struct.pack(">I", 1 if n_moov else 0) + (struct.pack(">II", n_moov, 1000) if n_moov else b""))
        stsc = _full(b"stsc", struct.pack(">I", 1 if n_moov else 0) + (struct.pack(">III", 1, 1, 1) if n_moov else b""))
        stsz = _full(b"stsz", struct.pack(">II", 0, n_moov) + b"".join(struct.pack(">I", len(s)) for s in first))
        stco = _full(b"stco", struct.pack(">I", n_moov) + b"".join(struct.pack(">I", o) for o in offs))
        stbl = _box(b"stbl", stsd + stts + stsc + stsz + stco)
        dinf = _box(b"dinf", _full(b"dref", struct.pack(">I", 1) + _full(b"url ", b"", flags=1)))
        minf = _box(b"minf", _full(b"nmhd", b"") + dinf + stbl)
        trak = _box(b"trak", tkhd + _box(b"mdia", mdhd + hdlr + minf))
        trex = _full(b"trex", struct.pack(">IIIII", 3, 1, 1000, 0, 0))
        mehd = _full(b"mehd", struct.pack(">I", (n_moov + n_frag) * 1000))
        return _box(b"moov", mvhd + trak + _box(b"mvex", mehd + trex))

    p = len(ftyp) + len(mk_moov([0] * n_moov)) + 8
    offs = []
    for s in first:
        offs.append(p)
        p += len(s)
    out = ftyp + mk_moov(offs)
    if n_moov:
        out += _box(b"mdat", b"".join(first))
    for n in range(n_frag):
        k = n_moov + n
        sample = _nmea_at(k, lat=lat).replace(b"0824", f"08{utc_min:02d}".encode(), 1) + b"\r\n"
        tfhd = _full(b"tfhd", struct.pack(">I", 3), flags=0x020000)
        tfdt = _full(b"tfdt", struct.pack(">I", k * 1000))
        tmp = _box(b"traf", tfhd + tfdt + _full(b"trun", struct.pack(">II", 1, 0) + struct.pack(">II", 1000, len(sample)), flags=0x301))
        msz = 8 + 16 + len(tmp)
        trun = _full(b"trun", struct.pack(">Ii", 1, msz + 8) + struct.pack(">II", 1000, len(sample)), flags=0x301)
        out += _box(b"moof", _full(b"mfhd", struct.pack(">I", n + 1)) + _box(b"traf", tfhd + tfdt + trun)) + _box(b"mdat", sample)
    return out


def _mp4_durations(data: bytes) -> dict:
    """mvhd/tkhd/mdhd/mehd의 duration 값."""
    out = {}
    for name, off in ((b"mvhd", 16), (b"tkhd", 20), (b"mdhd", 16), (b"mehd", 4)):
        i = data.find(name)
        if i >= 0:
            out[name.decode()] = struct.unpack_from(">I", data, i + 4 + off)[0]
    return out


# ---- #83 read() 기반 검사 ----------------------------------------------------

def test_scan_uses_read_not_mmap_and_media_errors_become_exceptions(tmp_path, monkeypatch):
    import inspect
    assert not re.search(r"^\s*import mmap", inspect.getsource(damage_mod), re.M)
    assert not re.search(r"^\s*import mmap", inspect.getsource(repair_mod), re.M)
    p = tmp_path / "ok.avi"
    p.write_bytes(_avi_file(seconds=30))
    assert not scan_damage(str(p)).damaged
    real = damage_mod._read_at

    def failing(f, pos, n):
        if pos > 2000:
            raise OSError(5, "Input/output error")
        return real(f, pos, n)
    monkeypatch.setattr(damage_mod, "_read_at", failing)
    with pytest.raises(OSError):
        scan_damage(str(p))


def _avi_rec_file(seconds=20, fps=3, rec_entries=True, with_ix=False, sig_damaged=False) -> bytes:
    """OpenDML식 'LIST rec ' 그룹(초마다 하나)에 영상·GPS 청크를 넣은 AVI(리뷰 #16 재현). idx1에는 rec 엔트리
    (AVIIF_LIST)를 넣거나 뺀다. with_ix면 그룹 뒤에 ix00 표준 색인 청크도 둔다."""
    strh_v = (b"vids" + b"MJPG" + struct.pack("<IHHIIIIIIII", 0, 0, 0, 0, 1, fps, 0, seconds * fps, 0, 0, 0)
              + struct.pack("<hhhh", 0, 0, 64, 48))
    strf_v = struct.pack("<IiiHHIIiiII", 40, 64, 48, 1, 24, 0x47504A4D, 64 * 48 * 3, 0, 0, 0, 0)
    strl_v = _chunk(b"LIST", b"strl" + _chunk(b"strh", strh_v) + _chunk(b"strf", strf_v))
    strh_t = (b"txts" + b"\0\0\0\0" + struct.pack("<IHHIIIIIIII", 0, 0, 0, 0, 1, 1, 0, seconds, 0, 0, 0)
              + struct.pack("<hhhh", 0, 0, 0, 0))
    strl_t = _chunk(b"LIST", b"strl" + _chunk(b"strh", strh_t) + _chunk(b"strf", b"\0" * 8))
    avih = struct.pack("<14I", 1_000_000 // fps, 0, 0, 0x10, seconds * fps, 0, 2, 0, 64, 48, 0, 0, 0, 0)
    hdrl = _chunk(b"LIST", b"hdrl" + _chunk(b"avih", avih) + strl_v + strl_t)
    movi = bytearray(b"movi")
    entries = []
    for sec in range(seconds):
        rec_off = len(movi)
        inner = bytearray(b"rec ")
        sub = []
        for k in range(fps):
            frame = b"\xff\xd8\xff" + bytes([sec]) * (1000 + sec + k) + b"\xff\xd9"
            sub.append((b"00dc", rec_off + 8 + len(inner), len(frame)))
            inner += _chunk(b"00dc", frame)
        text = _nmea_at(sec) + b"\r\n"
        sub.append((b"01tx", rec_off + 8 + len(inner), len(text)))
        inner += _chunk(b"01tx", text)
        if rec_entries:
            entries.append((b"rec ", 0x01, rec_off, len(inner)))
        entries.extend((cid, 0x10, off, ln) for cid, off, ln in sub)
        movi += _chunk(b"LIST", bytes(inner))
        if with_ix and sec == seconds // 2:
            movi += _chunk(b"ix00", bytes(64))
    idx1 = _chunk(b"idx1", b"".join(cid + struct.pack("<III", fl, off, size) for cid, fl, off, size in entries))
    body = b"AVI " + hdrl + _chunk(b"LIST", bytes(movi)) + idx1
    data = bytearray(b"RIFF" + struct.pack("<I", len(body)) + body)
    if sig_damaged:
        data[0:8] = bytes(8)
    return bytes(data)


def _avi_no_idx1(seconds=20, fps=3, tail_zeros=0, cut_last_chunk=False, garbage_tail=0) -> bytes:
    """idx1 없는(녹화 중단) AVI: movi 뒤에 0 꼬리(사전 할당 미사용) 또는 쓰레기 꼬리, 또는 마지막 청크 절단."""
    data = _avi_file(seconds=seconds, fps=fps, frame_size=900)
    movi_pos = data.find(b"LIST", data.find(b"hdrl")) if False else None
    body = bytearray(data[:data.rfind(b"idx1")])
    # movi LIST 크기를 꼬리까지 포함하도록 다시 쓴다
    lp = body.find(b"movi") - 8
    if cut_last_chunk:
        body = body[:-500]
    body += bytes(tail_zeros) + bytes([0xA7]) * garbage_tail
    struct.pack_into("<I", body, lp + 4, len(body) - lp - 8)
    struct.pack_into("<I", body, 4, len(body) - 8)
    return bytes(body)


def _walk_scenarios():
    out = {}
    d = bytearray(_avi_file(seconds=40, fps=3, frame_size=700, junk_in_movi=9000))
    movi = d.find(b"movi")
    d[movi + 20000:movi + 29000] = bytes(9000)                 # 창 두 개를 넘는 0 구간
    d[movi + 60000:movi + 60300] = b"\xa5" * 300               # 짧은 쓰레기 구간
    out["gaps_junk"] = bytes(d)
    out["rec_groups"] = _avi_rec_file(seconds=30, fps=4, with_ix=True)
    out["zero_tail"] = _avi_no_idx1(tail_zeros=300 * 1024)
    out["truncated_tail"] = _avi_no_idx1(cut_last_chunk=True)
    out["garbage_tail"] = _avi_no_idx1(garbage_tail=20000)
    d = bytearray(_avi_file(seconds=120, fps=2, frame_size=1500))
    movi = d.find(b"movi")
    d[movi + 50000:movi + 50000 + 200 * 1024] = bytes(200 * 1024)   # 창 50개를 넘는 0 손상
    out["long_zero"] = bytes(d)
    d = bytearray(_avi_rec_file(seconds=40, fps=5))
    movi = d.find(b"movi")
    d[movi + 30000:movi + 30000 + 12000] = bytes(12000)        # rec 그룹 중간이 0으로
    out["rec_damaged"] = bytes(d)
    return out


@pytest.mark.parametrize("name", list(_walk_scenarios().keys()))
@pytest.mark.parametrize("window", [4096, 777])
def test_windowed_movi_walk_matches_engine_walk(tmp_path, monkeypatch, name, window):
    """창 단위 걷기가 엔진 walk_movi_chunks(전체 버퍼)와 같은 청크·손상 구간·꼬리 판정을 내야 한다: 창 경계에
    걸친 청크, 창보다 큰 청크, 창을 넘는 0 구간, rec 그룹·ix 청크, 0 꼬리, 잘린 꼬리, 쓰레기 꼬리, 홀수 패딩."""
    monkeypatch.setattr(damage_mod, "_WINDOW", window)
    data = _walk_scenarios()[name]
    movi = data.find(b"movi")
    idx1 = data.rfind(b"idx1")
    end = idx1 if idx1 > movi else len(data)
    gaps_engine, gaps_mine = [], []
    tail_engine, tail_mine = {}, {}
    engine = list(A.walk_movi_chunks(data, movi + 4, end, gaps_engine, tail_info=tail_engine))
    A.WARNINGS.clear()
    p = tmp_path / f"{name}.avi"
    p.write_bytes(data)
    with open(p, "rb") as f:
        mine = list(damage_mod.walk_movi_chunks(f, movi + 4, end, gaps_mine, None, tail_mine))
    assert mine == engine and len(mine) > 50
    assert sorted(gaps_mine) == sorted(gaps_engine)
    assert tail_mine == tail_engine
    if name == "zero_tail":
        assert tail_engine.get("zero_tail") and not gaps_engine
    if name == "truncated_tail":
        assert tail_engine.get("truncated_tail") is not None and not gaps_engine
    if name == "garbage_tail":
        assert gaps_engine and gaps_engine[-1][1] == end
    if name in ("long_zero", "gaps_junk", "rec_damaged"):
        assert gaps_engine


def _cut_scenarios():
    """(이름, 파일 바이트, 기대 cut 여부). GPS 앵커(체크섬 OK·A·날짜) 연속성 규칙 비교용."""
    out = []
    base = _avi_file(seconds=60, fps=2, frame_size=1200)
    movi = base.find(b"movi")
    end = base.rfind(b"idx1")
    # (a) 중간 30 KB 0 덮임, 뒤 GPS 이어짐 → 자르지 않음
    d = bytearray(base[:end]); d[movi + 40000:movi + 70000] = bytes(30000)
    out.append(("zero_continuous", bytes(d)))
    # (b) 손상 뒤 GPS가 과거(옛 녹화 잔재) → 경계
    old = _avi_file(seconds=60, fps=2, frame_size=1200)
    old_movi = old.find(b"movi")
    d = bytearray(base[:movi + 40000]) + bytes(5000) + old[old_movi + 4 + 2000:old.rfind(b"idx1")]
    out.append(("past_after_gap", bytes(d)))
    # (c) 앞뒤에 근거 RMC가 없고(V 상태) 쓰레기로 덮임 → 경계 불확실
    nov = base[:end].replace(b",A,", b",V,")
    d = bytearray(nov); d[movi + 40000:movi + 50000] = b"\x5a" * 10000
    out.append(("no_anchor_garbage", bytes(d)))
    # (d) 350초 분량이 0으로 덮였지만 손상 바이트로 설명되는 시간 → 같은 녹화
    long = _avi_file(seconds=400, fps=1, frame_size=1000)
    lm = long.find(b"movi"); le = long.rfind(b"idx1")
    d = bytearray(long[:le]); d[lm + 20000:lm + 20000 + 350 * 1070] = bytes(350 * 1070)
    out.append(("long_zero_same_recording", bytes(d)))
    return out


@pytest.mark.parametrize("name,data", _cut_scenarios(), ids=[n for n, _ in _cut_scenarios()])   # 바이트를 id로 쓰면 Windows tmp_path 이름이 깨진다
def test_recording_cut_matches_engine(tmp_path, name, data):
    movi = data.find(b"movi")
    gaps_e: list = []
    walked = [w for w in A.walk_movi_chunks(data, movi + 4, len(data), gaps_e) if w[1] != b"JUNK"]
    info_e: dict = {}
    cut_e = A.recording_cut_at(data, walked, sorted(gaps_e), info=info_e)
    A.WARNINGS.clear()
    p = tmp_path / f"{name}.avi"
    p.write_bytes(data)
    with open(p, "rb") as f:
        gaps_m: list = []
        walked_m = [w for w in damage_mod.walk_movi_chunks(f, movi + 4, len(data), gaps_m) if w[1] != b"JUNK"]
        info_m: dict = {}
        cut_m = damage_mod.recording_cut_at(f, len(data), walked_m, sorted(gaps_m), info_m)
    assert walked_m == walked and sorted(gaps_m) == sorted(gaps_e)
    assert cut_m == cut_e and info_m.get("uncertain") == info_e.get("uncertain")
    expect = {"zero_continuous": None, "long_zero_same_recording": None}
    if name in expect:
        assert cut_e is None
    else:
        assert cut_e is not None
    if name == "no_anchor_garbage":
        assert info_e.get("uncertain") is True
    r = scan_damage(str(p))
    assert r.index_missing
    if name == "no_anchor_garbage":
        assert "경계 불확실" in r.media_end_basis
    if name == "past_after_gap":
        assert r.media_range[1] == cut_e and r.gaps == []
    if name == "long_zero_same_recording":
        assert r.media_range[1] > movi + 20000 + 350 * 1070 and r.gaps


def test_idx1_fallback_and_chain_match_engine(tmp_path):
    """앞 8 KB가 0으로 덮인 파일 + 뒤에 옛 녹화 AVI(자기 idx1 포함)가 통째로 붙은 사전 할당 파일: 현재 녹화의
    idx1만 받아야 한다(엔진 find_idx1_fallback·first_chunk_chain과 같은 결과)."""
    cur = _avi_file(seconds=30, fps=2, frame_size=1500)
    old = _avi_file(seconds=50, fps=2, frame_size=1500, seed=9)
    d = bytearray(cur + old)
    d[0:8192] = bytes(8192)
    data = bytes(d)
    chain_e = A.first_chunk_chain(data)
    fb_e = A.find_idx1_fallback(data, chain_start=chain_e)
    h_e, m_e, i_e, _ = A.find_top_level_sections(data)
    A.WARNINGS.clear()
    p = tmp_path / "prealloc.avi"
    p.write_bytes(data)
    with open(p, "rb") as f:
        chain_m = damage_mod.first_chunk_chain(f, len(data))
        fb_m = damage_mod._find_idx1(f, len(data), chain_start=chain_m)
        h_m, m_m, i_m, _, _ = damage_mod.avi_sections(f, len(data))
    assert chain_m == chain_e and fb_m == fb_e
    assert fb_e is not None and fb_e[0] == cur.rfind(b"idx1")          # 옛 idx1(뒤쪽)이 아니라 현재 것
    assert (i_m.pos if i_m else None) == (i_e.pos if i_e else None)
    assert (m_m.data_start if m_m else None) == (m_e[0].data_start if m_e else None)   # 엔진은 movi LIST 목록
    assert h_m is None and h_e is None                                   # movi 뒤의 옛 hdrl은 헤더가 아니다
    r = scan_damage(str(p))
    assert r.header_missing and not r.index_missing and r.media_range[1] <= cur.rfind(b"idx1")


def test_rec_groups_and_ix_are_normal_structure(tmp_path):
    for name, data in (("rec_entries", _avi_rec_file(rec_entries=True, with_ix=True)),
                       ("no_rec_entries", _avi_rec_file(rec_entries=False)),
                       ("rec_sig_damaged", _avi_rec_file(rec_entries=True, sig_damaged=True))):
        p = tmp_path / f"{name}.avi"
        p.write_bytes(data)
        r = scan_damage(str(p))
        assert not r.gaps and not r.index_missing and not r.index_damaged, (name, r)
        assert r.damaged == (name == "rec_sig_damaged")


def test_zero_tail_without_idx1_is_not_damage(tmp_path):
    p = tmp_path / "prealloc_cut.avi"
    data = _avi_no_idx1(seconds=20, fps=3, tail_zeros=2 * 1024 * 1024)
    p.write_bytes(data)
    r = scan_damage(str(p))
    assert r.index_missing and not r.gaps and not r.zero_regions and not r.truncated
    assert r.media_range[1] < len(data) - 2 * 1024 * 1024 + 16        # 마지막 실제 청크 끝
    p2 = tmp_path / "cut_mid_chunk.avi"
    p2.write_bytes(_avi_no_idx1(seconds=20, fps=3, cut_last_chunk=True))
    r2 = scan_damage(str(p2))
    assert r2.index_missing and r2.truncated and not r2.gaps


def test_mp4_scan_cancel_and_tail_residue(tmp_path):
    import threading
    data = _mp4_fragmented_bytes(8)
    p = tmp_path / "ok.mp4"
    p.write_bytes(data)
    ev = threading.Event()
    ev.set()
    with pytest.raises(InterruptedError):
        scan_damage(str(p), cancel_event=ev)
    # 마지막 Box 뒤에 재동기화되지 않는 0이 아닌 잔재 → '파일 끝 잔재'로 구분
    p2 = tmp_path / "residue.mp4"
    p2.write_bytes(data + bytes([0xB6]) * 70000)
    r = scan_damage(str(p2))
    assert r.tail_residue == [len(data), len(data) + 70000]
    text = " ".join(r.summary_lines())
    assert "파일 끝 잔재" in text and "구조를 읽을 수 없는 구간" not in text


# ---- #84 #26 #28 #18 #81 #121 검사 판정 -------------------------------------

def test_junk_chunk_payload_is_not_zero_damage(tmp_path):
    p = tmp_path / "junk.avi"
    p.write_bytes(_avi_file(seconds=10, junk_in_movi=512 * 1024))
    r = scan_damage(str(p))
    assert not r.zero_regions and not r.gaps and not r.damaged


def test_idx1_tail_and_movi_tail_damage_detected(tmp_path):
    data = _avi_file(seconds=60, fps=5, frame_size=1500)
    idx1 = data.rfind(b"idx1")
    # idx1 뒤쪽 2/3을 0으로: 색인 손상 → 색인이 못 가리키는 뒷부분이 손상 구간
    d1 = bytearray(data)
    n_entries = (len(data) - idx1 - 8) // 16
    cut = idx1 + 8 + (n_entries // 3) * 16
    d1[cut:] = bytes(len(d1) - cut)
    p1 = tmp_path / "idx_tail.avi"
    p1.write_bytes(bytes(d1))
    r1 = scan_damage(str(p1))
    # 엔진(2026-10-11 #7)은 표본 엔트리가 깨진 idx1을 현재 녹화의 색인으로 받지 않고 movi를 걸어 다시 만든다.
    # 검사도 같은 규칙: idx1 소실(재구성)로 판정하고 미디어 범위는 걸어 읽은 끝까지다.
    assert r1.damaged and r1.index_missing and not r1.gaps
    assert r1.media_range[1] >= idx1 - 1
    # movi 꼬리 300 KB를 0으로(idx1은 온전) → 손상 구간/0 구간이 꼬리까지 이어져야 한다
    d2 = bytearray(data)
    d2[idx1 - 300 * 1024:idx1] = bytes(300 * 1024)
    p2 = tmp_path / "movi_tail.avi"
    p2.write_bytes(bytes(d2))
    r2 = scan_damage(str(p2))
    assert r2.damaged and not r2.index_damaged
    assert r2.regions() and r2.regions()[-1][1] >= idx1 - 1
    # 온전한 파일(사전 할당 잔재 없음)은 그대로 정상
    assert not scan_damage(str(tmp_path / "ok.avi")).damaged if (tmp_path / "ok.avi").write_bytes(data) else True


def test_header_content_damage_detected(tmp_path):
    # AVI: hdrl LIST 태그는 남기고 내용만 0
    data = _avi_file(seconds=10)
    h = data.find(b"hdrl")
    lsize = struct.unpack("<I", data[h - 4:h])[0]
    d = bytearray(data)
    d[h + 4:h + lsize] = bytes(lsize - 4)
    p = tmp_path / "hdrl0.avi"
    p.write_bytes(bytes(d))
    r = scan_damage(str(p))
    assert r.header_missing and r.header_damaged and r.needs_reference and r.avi_idx1_pos is not None
    assert "내용이 손상" in " ".join(r.summary_lines())
    # fMP4: moov 머리는 남기고 내용만 0
    frag = _mp4_fragmented_bytes(8)
    m = frag.find(b"moov") - 4
    msize = struct.unpack(">I", frag[m:m + 4])[0]
    d2 = bytearray(frag)
    d2[m + 8:m + msize] = bytes(msize - 8)
    p2 = tmp_path / "moov0.mp4"
    p2.write_bytes(bytes(d2))
    r2 = scan_damage(str(p2))
    assert r2.header_missing and r2.header_damaged and r2.fragmented and not r2.index_missing
    # 일반 MP4: 끝에 있는 moov의 마지막 200바이트(stco 꼬리)가 0 → 샘플 표 손상
    plain = _mp4_plain_bytes(60)
    d3 = bytearray(plain)
    d3[-200:] = bytes(200)
    p3 = tmp_path / "moov_tail.mp4"
    p3.write_bytes(bytes(d3))
    r3 = scan_damage(str(p3))
    assert r3.header_missing and r3.header_damaged and r3.index_missing
    assert not scan_damage(str(tmp_path / "plain.mp4")).damaged if (tmp_path / "plain.mp4").write_bytes(plain) else True


@pytest.mark.parametrize("kind", ["size8", "size0", "size1_large0", "size1_qmarks", "qmarks32", "truncated", "head_broken"])
def test_mdat_placeholder_size_keeps_media_range(tmp_path, kind):
    from core.damage_marks import carve_whole_file_gps
    import random
    rng = random.Random(1)
    filler = lambda n: bytes(rng.randrange(0x80, 0x100) for _ in range(n))  # noqa: E731
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2avc1mp41")
    payload = b"".join(filler(2000) + _nmea_at(s) + b"\r\n" for s in range(30)) + filler(2000)
    head = {
        "size8": struct.pack(">I", 8) + b"mdat",
        "size0": struct.pack(">I", 0) + b"mdat",
        "size1_large0": struct.pack(">I", 1) + b"mdat" + bytes(8),
        "size1_qmarks": struct.pack(">I", 1) + b"mdat" + b"????????",
        "qmarks32": b"????mdat",
        "truncated": struct.pack(">I", 8 + len(payload) + 50000) + b"mdat",
        "head_broken": bytes(8),
    }[kind]
    p = tmp_path / f"{kind}.mp4"
    p.write_bytes(ftyp + head + payload)
    r = scan_damage(str(p))
    assert r.recoverable and r.header_missing and r.index_missing, r
    assert r.media_range and r.media_range[1] == len(ftyp) + len(head) + len(payload)
    assert r.media_range[0] <= len(ftyp) + len(head)
    assert not r.gaps
    assert len(carve_whole_file_gps(str(p), *r.media_range)) == 30


def test_faststart_mdat_head_zero_detected(tmp_path):
    data = _mp4_plain_bytes(6000, faststart=True)
    moov_end = data.find(b"mdat") - 4
    d = bytearray(data)
    d[moov_end:moov_end + 300 * 1024] = bytes(300 * 1024)
    assert len(d) == len(data)
    p = tmp_path / "fast0.mp4"
    p.write_bytes(bytes(d))
    r = scan_damage(str(p))
    assert r.damaged and not r.header_missing
    assert r.media_range == [moov_end, len(data)]
    assert r.zero_regions and r.zero_regions[0][0] >= moov_end


def test_mp4_summary_has_no_idx1_wording():
    r = DamageReport(path="x", container="mp4", header_missing=True, index_missing=True)
    text = " ".join(r.summary_lines())
    assert "idx1" not in text and "moov" in text
    r2 = DamageReport(path="x", container="avi", index_missing=True)
    assert "idx1" in " ".join(r2.summary_lines())


# ---- #13 #39 참조 선택·검증 ---------------------------------------------------

def test_reference_candidates_require_same_channel_and_validate_settings(tmp_path):
    good = _avi_file(seconds=10, fps=2, w=64, h=48)
    (tmp_path / "REC_20240916_172336_F.avi").write_bytes(_avi_file(seconds=10, fps=2, w=64, h=48, seed=5))
    (tmp_path / "REC_20240916_172436_R.avi").write_bytes(_avi_file(seconds=10, fps=1, w=32, h=24))
    (tmp_path / "REC_20240916_172536_F.avi").write_bytes(_avi_file(seconds=10, fps=4, w=64, h=48))
    (tmp_path / "REC_20240916_172636_F.avi").write_bytes(_avi_file(seconds=10, fps=2, w=64, h=48, handler=b"H264", codec="avc"))
    dmg = tmp_path / "REC_20240916_172436_F.avi"
    dmg.write_bytes(_kill_hdrl(good))
    r = scan_damage(str(dmg))
    cands = find_reference_candidates(str(dmg), r)
    assert [os.path.basename(c) for c in cands] == ["REC_20240916_172336_F.avi"]
    ok, why = validate_reference(r, str(tmp_path / "REC_20240916_172436_R.avi"))
    assert not ok and "FPS" in why
    ok, why = validate_reference(r, str(tmp_path / "REC_20240916_172536_F.avi"))
    assert not ok and "FPS" in why
    ok, why = validate_reference(r, str(tmp_path / "REC_20240916_172636_F.avi"))
    assert not ok and "코덱" in why
    ok, why = validate_reference(r, str(tmp_path / "REC_20240916_172336_F.avi"))
    assert ok and "FPS 2.00" in why and "MJPG" in why
    # 해상도만 다른 MJPG 참조
    (tmp_path / "REC_20240916_172736_F.avi").write_bytes(_avi_file(seconds=10, fps=2, w=128, h=96))
    ok, why = validate_reference(r, str(tmp_path / "REC_20240916_172736_F.avi"))
    assert not ok and "해상도" in why


def test_validate_reference_rejects_self_and_broken_header_and_allows_junk(tmp_path):
    dmg = tmp_path / "REC_20240916_172436_F.avi"
    dmg.write_bytes(_kill_hdrl(_avi_file(seconds=10, fps=2, junk_in_movi=4000)))
    r = scan_damage(str(dmg))
    ok, why = validate_reference(r, str(dmg))
    assert not ok and "자신" in why
    broken = tmp_path / "REC_20240916_172536_F.avi"
    broken.write_bytes(_kill_hdrl(_avi_file(seconds=10, fps=2)))
    ok, why = validate_reference(r, str(broken))
    assert not ok
    ref = tmp_path / "REC_20240916_172336_F.avi"
    ref.write_bytes(_avi_file(seconds=10, fps=2, junk_in_movi=4000))
    ok, why = validate_reference(r, str(ref))
    assert ok, why
    with pytest.raises(ValueError):
        transplant_header(r, str(dmg), str(tmp_path / "x.avi"))
    assert not (tmp_path / "x.avi").exists()


# ---- #2 #50 #85 AVI 이식 ------------------------------------------------------

def test_avi_transplant_sets_frames_per_stream(tmp_path):
    two = _avi_file(seconds=20, fps=2, video_streams=2)
    ref = tmp_path / "REC_20240916_172336_F.avi"
    ref.write_bytes(_avi_file(seconds=60, fps=2, video_streams=2, seed=3))
    dmg = tmp_path / "REC_20240916_172436_F.avi"
    dmg.write_bytes(_kill_hdrl(two))
    r = scan_damage(str(dmg))
    out = tmp_path / "rep.avi"
    note = transplant_header(r, str(ref), str(out))
    data = out.read_bytes()
    lengths = [struct.unpack_from("<I", data, m.start() + 8 + 32)[0] for m in re.finditer(rb"strh", data)
               if data[m.start() + 8:m.start() + 12] == b"vids"]
    assert lengths == [40, 40] and "스트림 0 40개, 스트림 1 40개" in note
    avih = data.find(b"avih")
    assert struct.unpack_from("<I", data, avih + 8 + 16)[0] == 40
    # idx1이 없고 중간이 0으로 덮인 손상 파일: 걸어 읽은 청크 + 빈 자리 추정으로 원래 프레임 수에 가깝게
    one = _avi_file(seconds=60, fps=2)
    cut = bytearray(one[:one.rfind(b"idx1")])
    movi = cut.find(b"movi")
    cut[movi + 20000:movi + 30000] = bytes(10000)
    dmg2 = tmp_path / "REC_20240916_172636_F.avi"
    dmg2.write_bytes(bytes(cut))
    ref2 = tmp_path / "REC_20240916_172536_F.avi"
    ref2.write_bytes(_avi_file(seconds=30, fps=2, seed=1))
    r2 = scan_damage(str(dmg2))
    assert r2.index_missing and r2.gaps
    out2 = tmp_path / "rep2.avi"
    note2 = transplant_header(r2, str(ref2), str(out2))
    data2 = out2.read_bytes()
    strh = data2.find(b"strh")
    frames = struct.unpack_from("<I", data2, strh + 8 + 32)[0]
    assert 110 <= frames <= 125 and "빈 자리" in note2


def test_avi_transplant_streams_in_blocks_preserves_tail_and_cancels(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(repair_mod, "_COPY_BLOCK", 4096)
    good = _avi_file(seconds=20, fps=2, frame_size=3000)
    tail = b"RIFF" + struct.pack("<I", 100) + b"AVI " + b"\xcc" * 5000     # idx1 뒤 옛 녹화 잔재(슬랙)
    dmg = tmp_path / "REC_20240916_172436_F.avi"
    dmg.write_bytes(_kill_hdrl(good) + tail)
    ref = tmp_path / "REC_20240916_172336_F.avi"
    ref.write_bytes(_avi_file(seconds=30, fps=2, seed=2))
    r = scan_damage(str(dmg))
    out = tmp_path / "rep.avi"
    transplant_header(r, str(ref), str(out))
    data = out.read_bytes()
    assert data.endswith(tail)
    idx1 = data.rfind(b"idx1", 0, len(data) - len(tail))
    idx_size = struct.unpack_from("<I", data, idx1 + 4)[0]
    assert struct.unpack_from("<I", data, 4)[0] + 8 == idx1 + 8 + idx_size     # RIFF 크기 = 현재 녹화까지
    movi = data.find(b"movi")
    assert data[movi + 4:idx1] == good[good.find(b"movi") + 4:good.rfind(b"idx1")]   # 본문 바이트 동일
    import mmap
    with open(out, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        regions = A.slack_regions_avi(mm)
    A.WARNINGS.clear()
    assert regions and regions[-1][2] == len(data) and regions[-1][1] == idx1 + 8 + idx_size
    # 취소: 블록마다 확인하고 사본을 남기지 않는다. 참조 탐색·검증도 취소를 받는다(#48).
    from engine.engine_adapter import CancelledError
    ev = threading.Event()
    ev.set()
    with pytest.raises(CancelledError):
        transplant_header(r, str(ref), str(tmp_path / "cancelled.avi"), cancel_event=ev)
    assert not (tmp_path / "cancelled.avi").exists()
    with pytest.raises(CancelledError):
        find_reference_candidates(str(dmg), r, cancel_event=ev)
    with pytest.raises(CancelledError):
        validate_reference(r, str(ref), cancel_event=ev)


# ---- #1 fMP4 이식 ---------------------------------------------------------------

def test_fmp4_transplant_uses_only_ftyp_moov_and_rejects_hybrid_reference(tmp_path):
    from core import duration as duration_mod
    # 손상 파일: 혼합 fMP4의 ftyp/moov만 0으로(첫 moof 앞 mdat는 남아 있음)
    dmg_data = bytearray(_mp4_hybrid_bytes(3, 20, lat=37.5, utc_min=24))
    first_mdat = dmg_data.find(b"mdat") - 4
    dmg_data[0:first_mdat] = bytes(first_mdat)
    dmg = tmp_path / "REC_20240916_082400.mp4"
    dmg.write_bytes(bytes(dmg_data))
    r = scan_damage(str(dmg))
    assert r.header_missing and r.fragmented and r.mp4_media_start == first_mdat and r.mp4_first_moof > first_mdat
    # 혼합 참조(다른 녹화의 샘플이 moov·mdat에 있음)는 거부
    hybrid = tmp_path / "REC_20240916_082500.mp4"
    hybrid.write_bytes(_mp4_hybrid_bytes(3, 10, lat=35.1, utc_min=25))
    assert not find_reference_candidates(str(dmg), r)
    ok, why = validate_reference(r, str(hybrid))
    assert not ok and "혼합" in why
    # 순수 조각 참조(moov 샘플 표 비어 있음, 60초짜리)
    pure = tmp_path / "REC_20240916_082300.mp4"
    pure.write_bytes(_mp4_hybrid_bytes(0, 60, lat=35.1, utc_min=23))
    assert find_reference_candidates(str(dmg), r) == [str(pure)]
    out = tmp_path / "rep.mp4"
    note = transplant_header(r, str(pure), str(out))
    data = out.read_bytes()
    with open(out, "rb") as f:
        boxes, _b, _k = M.scan_top_level_bounded(f, len(data))
    M.WARNINGS.clear()
    types = [b.box_type for b in boxes]
    assert types[:4] == [b"ftyp", b"moov", b"mdat", b"moof"]          # 참조 mdat 없음, 손상 파일의 앞 mdat 보존
    assert all(v == 0 for v in _mp4_durations(data).values()) and "0으로 비움" in note
    assert b"3506." not in data                                         # 참조 영상의 좌표가 섞이지 않음
    dur = duration_mod.get_duration_sec(str(out), "mp4")
    assert dur is not None and 20 <= dur <= 24                           # 참조의 60초가 아니라 손상 파일 조각 길이
