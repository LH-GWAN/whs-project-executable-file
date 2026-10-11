"""코드 리뷰 2026-10-11 엔진 MP4 항목 회귀 테스트 (#5 #6 #24 #64 #65 #66 #67 #68 #69 #107 #108).

합성 fMP4/일반 MP4를 만들어 integration_mp4를 CLI처럼 돌리고 timeline.csv·slack 결과를 본다.
"""
from __future__ import annotations

import contextlib
import csv
import glob
import io
import os
import random
import struct

import pytest

from core.paths import ensure_vendor_importable

ensure_vendor_importable()
import integration_mp4 as M  # noqa: E402


# ---------------------------------------------------------------------------
# 합성 파일 만들기
# ---------------------------------------------------------------------------

def _rmc(hh, mm, ss, lat, lon, date, speed_kn=20.0, status="A", bad_checksum=False):
    def dm(v, deg_digits):
        d = int(abs(v))
        m = (abs(v) - d) * 60
        return f"{d:0{deg_digits}d}{m:07.4f}"
    body = (f"GPRMC,{hh:02d}{mm:02d}{ss:02d}.00,{status},{dm(lat, 2)},N,{dm(lon, 3)},E,"
            f"{speed_kn:.1f},84.4,{date},,,A")
    c = 0
    for ch in body:
        c ^= ord(ch)
    if bad_checksum:
        c ^= 0x55
    return f"${body}*{c:02X}".encode("ascii")


def _gga(hh, mm, ss, lat, lon):
    def dm(v, deg_digits):
        d = int(abs(v))
        m = (abs(v) - d) * 60
        return f"{d:0{deg_digits}d}{m:07.4f}"
    body = f"GPGGA,{hh:02d}{mm:02d}{ss:02d}.00,{dm(lat, 2)},N,{dm(lon, 3)},E,1,08,1.0,50.0,M,0.0,M,,"
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"${body}*{c:02X}".encode("ascii")


def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _full(kind, payload, version=0, flags=0):
    return _box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def _text_trak(track_id=3, stbl_extra=None):
    tkhd = _full(b"tkhd", struct.pack(">IIII", 0, 0, track_id, 0) + bytes(76), flags=7)
    mdhd = _full(b"mdhd", struct.pack(">IIIIHH", 0, 0, 1000, 0, 0, 0))
    hdlr = _full(b"hdlr", bytes(4) + b"text" + bytes(12) + b"text\0")
    stsd = _full(b"stsd", struct.pack(">I", 1) + _box(b"tx3g", bytes(6) + struct.pack(">H", 1) + bytes(30)))
    if stbl_extra is None:
        stbl_extra = (_full(b"stts", struct.pack(">I", 0)) + _full(b"stsc", struct.pack(">I", 0))
                      + _full(b"stsz", struct.pack(">II", 0, 0)) + _full(b"stco", struct.pack(">I", 0)))
    stbl = _box(b"stbl", stsd + stbl_extra)
    dinf = _box(b"dinf", _full(b"dref", struct.pack(">I", 1) + _full(b"url ", b"", flags=1)))
    minf = _box(b"minf", _full(b"nmhd", b"") + dinf + stbl)
    return _box(b"trak", tkhd + _box(b"mdia", mdhd + hdlr + minf))


def _init_segment(stbl_extra=None, mvex=True):
    mvhd = _full(b"mvhd", struct.pack(">IIII", 0, 0, 1000, 0) + bytes(80))
    trex = _full(b"trex", struct.pack(">IIIII", 3, 1, 1000, 0, 0))
    moov = _box(b"moov", mvhd + _text_trak(stbl_extra=stbl_extra) + (_box(b"mvex", trex) if mvex else b""))
    return _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso6") + moov


