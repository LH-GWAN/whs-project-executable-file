"""2026-10-11 코드 리뷰 반영 - 엔진 AVI(integration_avi.py, integration_blackbox.py) 회귀 테스트.

항목: #7 #8 #9 #10 #11 #16 #23 #27 #61 #62 #63 #80 #107(AVI) #118 #120.
합성 AVI만 쓴다(실제 샘플은 보고서의 회귀 비교로 확인)."""
from __future__ import annotations

import csv
import glob
import io
import contextlib
import mmap
import os
import random
import struct

import pytest

from core.paths import ensure_vendor_importable

ensure_vendor_importable()
import integration_avi as A  # noqa: E402
import integration_blackbox as bb  # noqa: E402


# ---------------------------------------------------------------------------
# 합성 AVI
# ---------------------------------------------------------------------------

def _cs(body: str) -> bytes:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"${body}*{c:02X}\r\n".encode("ascii")


def rmc(t: int, date: str = "160924", lat: float = 37.5, lon: float = 127.0, status: str = "A") -> bytes:
    """t: 그 날의 UTC 초. status V면 좌표 없는(측위 전) 문장."""
    t %= 86400
    hh, mm_, ss = t // 3600, (t // 60) % 60, t % 60
    if status == "V":
        return _cs(f"GPRMC,{hh:02d}{mm_:02d}{ss:02d}.00,V,,,,,0.00,0.00,,,,N")
    la = lat + t * 0.00001
    return _cs(f"GPRMC,{hh:02d}{mm_:02d}{ss:02d}.00,A,{int(la) * 100 + (la % 1) * 60:09.4f},N,"
               f"{int(lon) * 100 + (lon % 1) * 60:010.4f},E,20.0,84.4,{date},,,A")