def _fragment(seq, tfdt, text, video_len, rng, tfdt_version=0):
    """moof(mfhd=seq, tfdt) + mdat(영상 바이트 + 텍스트 샘플 하나)."""
    sample = struct.pack(">H", len(text)) + text
    video = bytes(rng.getrandbits(8) for _ in range(video_len))
    mdat = _box(b"mdat", video + sample)
    tfhd = _full(b"tfhd", struct.pack(">I", 3), flags=0x020000)
    if tfdt_version == 1:
        tfdt_box = _full(b"tfdt", struct.pack(">Q", tfdt), version=1)
    else:
        tfdt_box = _full(b"tfdt", struct.pack(">I", tfdt))
    trun_tmp = _full(b"trun", struct.pack(">Ii", 1, 0) + struct.pack(">II", 1000, len(sample)), flags=0x301)
    moof_size = 8 + 16 + len(_box(b"traf", tfhd + tfdt_box + trun_tmp))
    trun = _full(b"trun", struct.pack(">Ii", 1, moof_size + 8 + len(video)) + struct.pack(">II", 1000, len(sample)),
                 flags=0x301)
    traf = _box(b"traf", tfhd + tfdt_box + trun)
    moof = _box(b"moof", _full(b"mfhd", struct.pack(">I", seq)) + traf)
    return moof + mdat