def ck(cid: bytes, payload: bytes) -> bytes:
    return cid + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def hdrl_bytes(frames: int, fps: int = 1, audio: bool = False, text_cid: bytes = b"01tx") -> bytes:
    def strh(fcc, handler, scale, rate, length):
        return (fcc + handler + struct.pack("<IHHIIIIIIII", 0, 0, 0, 0, scale, rate, 0, length, 0, 0, 0)
                + struct.pack("<hhhh", 0, 0, 0, 0))
    strf_v = struct.pack("<IiiHHIIiiII", 40, 64, 48, 1, 24, 0x47504A4D, 64 * 48 * 3, 0, 0, 0, 0)
    strls = ck(b"LIST", b"strl" + ck(b"strh", strh(b"vids", b"MJPG", 1, fps, frames)) + ck(b"strf", strf_v))
    n = 2
    if audio:
        strf_a = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
        strls += ck(b"LIST", b"strl" + ck(b"strh", strh(b"auds", b"\0\0\0\0", 1, 8000, frames * 8000 // fps))
                    + ck(b"strf", strf_a))
        n = 3
    strls += ck(b"LIST", b"strl" + ck(b"strh", strh(b"txts", b"\0\0\0\0", 1, 1, frames // fps)) + ck(b"strf", b"\0" * 8))
    avih = struct.pack("<14I", 1_000_000 // fps, 0, 0, 0x10, frames, 0, n, 0, 64, 48, 0, 0, 0, 0)
    return ck(b"LIST", b"hdrl" + ck(b"avih", avih) + strls)


def movi_chunks(seconds: int, text_fn, fps: int = 1, audio: bool = False, frame_bytes: int = 1000,
                seed: int = 1, fixed_audio: bool = True, start_sec: int = 0):
    """초마다 [text, (frame, audio) x fps] 순서의 (cid, payload) 목록."""
    rnd = random.Random(seed)
    tid = b"02tx" if audio else b"01tx"
    out = []
    for sec in range(start_sec, start_sec + seconds):
        out.append((tid, text_fn(sec)))
        for f in range(fps):
            n = frame_bytes + rnd.randint(0, frame_bytes // 2)
            out.append((b"00dc", b"\xff\xd8" + bytes([rnd.randrange(1, 250)]) * n + b"\xff\xd9"))
            if audio:
                out.append((b"01wb", b"\x80" * (533 if fixed_audio else rnd.randint(500, 600))))
    return out


def build_avi(chunks, planned_frames=None, fps=1, audio=False, with_idx1=True, tail=b"", riff_size=None):
    """chunks: (cid, payload) 목록. 반환 (bytes, movi FourCC 위치)."""
    frames = sum(1 for c, _ in chunks if c == b"00dc")
    hdrl = hdrl_bytes(planned_frames or frames, fps, audio)
    movi = bytearray(b"movi")
    entries = []
    for cid, payload in chunks:
        entries.append((cid, len(movi), len(payload)))
        movi += ck(cid, payload)
    movi += tail
    movi_list = b"LIST" + struct.pack("<I", len(movi)) + bytes(movi)
    body = b"AVI " + hdrl + movi_list
    if with_idx1:
        body += ck(b"idx1", b"".join(c + struct.pack("<III", 0x10, o, s) for c, o, s in entries))
    data = b"RIFF" + struct.pack("<I", riff_size if riff_size is not None else len(body)) + body
    return data, 12 + len(hdrl) + 8


def _run_engine(path: str, out_dir: str, extra=()):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        A.main(["-o", out_dir, *extra, path])
    tl = glob.glob(os.path.join(out_dir, "**", "timeline.csv"), recursive=True)
    rows = list(csv.DictReader(open(tl[0], encoding="utf-8"))) if tl else []
    return rows, buf.getvalue()


def _mm(data: bytes):
    return data  # 엔진 함수는 bytes에도 그대로 동작한다(find/rfind/슬라이스/regex)


def _quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


# ---------------------------------------------------------------------------
# #16 'LIST rec ' 그룹·ix## 는 정상 구조
# ---------------------------------------------------------------------------

def _rec_avi(seconds=20, rec_entries=True):
    hdrl = hdrl_bytes(seconds)
    movi = bytearray(b"movi")
    entries = []
    for sec in range(seconds):
        rec_off = len(movi)
        inner = bytearray(b"rec ")
        sub = []
        for cid, payload in ((b"00dc", b"\xff\xd8" + bytes([sec + 1]) * (1000 + sec) + b"\xff\xd9"), (b"01tx", rmc(8 * 3600 + sec))):
            sub.append((cid, rec_off + 8 + len(inner), len(payload), 0x10))
            inner += ck(cid, payload)
        if rec_entries:
            entries.append((b"rec ", rec_off, len(inner), 0x01))
        entries.extend(sub)
        movi += ck(b"LIST", bytes(inner))
    body = b"AVI " + hdrl + ck(b"LIST", bytes(movi)) + ck(b"idx1", b"".join(
        c + struct.pack("<III", fl, o, s) for c, o, s, fl in entries))
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_walk_descends_into_rec_groups_without_gaps():
    data = _rec_avi()
    fourcc = data.find(b"movi")
    idx1 = data.rfind(b"idx1")
    gaps = []
    walked = list(_quiet(lambda: list(A.walk_movi_chunks(data, fourcc + 4, idx1, gaps))))
    assert gaps == []
    assert [c for _, c, _ in walked].count(b"00dc") == 20 and [c for _, c, _ in walked].count(b"01tx") == 20
    assert A.CHUNK_ID_RE.fullmatch(b"ix00") and A.CHUNK_ID_RE.fullmatch(b"01tx")


@pytest.mark.parametrize("rec_entries", [True, False])
def test_signature_damaged_rec_avi_keeps_its_idx1(tmp_path, rec_entries):
    data = bytearray(_rec_avi(rec_entries=rec_entries))
    idx1_pos = data.rfind(b"idx1")
    data[0:8] = bytes(8)
    hdrl, movi, idx1, _ = _quiet(A.find_top_level_sections, bytes(data))
    assert hdrl is not None and movi and idx1 is not None and idx1.pos == idx1_pos
    p = tmp_path / "rec.avi"
    p.write_bytes(bytes(data))
    rows, out = _run_engine(str(p), str(tmp_path / "out"))
    assert len(rows) == 20 and "DAMAGE_JSON" not in out
    assert [float(r["start_time_sec"]) for r in rows] == [float(i) for i in range(20)]


# ---------------------------------------------------------------------------
# #11 0 꼬리는 빈 자리를 넣지 않고, 시간축은 살아남은 프레임 수 ÷ fps
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tail_mb", [2, 6])
def test_zero_tail_is_unused_allocation_not_damage(tmp_path, tail_mb):
    chunks = movi_chunks(20, lambda s: rmc(8 * 3600 + s), fps=30, frame_bytes=2000)
    data, fourcc = build_avi(chunks, planned_frames=60 * 30, fps=30, with_idx1=False, tail=bytes(tail_mb * 1024 * 1024))
    tail = {}
    gaps = []
    walked = _quiet(lambda: list(A.walk_movi_chunks(data, fourcc + 4, len(data), gaps, tail_info=tail)))
    assert gaps == [] and tail.get("zero_tail") is not None and len(walked) == 20 * 31
    p = tmp_path / "pre.avi"
    p.write_bytes(data)
    rows, out = _run_engine(str(p), str(tmp_path / "out"))
    assert len(rows) == 20
    assert [float(r["start_time_sec"]) for r in rows] == [float(i) for i in range(20)]
    assert "빈 자리" not in out and '"placeholders": 0' in out and '"zero_tail": [' in out
    assert "20.000초" in out and "60.000초" in out   # 헤더 60초 대신 색인 20초를 썼다고 경고


# ---------------------------------------------------------------------------
# #10 잘린 idx1·잘린 movi
# ---------------------------------------------------------------------------

def _frame_avi(frames=300, fps=30):
    chunks = []
    for i in range(frames):
        chunks.append((b"00dc", b"\xff\xd8\xff" + bytes([i % 251 + 1]) * (900 + i % 7) + b"\xff\xd9"))
        chunks.append((b"01tx", rmc(12 * 3600 + i)))
    return build_avi(chunks, fps=fps)


def test_truncated_idx1_is_supplemented_from_movi(tmp_path):
    data, _ = _frame_avi()
    idx1 = data.rfind(b"idx1")
    for name, cut in (("half", idx1 + 8 + 150 * 16), ("five", idx1 + 8 + 5 * 16)):
        p = tmp_path / f"{name}.avi"
        p.write_bytes(data[:cut])
        rows, out = _run_engine(str(p), str(tmp_path / f"out_{name}"))
        assert len(rows) == 300, name
        assert abs(float(rows[-1]["start_time_sec"]) - 299 / 30) < 1e-3
        assert '"idx1_truncated": true' in out and "보충" in out


def test_truncated_movi_axis_uses_surviving_frames(tmp_path):
    data, _ = _frame_avi()
    idx1 = data.rfind(b"idx1")
    p = tmp_path / "cut.avi"
    p.write_bytes(data[:idx1 - 20 * 1024])
    rows, out = _run_engine(str(p), str(tmp_path / "out"))
    assert 270 <= len(rows) < 300
    # 남은 n행이 10초 전체로 늘어나지 않고 1/30초 간격 그대로
    assert abs(float(rows[1]["start_time_sec"]) - 1 / 30) < 1e-3
    assert abs(float(rows[-1]["start_time_sec"]) - (len(rows) - 1) / 30) < 1e-3
    assert '"truncated_tail":' in out and '"gaps": []' in out


# ---------------------------------------------------------------------------
# #61 시그니처 손상 OpenDML(AVIX)
# ---------------------------------------------------------------------------

def test_signature_damaged_opendml_counts_avix_and_keeps_axis(tmp_path):
    first = movi_chunks(20, lambda s: rmc(8 * 3600 + s))
    second = movi_chunks(20, lambda s: rmc(8 * 3600 + s), start_sec=20)
    data, _ = build_avi(first, planned_frames=40)
    movi2 = bytearray(b"movi")
    for cid, payload in second:
        movi2 += ck(cid, payload)
    avix = b"AVIX" + b"LIST" + struct.pack("<I", len(movi2)) + bytes(movi2)
    whole = bytearray(data + b"RIFF" + struct.pack("<I", len(avix)) + avix)
    ok = _quiet(A.find_top_level_sections, bytes(whole))
    whole[0:8] = bytes(8)
    dmg = _quiet(A.find_top_level_sections, bytes(whole))
    assert ok[3] == 1 and dmg[3] == 1
    p = tmp_path / "avix.avi"
    p.write_bytes(bytes(whole))
    rows, _ = _run_engine(str(p), str(tmp_path / "out"))
    assert [float(r["start_time_sec"]) for r in rows] == [float(i) for i in range(20)]


# ---------------------------------------------------------------------------
# #7 옛 idx1 잔재 / #62 뒤에 붙은 옛 AVI
# ---------------------------------------------------------------------------

def _old_recording(seconds=30, with_idx1=True, frame_bytes=2000):
    chunks = movi_chunks(seconds, lambda s: rmc(3 * 3600 + s, date="010124", lat=35.1, lon=129.0),
                         frame_bytes=frame_bytes, seed=7)
    return build_avi(chunks, with_idx1=with_idx1)[0]


def test_old_idx1_remnants_are_not_accepted_as_current_index(tmp_path):
    old = _old_recording()
    cur_chunks = movi_chunks(10, lambda s: rmc(8 * 3600 + 24 * 60 + s))
    # (a) 옛 AVI가 통째로 현재 movi 안에 남은 경우(현재 idx1 없음)
    cur, fourcc = build_avi(cur_chunks, with_idx1=False, tail=bytes(4096) + old + bytes(8192))
    assert _quiet(A.find_idx1_fallback, cur) is None
    # (b) 사전 할당: 옛 녹화 위에 새 녹화가 앞부분만 덮어쓴 경우 - 옛 idx1이 파일 끝에 그대로 남는다
    new_movi = b"".join(ck(c, p) for c, p in cur_chunks)
    pre = bytearray(old)
    pre[fourcc + 4:fourcc + 4 + len(new_movi)] = new_movi
    assert _quiet(A.find_idx1_fallback, bytes(pre)) is None
    for name, data in (("embedded", cur), ("prealloc", bytes(pre))):
        p = tmp_path / f"{name}.avi"
        p.write_bytes(data)
        rows, out = _run_engine(str(p), str(tmp_path / f"out_{name}"))
        assert rows and all(r["gps_date"] == "2024-09-16" for r in rows), name
        assert len(rows) == 10, name


def test_appended_old_avi_does_not_invalidate_current_boundary():
    cur, _ = build_avi(movi_chunks(10, lambda s: rmc(8 * 3600 + s)), with_idx1=False)
    data = cur + _old_recording(seconds=12)
    end, status = _quiet(A.current_recording_end, data)
    assert status == "ok" and end == len(cur)
    regions = _quiet(A.slack_regions_avi, data)
    assert [(k, s, e) for k, s, e in regions] == [("appended_riff", len(cur), len(data))]


# ---------------------------------------------------------------------------
# #8 녹화 경계 판정
# ---------------------------------------------------------------------------

def _prealloc(new_chunk_count, old_seconds=40, new_text=None, old_text=None, fps=30):
    old = movi_chunks(old_seconds, old_text or (lambda s: rmc(10 * 3600 + s, date="010124", lat=35.1, lon=129.0)),
                      fps=fps, audio=True, frame_bytes=3200, seed=1)
    new = movi_chunks(old_seconds, new_text or (lambda s: rmc(8 * 3600 + 24 * 60 + s)),
                      fps=fps, audio=True, frame_bytes=2900, seed=2)[:new_chunk_count]
    old_movi = b"".join(ck(c, p) for c, p in old)
    new_movi = b"".join(ck(c, p) for c, p in new)
    hdrl = hdrl_bytes(old_seconds * fps, fps, audio=True)
    movi = b"movi" + new_movi + old_movi[len(new_movi):]
    body = b"AVI " + hdrl + ck(b"LIST", movi)
    return b"RIFF" + struct.pack("<I", len(body)) + body, len(new)


def _cut_of(data):
    hdrl, movi, idx1, _ = _quiet(A.find_top_level_sections, data)
    fourcc = movi[0].data_start
    gaps = []
    walked = _quiet(lambda: list(A.walk_movi_chunks(data, fourcc + 4, movi[0].data_end, gaps)))
    info = {}
    cut = _quiet(A.recording_cut_at, data, walked, sorted(gaps), info=info)
    return cut, info, walked, gaps


@pytest.mark.parametrize("j", [0, 3, 8, 20, 29])
def test_boundary_found_regardless_of_chunk_distance_to_gps(j):
    """30fps+오디오(초당 61청크): 경계 앞뒤 40청크 안에 GPS가 없어도 앞뒤 앵커로 판정한다."""
    data, _n = _prealloc(14 * 61 + 1 + 2 * j)
    cut, info, walked, gaps = _cut_of(data)
    assert gaps and cut == gaps[0][0] and info["uncertain"] is False
    assert all(w[0] < cut for w in walked if w[0] < gaps[0][0])


def test_midnight_crossing_is_continuous(tmp_path):
    chunks = movi_chunks(60, lambda s: rmc(23 * 3600 + 59 * 60 + 30 + s, date="160924" if s < 30 else "170924"))
    data, fourcc = build_avi(chunks, with_idx1=False)
    d = bytearray(data)
    start = fourcc + 4 + sum(len(ck(c, p)) for c, p in chunks[:28 * 2])
    d[start:start + 2500] = bytes(2500)
    cut, info, _w, gaps = _cut_of(bytes(d))
    assert gaps and cut is None


def test_long_zero_damage_within_bitrate_is_continuous():
    chunks = movi_chunks(600, lambda s: rmc(8 * 3600 + s), frame_bytes=10000)
    data, fourcc = build_avi(chunks, with_idx1=False)
    d = bytearray(data)
    sizes = [len(ck(c, p)) for c, p in chunks]
    s = fourcc + 4 + sum(sizes[:100 * 2])
    e = fourcc + 4 + sum(sizes[:450 * 2])
    d[s:e] = bytes(e - s)
    cut, info, _w, gaps = _cut_of(bytes(d))
    assert gaps and cut is None


def test_rtc_v_sentences_before_zero_gap_do_not_cut_current_recording(tmp_path):
    def text(sec):
        return rmc(sec, status="V") if sec < 20 else rmc(8 * 3600 + sec)
    chunks = movi_chunks(120, text, frame_bytes=10000)
    data, fourcc = build_avi(chunks, with_idx1=False)
    d = bytearray(data)
    s = fourcc + 4 + sum(len(ck(c, p)) for c, p in chunks[:18 * 2])
    d[s:s + 30000] = bytes(30000)
    cut, info, _w, gaps = _cut_of(bytes(d))
    assert gaps and cut is None
    p = tmp_path / "rtc.avi"
    p.write_bytes(bytes(d))
    rows, _ = _run_engine(str(p), str(tmp_path / "out"))
    # 20초의 문장은 0 덮어쓰기에 지워졌고(99개 남음), 뒤의 A 문장은 현재 녹화로 남는다
    assert sum(1 for r in rows if r["latitude"]) == 99
    assert rows[-1]["gps_utc_time"].startswith("08:01:59")


def test_gap_without_anchors_and_foreign_bytes_is_uncertain_boundary(tmp_path):
    """앞은 V 문장뿐, 뒤는 옛 녹화(GPS 없음): 근거가 없으면 경계 불확실로 자르고 뒤는 슬랙으로 남긴다."""
    data, _n = _prealloc(15 * 61, new_text=lambda s: rmc(s, status="V"),
                         old_text=lambda s: rmc(s, status="V"))
    cut, info, _w, gaps = _cut_of(data)
    assert gaps and cut == gaps[0][0] and info["uncertain"] is True
    p = tmp_path / "unc.avi"
    p.write_bytes(data)
    rows, out = _run_engine(str(p), str(tmp_path / "out"), extra=("--slack",))
    assert '"boundary_uncertain": true' in out
    assert os.path.isfile(os.path.join(tmp_path, "out", "unc", "slack", "slack_regions.csv"))
    regions = list(csv.DictReader(open(os.path.join(tmp_path, "out", "unc", "slack", "slack_regions.csv"))))
    assert any(r["region_kind"] == "after_cut_uncertain" for r in regions)


def test_placeholders_from_anchor_time_difference():
    chunks = movi_chunks(120, lambda s: rmc(8 * 3600 + s), fps=30, frame_bytes=2000)
    data, fourcc = build_avi(chunks, fps=30, with_idx1=False)
    d = bytearray(data)
    sizes = [len(ck(c, p)) for c, p in chunks]
    s = fourcc + 4 + sum(sizes[:40 * 31])
    e = fourcc + 4 + sum(sizes[:45 * 31])
    d[s:e] = bytes(e - s)
    entries = _quiet(A.synthesize_idx1, bytes(d), fourcc, len(d))
    ph = [x for x in entries if x["idx_offset"] < 0]
    by = {}
    for x in ph:
        by[x["chunk_id"]] = by.get(x["chunk_id"], 0) + 1
    assert by[b"01tx"] == 5 and 140 <= by[b"00dc"] <= 160
    assert A.SYNTH_INFO["placeholders"] == len(ph) and A.SYNTH_INFO["cut_at"] is None


# ---------------------------------------------------------------------------
# #9 movi 안 옛 hdrl
# ---------------------------------------------------------------------------

def test_old_hdrl_inside_movi_is_not_used_as_header():
    old = _old_recording(seconds=60, with_idx1=False)
    cur_chunks = movi_chunks(20, lambda s: rmc(8 * 3600 + s))
    cur, fourcc = build_avi(cur_chunks, tail=old)
    for strip_idx1 in (False, True):
        d = bytearray(cur)
        if strip_idx1:
            d = d[:d.rfind(b"idx1")]
        d[0:fourcc + 4] = bytes(fourcc + 4)   # RIFF ~ movi FourCC(현재 hdrl 포함) 소실
        hdrl, movi, idx1, _ = _quiet(A.find_top_level_sections, bytes(d))
        assert hdrl is None, strip_idx1
        assert movi and movi[0].content_start == fourcc + 4
        assert (idx1 is None) == strip_idx1
    # 시그니처만 깨진 파일의 현재 hdrl(movi 앞)은 그대로 쓴다
    d = bytearray(cur)
    d[0:8] = bytes(8)
    hdrl, movi, idx1, _ = _quiet(A.find_top_level_sections, bytes(d))
    assert hdrl is not None and hdrl.pos == 12 and movi[0].data_start == fourcc


# ---------------------------------------------------------------------------
# #27 base 후보 D / #80 앞부분 손상
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(6))
def test_base_candidate_D_cannot_beat_A_on_coincidence(seed):
    chunks = movi_chunks(120, lambda s: rmc(8 * 3600 + s), audio=True, frame_bytes=4500, seed=seed)
    data, fourcc = build_avi(chunks, audio=True)
    d = bytearray(data)
    d[fourcc + 4:fourcc + 4 + 100 * 1024] = bytes(100 * 1024)
    idx1 = d.rfind(b"idx1")
    entries = _quiet(A.parse_idx1, bytes(d), idx1 + 8, struct.unpack_from("<I", d, idx1 + 4)[0])
    base, label, scores, uncertain = _quiet(A.detect_base_offset, bytes(d), fourcc, entries)
    assert base == fourcc and label.startswith("A") and not uncertain


def test_head_damage_still_accepts_true_idx1_and_sniffs_tail(tmp_path):
    chunks = movi_chunks(120, lambda s: rmc(8 * 3600 + s), fps=5, frame_bytes=3000)
    data, fourcc = build_avi(chunks, fps=5)
    d = bytearray(data)
    d[0:400 * 1024] = bytes(400 * 1024)   # RIFF·hdrl·movi 머리·앞 청크 수백 개 소실
    idx1 = data.rfind(b"idx1")
    fb = _quiet(A.find_idx1_fallback, bytes(d))
    assert fb == (idx1, struct.unpack_from("<I", data, idx1 + 4)[0])
    hdrl, movi, idx1c, _ = _quiet(A.find_top_level_sections, bytes(d))
    assert hdrl is None and idx1c is not None and movi and movi[0].data_start == fourcc
    p = tmp_path / "head.avi"
    p.write_bytes(bytes(d))
    assert bb.sniff_damaged_container(str(p), window=256 * 1024)[0] == "avi"
    assert bb.detect_container(str(p))[0] == "avi"
    rows, _ = _run_engine(str(p), str(tmp_path / "out"))
    assert 80 <= sum(1 for r in rows if r["latitude"]) <= 120


# ---------------------------------------------------------------------------
# #63 빈 자리 엔트리 산출물
# ---------------------------------------------------------------------------

def test_placeholder_entries_are_not_written_as_chunks(tmp_path):
    chunks = movi_chunks(30, lambda s: rmc(8 * 3600 + s), frame_bytes=3000)
    data, fourcc = build_avi(chunks, with_idx1=False)
    d = bytearray(data)
    sizes = [len(ck(c, p)) for c, p in chunks]
    s = fourcc + 4 + sum(sizes[:10 * 2])
    e = fourcc + 4 + sum(sizes[:15 * 2])
    d[s:e] = bytes(e - s)
    p = tmp_path / "mid.avi"
    p.write_bytes(bytes(d))
    rows, out = _run_engine(str(p), str(tmp_path / "out"))
    index_csv = glob.glob(str(tmp_path / "out" / "**" / "index.csv"), recursive=True)[0]
    idx_rows = list(csv.DictReader(open(index_csv, encoding="utf-8")))
    ph = [r for r in idx_rows if r["validation"] == "DAMAGED_PLACEHOLDER"]
    assert len(ph) == 5 and all(r["output_file"] == "" and r["absolute_chunk_offset"] == "" for r in ph)
    assert not any("MISMATCH" in r["validation"] or "OUT_OF_RANGE" in r["validation"] for r in idx_rows)
    bins = glob.glob(str(tmp_path / "out" / "**" / "chunks" / "*.bin"), recursive=True)
    assert len(bins) == 25 and all(os.path.getsize(b) > 0 for b in bins)
    assert "ID_MISMATCH" not in out and "validation=" not in out
    assert len(rows) == 25 and abs(float(rows[-1]["start_time_sec"]) - 29.0) < 1e-6


# ---------------------------------------------------------------------------
# #23 다수결 / #107 NMEA 선택
# ---------------------------------------------------------------------------

def test_stream_kind_ignores_empty_chunks_and_accepts_half_parsed_nmea():
    assert A.classify_payload(b"\0" * 64)[0] == "empty"
    assert A.decide_stream_kind({"nmea_text": 45, "generic_text": 0, "record72": 0, "float_vector": 0,
                                 "binary": 0, "empty": 15}) == "text"
    assert A.decide_stream_kind({"nmea_text": 0, "generic_text": 0, "record72": 45, "float_vector": 0,
                                 "binary": 0, "empty": 15}) == "record72"
    assert A.decide_stream_kind({"nmea_text": 47, "generic_text": 0, "record72": 0, "float_vector": 0,
                                 "binary": 13, "empty": 0}) == "text"
    assert A.decide_stream_kind({"nmea_text": 3, "generic_text": 0, "record72": 0, "float_vector": 0,
                                 "binary": 57, "empty": 0}) is None
    assert A.decide_stream_kind({"nmea_text": 0, "generic_text": 0, "record72": 0, "float_vector": 0,
                                 "binary": 0, "empty": 60}) is None


def test_zero_padded_gps_stream_is_still_decoded(tmp_path):
    chunks = [(b"00dc", b"\xff\xd8" + bytes([s + 1]) * 1000 + b"\xff\xd9") for s in range(60)]
    chunks = [c for pair in zip(chunks, [(b"01tx", b"\0" * 64)] * 15 + [(b"01tx", rmc(8 * 3600 + s)) for s in range(15, 60)]) for c in pair]
    data, _ = build_avi(chunks)
    p = tmp_path / "pad.avi"
    p.write_bytes(data)
    rows, _ = _run_engine(str(p), str(tmp_path / "out"))
    assert sum(1 for r in rows if r["latitude"]) == 45


def test_valid_checksum_sentence_wins_over_sentence_type():
    good_gga = "GPGGA,082400.00,3730.0000,N,12700.0000,E,1,08,1.0,10.0,M,0.0,M,,"
    c = 0
    for ch in good_gga:
        c ^= ord(ch)
    good_gga = f"${good_gga}*{c:02X}"
    bad_rmc = "$GPRMC,082400.00,A,3730.0000,N,12700.0000,E,20.0,84.4,160924,,,A*00"
    payload = (bad_rmc + "\r\n" + good_gga + "\r\n").encode()
    assert A.find_embedded_nmea_text(payload).startswith("GPGGA")
    good_rmc = rmc(8 * 3600).decode().strip()
    assert A.find_embedded_nmea_text((good_gga + "\r\n" + good_rmc + "\r\n").encode()).startswith("GPRMC")
    no_cs = "$GPRMC,082400.00,A,3730.0000,N,12700.0000,E,20.0,84.4,160924,,,A"
    assert A.find_embedded_nmea_text((bad_rmc + "\r\n" + no_cs + "\r\n").encode()) == no_cs[1:]


# ---------------------------------------------------------------------------
# #118 CLI 출력 폴더 공유 / #120 formType 손상
# ---------------------------------------------------------------------------

def test_cli_reserves_out_dirs_across_avi_and_mp4(tmp_path, monkeypatch):
    import integration_mp4 as M
    data, _ = build_avi(movi_chunks(5, lambda s: rmc(8 * 3600 + s)))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    avi = tmp_path / "a" / "REC.avi"
    avi.write_bytes(data)
    mp4 = tmp_path / "b" / "REC.mp4"
    mp4.write_bytes(struct.pack(">I", 24) + b"ftyp" + b"isom" + bytes(12))
    picked = {}

    def fake_mp4_main(argv):
        M._USED_OUT_DIRS.clear()
        picked["dir"] = M.unique_out_dir(argv[argv.index("-o") + 1], argv[-1])
    monkeypatch.setattr(M, "main", fake_mp4_main)
    with contextlib.redirect_stdout(io.StringIO()):
        bb.main(["-o", str(tmp_path / "out"), str(avi), str(mp4)])
    assert os.path.isdir(tmp_path / "out" / "REC")
    assert os.path.normcase(picked["dir"]) == os.path.normcase(str(tmp_path / "out" / "REC_2"))
    assert not isinstance(A._USED_OUT_DIRS, bb._SharedReservations)


def test_riff_with_damaged_formtype_is_sniffed_as_avi(tmp_path):
    data, _ = build_avi(movi_chunks(5, lambda s: rmc(8 * 3600 + s)))
    d = bytearray(data)
    d[8:12] = bytes(4)
    p = tmp_path / "form.avi"
    p.write_bytes(bytes(d))
    container, note = bb.detect_container(str(p))
    assert container == "avi" and "formType 손상" in note
    w = tmp_path / "w.wav"
    w.write_bytes(b"RIFF" + struct.pack("<I", 36) + b"WAVE" + bytes(32))
    assert bb.detect_container(str(w))[0] is None
    rows, _ = _run_engine(str(p), str(tmp_path / "out"))
    assert len(rows) == 5