def _recording(frags, hh, mm, ss, lat, lon, date, seed=7, vmin=3000, vmax=6000, texts=None,
               start_seq=1, start_tfdt=0, header=True):
    """조각마다 RMC 하나(초 단위 증가)인 fMP4. texts를 주면 조각별 텍스트를 그대로 쓴다."""
    rng = random.Random(seed)
    out = _init_segment() if header else b""
    for n in range(frags):
        if texts is not None:
            text = texts[n]
        else:
            t = hh * 3600 + mm * 60 + ss + n
            text = _rmc(t // 3600 % 24, t // 60 % 60, t % 60, lat + n * 0.0001, lon, date) + b"\r\n"
        out += _fragment(start_seq + n, start_tfdt + n * 1000, text, rng.randint(vmin, vmax), rng)
    return out


def _flat_mp4(texts, video_len=2000, seed=3, moov_last=False):
    """ftyp + moov(샘플 표) + mdat 순서의 일반 MP4(moov_last면 ftyp + mdat + moov). 텍스트 샘플 N개, 초당 1개."""
    rng = random.Random(seed)
    samples = [struct.pack(">H", len(t)) + t for t in texts]
    video = bytes(rng.getrandbits(8) for _ in range(video_len))
    # moov 크기를 먼저 알아야 stco를 채울 수 있다 - 더미로 한 번 만든 뒤 다시 만든다.
    def build(first_sample_off):
        offs = []
        pos = first_sample_off
        for s in samples:
            offs.append(pos)
            pos += len(s)
        stbl_extra = (_full(b"stts", struct.pack(">III", 1, len(samples), 1000))
                      + _full(b"stsc", struct.pack(">IIII", 1, 1, 1, 1))
                      + _full(b"stsz", struct.pack(">II", 0, len(samples)) + b"".join(struct.pack(">I", len(s)) for s in samples))
                      + _full(b"stco", struct.pack(">I", len(offs)) + b"".join(struct.pack(">I", o) for o in offs)))
        return _init_segment(stbl_extra=stbl_extra, mvex=False)
    mdat = _box(b"mdat", video + b"".join(samples))
    if moov_last:
        head = build(24 + 8 + len(video))
        return head[:24] + mdat + head[24:]
    head = build(0)
    head = build(len(head) + 8 + len(video))
    return head + mdat


def _hybrid_mp4(moov_texts, frag_texts, seed=5):
    """moov 샘플 표가 가리키는 mdat(앞부분) + moof 조각(뒷부분)인 혼합 fMP4."""
    rng = random.Random(seed)
    samples = [struct.pack(">H", len(t)) + t for t in moov_texts]
    video = bytes(rng.getrandbits(8) for _ in range(1500))
    def build(first_sample_off):
        offs, pos = [], first_sample_off
        for s in samples:
            offs.append(pos)
            pos += len(s)
        stbl_extra = (_full(b"stts", struct.pack(">III", 1, len(samples), 1000))
                      + _full(b"stsc", struct.pack(">IIII", 1, 1, 1, 1))
                      + _full(b"stsz", struct.pack(">II", 0, len(samples)) + b"".join(struct.pack(">I", len(s)) for s in samples))
                      + _full(b"stco", struct.pack(">I", len(offs)) + b"".join(struct.pack(">I", o) for o in offs)))
        return _init_segment(stbl_extra=stbl_extra, mvex=True)
    head = build(0)
    head = build(len(head) + 8 + len(video))
    out = head + _box(b"mdat", video + b"".join(samples))
    for n, text in enumerate(frag_texts):
        out += _fragment(n + 1, (len(samples) + n) * 1000, text, rng.randint(2000, 4000), rng)
    return out


def _find_all(data, needle):
    out, pos = [], 0
    while True:
        i = data.find(needle, pos)
        if i < 0:
            return out
        out.append(i)
        pos = i + 1


def _nth_box_start(data, kind, n):
    """n번째(0-based) 최상위 kind Box의 시작 위치(size 필드 포함)."""
    pos, seen = 0, 0
    while pos + 8 <= len(data):
        size, t = struct.unpack(">I4s", data[pos:pos + 8])
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        elif size == 0:
            size = len(data) - pos
        if t == kind:
            if seen == n:
                return pos, size
            seen += 1
        if size < 8:
            break
        pos += size
    raise AssertionError(f"{kind!r} #{n} not found")


# ---------------------------------------------------------------------------
# 엔진 실행 도우미
# ---------------------------------------------------------------------------

def _run(path, out_dir, slack=True):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        M.main([str(path), "-o", str(out_dir)] + (["--slack"] if slack else []))
    warnings = [l for l in buf.getvalue().splitlines() if "[WARN]" in l]
    tl = glob.glob(os.path.join(str(out_dir), "**", "timeline.csv"), recursive=True)
    rows = list(csv.DictReader(open(tl[0], encoding="utf-8"))) if tl else []
    sc = glob.glob(os.path.join(str(out_dir), "**", "slack_coordinates.csv"), recursive=True)
    slack_rows = list(csv.DictReader(open(sc[0], encoding="utf-8"))) if sc else []
    sr = glob.glob(os.path.join(str(out_dir), "**", "slack_regions.csv"), recursive=True)
    regions = list(csv.DictReader(open(sr[0], encoding="utf-8"))) if sr else []
    return rows, slack_rows, regions, warnings


def _scan(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f, contextlib.redirect_stdout(io.StringIO()):
        boxes, boundary, kind = M.scan_top_level_bounded(f, size)
    return boxes, boundary, kind, list(M.DAMAGE_GAPS)


def _gps_rows(rows):
    return [r for r in rows if r.get("latitude")]


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


CUR = dict(hh=8, mm=0, ss=0, lat=37.50, lon=127.0, date="101026")


# ---------------------------------------------------------------------------
# #5 재동기화 뒤의 옛 녹화 조각/moov를 현재 녹화로 받지 않는다
# ---------------------------------------------------------------------------

def test_5_prealloc_old_fragments_stay_in_slack(tmp_path):
    old = _recording(40, 8, 0, 0, 35.10, 129.0, "091026", seed=1)
    cur = _recording(15, seed=7, **CUR)
    p = _write(tmp_path, "prealloc.mp4", cur + old[len(cur):])
    rows, slack_rows, regions, _w = _run(p, tmp_path / "out")
    gps = _gps_rows(rows)
    assert len(gps) == 15 and {r["gps_date"] for r in gps} == {"2026-10-10"}
    assert all(float(r["latitude"]) > 37 for r in gps)
    assert max(float(r["start_time_sec"]) for r in rows) < 15
    # 옛 조각의 GPS는 슬랙에만 있고, 같은 바이트가 본 궤적과 슬랙에 같이 나오지 않는다
    assert len(slack_rows) >= 20 and all(r["date"] == "2026-10-09" for r in slack_rows if r["date"])
    main_offsets = {int(r["source_offset"]) for r in rows if r.get("source_offset")}
    assert not main_offsets & {int(r["absolute_offset"], 16) for r in slack_rows}
    _b, boundary, kind, _g = _scan(p)
    assert boundary is not None and boundary >= len(cur) and kind in M.APPENDED_BOUNDARY_KINDS


def test_5_same_day_old_recording_with_continuous_mfhd_is_boundary(tmp_path):
    # 조각 경계에 정확히 맞춰 이어붙어 재동기화가 없고, mfhd·tfdt도 이어지는데 GPS만 어긋나는 경우
    cur = _recording(15, seed=7, **CUR)
    old_tail = _recording(10, 3, 30, 0, 37.40, 127.1, "101026", seed=2, start_seq=16, start_tfdt=15000, header=False)
    p = _write(tmp_path, "aligned.mp4", cur + old_tail)
    rows, slack_rows, _r, _w = _run(p, tmp_path / "out")
    gps = _gps_rows(rows)
    assert len(gps) == 15 and all(r["gps_utc_time"].startswith("08:00") for r in gps)
    assert len(slack_rows) == 10 and all(r["utc_time"].startswith("03:30") for r in slack_rows)
    _b, boundary, kind, gaps = _scan(p)
    assert boundary == len(cur) and kind in M.APPENDED_BOUNDARY_KINDS and not gaps


def test_5_midnight_crossing_with_gap_is_not_boundary(tmp_path):
    # 23:59:55 -> 00:00:05(날짜 바뀜)로 이어지는 정상 녹화 중간에 손상 구간이 있어도 뒤 조각을 현재 녹화로 받는다
    texts = []
    for n in range(15):
        t = 23 * 3600 + 59 * 60 + 55 + n
        date = "091026" if t < 86400 else "101026"
        texts.append(_rmc(t // 3600 % 24, t // 60 % 60, t % 60, 37.5, 127.0, date) + b"\r\n")
    cur = _recording(15, texts=texts, seed=7, **CUR)
    data = bytearray(cur)
    start, size = _nth_box_start(cur, b"moof", 4)
    data[start:start + 16] = bytes(16)
    p = _write(tmp_path, "midnight_gap.mp4", bytes(data))
    boxes, boundary, kind, gaps = _scan(p)
    assert boundary is None and sum(1 for b in boxes if b.box_type == b"moof") == 14
    rows, slack_rows, _r, _w = _run(p, tmp_path / "out")
    gps = _gps_rows(rows)
    assert len(gps) == 15 and not slack_rows
    assert {r["gps_date"] for r in gps} == {"2026-10-09", "2026-10-10"}


def test_5_flat_mp4_second_moov_is_boundary(tmp_path):
    cur_texts = [_rmc(5, 0, s, 35.10, 129.0, "101026") + b"\r\n" for s in range(10)]
    old_texts = [_rmc(3, 0, s, 37.50, 127.0, "091026") + b"\r\n" for s in range(60)]
    # moov가 끝에 오는 배치: 현재 녹화 뒤에 옛 mdat 잔재와 옛 moov가 남는다
    cur = _flat_mp4(cur_texts, video_len=2000, moov_last=True)
    old = _flat_mp4(old_texts, video_len=20000, moov_last=True)
    p = _write(tmp_path, "flat_prealloc.mp4", cur + old[len(cur):])
    rows, slack_rows, _r, _w = _run(p, tmp_path / "out")
    gps = _gps_rows(rows)
    assert len(gps) == 10 and {r["gps_date"] for r in gps} == {"2026-10-10"}
    assert slack_rows and all(r["date"] == "2026-10-09" for r in slack_rows if r["date"])
    boxes, boundary, kind, _g = _scan(p)
    assert boundary is not None and kind in M.APPENDED_BOUNDARY_KINDS
    assert sum(1 for b in boxes if b.box_type == b"moov") == 1


# ---------------------------------------------------------------------------
# #6 gap 카빙: EOF 꼬리는 슬랙으로만, 날짜·앵커 UTC·영상 길이 안의 행만 받는다
# ---------------------------------------------------------------------------

def _garbage_tail(hh, secs, date, lat):
    t = b"\x00\x00\x10\x00\x01\x02\x03\x04" + bytes(range(200, 256)) * 2
    for s in secs:
        t += b"\x11\x22" * 10 + _rmc(hh, 0, s, lat, 129.0, date) + b"\r\n"
    return t


@pytest.mark.parametrize("hh,date", [(8, "091026"), (19, "091026"), (8, "101026")])
def test_6_eof_tail_residue_is_slack_only(tmp_path, hh, date):
    cur = _recording(15, seed=7, **CUR)
    p = _write(tmp_path, f"tail_{hh}_{date}.mp4", cur + _garbage_tail(hh, range(20, 30), date, 35.1))
    rows, slack_rows, regions, _w = _run(p, tmp_path / "out")
    assert len(rows) == 15 and not [r for r in rows if r["time_source"] == "gap_carved_utc"]
    assert max(float(r["start_time_sec"]) for r in rows) < 15
    assert len(slack_rows) == 10 and {r["utc_time"][:2] for r in slack_rows} == {f"{hh:02d}"}


def test_6_old_date_in_mid_gap_is_rejected_but_current_date_is_carved(tmp_path):
    cur = _recording(15, seed=7, **CUR)
    old = _recording(40, 8, 0, 0, 35.10, 129.0, "091026", seed=1)
    old_tail = _garbage_tail(8, range(3, 6), "091026", 35.1)
    # 손상 구간(옛 RMC 포함) 뒤에 옛 녹화 전체(두 번째 ftyp)
    p = _write(tmp_path, "gap_old.mp4", cur + old_tail + old)
    rows, slack_rows, _r, _w = _run(p, tmp_path / "out")
    assert len(rows) == 15 and {r["gps_date"] for r in _gps_rows(rows)} == {"2026-10-10"}
    assert any(r["utc_time"] == "08:00:03.00" for r in slack_rows)
    # 현재 녹화의 moof 하나를 0으로 덮으면 그 mdat의 RMC는 회수된다(날짜·앵커 안)
    data = bytearray(cur)
    start, size = _nth_box_start(cur, b"moof", 5)
    data[start:start + 16] = bytes(16)
    p2 = _write(tmp_path, "gap_cur.mp4", bytes(data))
    rows2, slack2, _r2, _w2 = _run(p2, tmp_path / "out2")
    carved = [r for r in rows2 if r["time_source"] == "gap_carved_utc"]
    assert len(carved) == 1 and carved[0]["gps_utc_time"] == "08:00:05.00" and carved[0]["start_time_sec"] == "5.000"
    # #24: 회수한 문장은 슬랙에 중복으로 나오지 않는다
    assert not [r for r in slack2 if r["utc_time"] == "08:00:05.00"]


def test_6_carved_row_outside_anchor_window_is_rejected(tmp_path):
    # moof 5를 지우고 그 mdat의 RMC를 같은 날짜지만 영상 길이 밖(08:30)으로 바꾼다
    texts = [_rmc(8, 0, s, 37.5, 127.0, "101026") + b"\r\n" for s in range(15)]
    texts[5] = _rmc(8, 30, 0, 37.5, 127.0, "101026") + b"\r\n"
    cur = _recording(15, texts=texts, seed=7, **CUR)
    data = bytearray(cur)
    start, _size = _nth_box_start(cur, b"moof", 5)
    data[start:start + 16] = bytes(16)
    p = _write(tmp_path, "gap_far.mp4", bytes(data))
    rows, _s, _r, _w = _run(p, tmp_path / "out")
    assert not [r for r in rows if r["time_source"] == "gap_carved_utc"]
    assert len(rows) == 14


# ---------------------------------------------------------------------------
# #24 슬랙: 본 궤적이 쓴 바이트는 빼고, largesize mdat 잘림은 truncated
# ---------------------------------------------------------------------------

def test_24_largesize_mdat_truncation_is_truncated_not_slack(tmp_path):
    cur = _recording(6, seed=7, **CUR)
    start, size = _nth_box_start(cur, b"mdat", 5)
    payload = cur[start + 8:start + size]
    big = struct.pack(">I4s", 1, b"mdat") + struct.pack(">Q", 16 + len(payload) + 4096) + payload
    data = cur[:start] + big
    # trun data_offset은 moof 기준이라 mdat 머리가 8바이트 늘면 샘플 위치가 어긋난다 - 마지막 샘플은 포기해도 된다
    p = _write(tmp_path, "large_cut.mp4", data)
    boxes, boundary, kind, gaps = _scan(p)
    assert kind == "truncated" and boundary == start and not gaps
    rows, slack_rows, regions, _w = _run(p, tmp_path / "out")
    assert not [r for r in regions if r["region_kind"] == "trailing"]
    assert not slack_rows
    from core.damage import scan_damage
    assert scan_damage(str(p)).truncated


# ---------------------------------------------------------------------------
# #64 tfdt 하나 손상은 그 moof만 손상, 뒤는 현재 녹화
# ---------------------------------------------------------------------------

def test_64_single_tfdt_damage_does_not_cut_rest(tmp_path):
    cur = _recording(15, seed=7, **CUR)
    start, size = _nth_box_start(cur, b"moof", 4)
    data = bytearray(cur)
    tfdt = data.find(b"tfdt", start, start + size)
    data[tfdt + 4:tfdt + 12] = bytes(8)
    p = _write(tmp_path, "tfdt0.mp4", bytes(data))
    boxes, boundary, kind, gaps = _scan(p)
    assert boundary is None and kind is None
    assert gaps and gaps[0][0] == start and gaps[0][1] == start + size
    assert sum(1 for b in boxes if b.box_type == b"moof") == 14
    rows, slack_rows, _r, _w = _run(p, tmp_path / "out")
    gps = _gps_rows(rows)
    assert len(gps) == 15 and not slack_rows
    carved = [r for r in rows if r["time_source"] == "gap_carved_utc"]
    assert len(carved) == 1 and carved[0]["gps_utc_time"] == "08:00:04.00"
    from core.damage import scan_damage
    assert scan_damage(str(p)).damaged


# ---------------------------------------------------------------------------
# #65 size 0·ASCII 꼬리를 '끝까지 Box'·truncated로 오판하지 않는다
# ---------------------------------------------------------------------------

def test_65_ftyp_size_zero_still_analyzable(tmp_path):
    cur = _recording(15, seed=7, **CUR)
    data = bytearray(cur)
    data[0:4] = bytes(4)
    p = _write(tmp_path, "ftyp0.mp4", bytes(data))
    rows, _s, _r, _w = _run(p, tmp_path / "out")
    assert len(_gps_rows(rows)) == 15


def test_65_moof_size_zero_keeps_rest_and_marks_damage(tmp_path):
    cur = _recording(15, seed=7, **CUR)
    start, size = _nth_box_start(cur, b"moof", 3)
    data = bytearray(cur)
    data[start:start + 4] = bytes(4)
    p = _write(tmp_path, "moof0.mp4", bytes(data))
    boxes, boundary, kind, gaps = _scan(p)
    assert sum(1 for b in boxes if b.box_type == b"moof") == 14 and gaps and gaps[0][0] == start
    rows, _s, _r, _w = _run(p, tmp_path / "out")
    assert len(_gps_rows(rows)) >= 14


def test_65_ascii_tail_is_not_truncated(tmp_path):
    cur = _recording(15, seed=7, **CUR)
    tail = b"".join(_rmc(8, 0, s, 35.1, 129.0, "091026") + b"\r\n" for s in range(20, 30))
    p = _write(tmp_path, "ascii_tail.mp4", cur + tail)
    boxes, boundary, kind, gaps = _scan(p)
    assert kind != "truncated" and boundary == len(cur)
    rows, slack_rows, regions, _w = _run(p, tmp_path / "out")
    assert len(rows) == 15 and len(slack_rows) == 10
    from core.damage import scan_damage
    assert not scan_damage(str(p)).truncated


# ---------------------------------------------------------------------------
# #66 0으로 채운 꼬리는 padding(손상 아님)
# ---------------------------------------------------------------------------

def test_66_zero_tail_is_padding_not_damage(tmp_path):
    cur = _recording(15, seed=7, **CUR)
    p = _write(tmp_path, "zero_tail.mp4", cur + bytes(200 * 1024))
    boxes, boundary, kind, gaps = _scan(p)
    assert kind == "padding" and boundary == len(cur) and not gaps
    from core.damage import scan_damage
    assert not scan_damage(str(p)).damaged
    rows, slack_rows, regions, _w = _run(p, tmp_path / "out")
    assert len(_gps_rows(rows)) == 15 and not slack_rows


# ---------------------------------------------------------------------------
# #67 혼합 fMP4: moov 샘플 표가 가리키는 mdat는 샘플 표로 읽는다
# ---------------------------------------------------------------------------

def test_67_hybrid_moov_samples_are_read_not_carved(tmp_path):
    moov_texts = [_rmc(8, 0, s, 37.5, 127.0, "101026") + b"\r\n" for s in range(5)]
    frag_texts = [_rmc(8, 0, s, 37.5, 127.0, "101026") + b"\r\n" for s in range(5, 20)]
    p = _write(tmp_path, "hybrid.mp4", _hybrid_mp4(moov_texts, frag_texts))
    rows, slack_rows, _r, warnings = _run(p, tmp_path / "out")
    gps = _gps_rows(rows)
    assert len(gps) == 20 and not [r for r in rows if r["time_source"] == "gap_carved_utc"]
    assert [r["start_time_sec"] for r in rows[:5]] == ["0.000", "1.000", "2.000", "3.000", "4.000"]
    assert rows[0]["time_source"] == "stts" and rows[5]["time_source"] == "tfdt_trun"
    assert not any("누락" in w for w in warnings)
    from core.damage import scan_damage
    assert not scan_damage(str(p)).damaged


# ---------------------------------------------------------------------------
# #68 moof 머리는 멀쩡하고 내부가 손상되면 손상으로 기록하고 mdat를 카빙한다
# ---------------------------------------------------------------------------

def test_68_moof_internal_damage_is_recorded_and_carved(tmp_path):
    cur = _recording(15, seed=7, **CUR)
    start, size = _nth_box_start(cur, b"moof", 5)
    data = bytearray(cur)
    traf = data.find(b"traf", start, start + size) - 4
    data[traf:start + size] = bytes(start + size - traf)
    p = _write(tmp_path, "moof_inner.mp4", bytes(data))
    boxes, boundary, kind, gaps = _scan(p)
    assert boundary is None and gaps == [(start, start + size)]
    rows, slack_rows, _r, _w = _run(p, tmp_path / "out")
    carved = [r for r in rows if r["time_source"] == "gap_carved_utc"]
    assert len(_gps_rows(rows)) == 15 and len(carved) == 1 and carved[0]["gps_utc_time"] == "08:00:05.00"
    from core.damage import scan_damage
    assert scan_damage(str(p)).damaged


# ---------------------------------------------------------------------------
# #69 체크섬 실패 행은 시간축 판정에서 뺀다
# ---------------------------------------------------------------------------

def test_69_bad_checksum_row_does_not_shift_time_axis():
    rows = []
    for s in range(10):
        rows.append({"utc_time": f"20:00:{s:02d}.00", "date": "2026-10-10", "checksum_ok": True})
    rows.insert(5, {"utc_time": "00:00:02.00", "date": "2026-10-10", "checksum_ok": False})
    with contextlib.redirect_stdout(io.StringIO()):
        M.assign_utc_elapsed_times(rows)
    good = [r for r in rows if r["checksum_ok"]]
    assert [r["start_time_sec"] for r in good] == [f"{s:.3f}" for s in range(10)]
    bad = rows[5]
    assert bad["start_time_sec"] == "" and bad["time_source"] == ""
    # 체크섬 실패 행이 맨 앞이어도 기준 시각이 되지 않는다
    rows2 = [{"utc_time": "23:59:50.00", "date": "2026-10-09", "checksum_ok": False}]
    rows2 += [{"utc_time": f"20:00:{s:02d}.00", "date": "2026-10-10", "checksum_ok": True} for s in range(3)]
    with contextlib.redirect_stdout(io.StringIO()):
        M.assign_utc_elapsed_times(rows2)
    assert [r["start_time_sec"] for r in rows2[1:]] == ["1.000", "2.000", "3.000"]


# ---------------------------------------------------------------------------
# #107 체크섬 맞는 문장을 먼저 고른다 (RMC 틀림 + GGA 맞음 → GGA)
# ---------------------------------------------------------------------------

def test_107_valid_gga_preferred_over_bad_rmc(tmp_path):
    texts = [_rmc(8, 0, s, 37.5, 127.0, "101026", bad_checksum=True) + b"\r\n" + _gga(8, 0, s, 37.5, 127.0) + b"\r\n"
             for s in range(10)]
    p = _write(tmp_path, "rmc_bad_gga.mp4", _recording(10, texts=texts, seed=7, **CUR))
    rows, _s, _r, _w = _run(p, tmp_path / "out", slack=False)
    assert len(rows) == 10 and all(r["latitude"] and r["gps_checksum_ok"] == "True" for r in rows)
    from engine.engine_adapter import load_timeline
    tl = glob.glob(os.path.join(str(tmp_path / "out"), "**", "timeline.csv"), recursive=True)[0]
    assert sum(1 for pt in load_timeline(tl) if pt.has_fix) == 10
    # 둘 다 맞으면 RMC(속도·날짜)가 대표
    texts2 = [_rmc(8, 0, s, 37.5, 127.0, "101026") + b"\r\n" + _gga(8, 0, s, 37.5, 127.0) + b"\r\n" for s in range(3)]
    p2 = _write(tmp_path, "rmc_gga.mp4", _recording(3, texts=texts2, seed=7, **CUR))
    rows2, _s, _r, _w = _run(p2, tmp_path / "out2", slack=False)
    assert all(r["gps_date"] == "2026-10-10" and r["speed_kmh"] for r in rows2)
    # 체크섬이 없는 RMC보다 체크섬 맞는 GGA, 체크섬 틀린 RMC보다 체크섬 없는 RMC(AVI 엔진과 같은 순서)
    no_cs = [_rmc(8, 0, s, 37.5, 127.0, "101026").split(b"*")[0] + b"\r\n" + _gga(8, 0, s, 37.5, 127.0) + b"\r\n"
             for s in range(3)]
    p4 = _write(tmp_path, "rmc_nocs_gga.mp4", _recording(3, texts=no_cs, seed=7, **CUR))
    rows4, _s, _r, _w = _run(p4, tmp_path / "out4", slack=False)
    assert all(r["gps_checksum_ok"] == "True" and not r["gps_date"] for r in rows4)
    mixed = [_rmc(8, 0, s, 37.5, 127.0, "101026", bad_checksum=True) + b"\r\n"
             + _rmc(8, 0, s, 37.5, 127.0, "101026").split(b"*")[0] + b"\r\n" for s in range(3)]
    p5 = _write(tmp_path, "rmc_bad_nocs.mp4", _recording(3, texts=mixed, seed=7, **CUR))
    rows5, _s, _r, _w = _run(p5, tmp_path / "out5", slack=False)
    assert all(r["gps_checksum_ok"] == "" and r["gps_date"] == "2026-10-10" for r in rows5)
    # 일반 MP4(샘플 표) 경로도 같다
    p3 = _write(tmp_path, "flat_rmc_bad_gga.mp4", _flat_mp4(texts))
    rows3, _s, _r, _w = _run(p3, tmp_path / "out3", slack=False)
    assert len(rows3) == 10 and all(r["latitude"] and r["gps_checksum_ok"] == "True" for r in rows3)


# ---------------------------------------------------------------------------
# #108 최상위 순회 결과를 캐시하고 취소할 수 있다
# ---------------------------------------------------------------------------

def test_108_scan_is_cached_and_cancellable(tmp_path, monkeypatch):
    cur = _recording(15, seed=7, **CUR)
    p = _write(tmp_path, "cache.mp4", cur + _garbage_tail(8, range(20, 25), "091026", 35.1) + bytes(300 * 1024))
    calls = []
    real = M.resync_top_level
    def counted(f, start, filesize, **kw):
        calls.append(start)
        return real(f, start, filesize, **kw)
    monkeypatch.setattr(M, "resync_top_level", counted)
    r1 = _scan(p)
    r2 = _scan(p)
    assert calls and len(calls) == 1
    assert (r1[1], r1[2], r1[3]) == (r2[1], r2[2], r2[3]) and len(r1[0]) == len(r2[0])
    # 파일이 바뀌면 다시 읽는다
    p.write_bytes(cur + _garbage_tail(8, range(20, 25), "091026", 35.1) + bytes(301 * 1024))
    _scan(p)
    assert len(calls) == 2
    # 취소
    size = os.path.getsize(p)
    with open(p, "rb") as f, contextlib.redirect_stdout(io.StringIO()):
        with pytest.raises(InterruptedError):
            M.scan_top_level_bounded(f, size, cancel_check=lambda: True)
