"""Non-destructive MP4/fMP4 salvage with separate, untrusted metadata carving.

Uses surviving sample tables first. With moov lost, codec-only reference settings
or in-band AVC parameters allow bounded NAL carving. Predictive pictures after
loss are discarded until an IDR. Output is review media, NOT the source time axis.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from bisect import bisect_right
from fractions import Fraction
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import time

from core.avi_recovery import RecoveryCancelled, _check, _hash
from core.mp4_structure import (InvalidMP4, MAX_TABLE, Track, box_at, discover,
                                fragment_samples, parse_moov, trak_handlers)

MAX_FILE = 16 * 1024**3
MAX_NAL = 32 * 1024**2
DEFAULT_TIMEOUT = 180
# 카빙 중 연속된 두 그림 사이에 같은 바이트(0x00/0xFF)가 이 길이 이상 이어지면 손실 증거로
# 본다. 정확히 GOP 배수만큼 0으로 지워진 손실은 frame_num/POC가 우연히 이어져 비트스트림
# 만으로는 구별되지 않는다. 음성 샘플 하나는 이 길이에 미치지 않는다.
IDENTICAL_RUN = 64 * 1024
TIMING_WARNING = ('회수된 프레임을 빈 구간 없이 이어 붙인 검토용 영상입니다. 원래 시각·길이, '
                  'GPS 동기화, 사고 시각·급가감속 판정에 사용하지 마세요. GPS·G센서는 파일 '
                  '전체에서 별도로 회수하며 슬랙/과거 녹화가 포함될 수 있습니다.')
NMEA = re.compile(rb'(?<![A-Za-z])\$?[A-Z]{2}(?:RMC|GGA),[\x20-\x7e]{10,180}?\*[0-9A-Fa-f]{2}')
SENSOR = re.compile(rb'gsensor[a-z]*,-?\d{1,8},\d{1,8},-?\d{1,8},-?\d{1,8},-?\d{1,8}(?=[;\r\n\x00])', re.I)


class RecoveryTimedOut(TimeoutError):
    pass


class RecoveryDeadline:
    """A shared wall-clock budget, including hashing, parsing and all children."""
    def __init__(self, cancel, seconds, protected_dirs=()):
        self.cancel = cancel
        self.deadline = time.monotonic() + seconds
        # 증거 폴더·출력 폴더 아래의 실행 파일은 검증 도구로 쓰지 않는다.
        self.protected_dirs = tuple(Path(d).resolve() for d in protected_dirs)

    def is_set(self):
        if self.cancel is not None and self.cancel.is_set():
            return True
        if self.remaining <= 0:
            raise RecoveryTimedOut('MP4 복원 전체 시간 제한에 도달했습니다. 임시 결과를 정리했습니다.')
        return False

    @property
    def remaining(self):
        return self.deadline - time.monotonic()


def bounded_matches(pattern, buf, check):
    """Check cancellation/deadline even when a huge region has no matches."""
    previous_end = 0
    for begin in range(0, len(buf), 1024*1024):
        check(); end = min(len(buf), begin+1024*1024)
        for match in pattern.finditer(buf, max(begin, previous_end), min(len(buf), end+256)):
            if match.start() >= end:
                break
            previous_end = match.end()
            yield match


class Bits:
    def __init__(self, data):
        self.data = data; self.pos = 0

    def get(self, width=1):
        if width < 0 or width > 32 or self.pos + width > len(self.data) * 8:
            raise InvalidMP4('incomplete bitstream header')
        value = 0
        for _ in range(width):
            value = (value << 1) | ((self.data[self.pos // 8] >> (7 - self.pos % 8)) & 1)
            self.pos += 1
        return value

    def ue(self):
        zeros = 0
        while not self.get():
            zeros += 1
            if zeros > 30:
                raise InvalidMP4('invalid Exp-Golomb code')
        return (1 << zeros) - 1 + self.get(zeros)

    def se(self):
        value = self.ue()
        return (value + 1) // 2 if value & 1 else -(value // 2)


def rbsp(nal):
    return re.sub(b'\x00\x00\x03', b'\x00\x00', nal)


def sps_info(nal):
    b = Bits(rbsp(nal[1:])); profile = b.get(8); b.get(8); b.get(8); sid = b.ue()
    if sid > 31:
        raise InvalidMP4('invalid SPS id')
    chroma = 1; separate = 0
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = b.ue()
        if chroma > 3:
            raise InvalidMP4('invalid chroma format')
        if chroma == 3:
            separate = b.get()
        if b.ue() > 6 or b.ue() > 6:
            raise InvalidMP4('unsupported bit depth')
        b.get()
        if b.get():
            for i in range(8 if chroma != 3 else 12):
                if b.get():
                    last = following = 8
                    for _ in range(16 if i < 6 else 64):
                        if following:
                            following = (last + b.se() + 256) % 256
                        last = following or last
    frame_bits = b.ue() + 4; poc = b.ue(); poc_bits = 0; delta_zero = 0
    if not 4 <= frame_bits <= 16 or poc > 2:
        raise InvalidMP4('invalid picture order configuration')
    if poc == 0:
        poc_bits = b.ue() + 4
        if not 4 <= poc_bits <= 16:
            raise InvalidMP4('invalid picture order bits')
    elif poc == 1:
        delta_zero = b.get(); b.se(); b.se(); count = b.ue()
        if count > 255:
            raise InvalidMP4('invalid POC cycle')
        for _ in range(count):
            b.se()
    if b.ue() > 32:
        raise InvalidMP4('invalid reference picture count')
    b.get(); mbw, mbh = b.ue() + 1, b.ue() + 1; frame_only = b.get()
    if not frame_only:
        b.get()
    b.get(); crop = [b.ue() for _ in range(4)] if b.get() else [0] * 4
    subw, subh = ((1, 1) if chroma in (0, 3) else (2, 2) if chroma == 1 else (2, 1))
    width = mbw * 16 - (crop[0] + crop[1]) * subw
    height = mbh * 16 * (2 - frame_only) - (crop[2] + crop[3]) * subh * (2 - frame_only)
    if not 0 < width <= 16384 or not 0 < height <= 16384:
        raise InvalidMP4('invalid coded dimensions')
    fps = None
    if b.get():  # VUI, optional timing used only when actual SPS supplies it.
        if b.get():
            if b.get(8) == 255:
                b.get(16); b.get(16)
        if b.get():
            b.get()
        if b.get():
            b.get(3); b.get()
            if b.get():
                b.get(8); b.get(8); b.get(8)
        if b.get():
            b.ue(); b.ue()
        if b.get():
            tick, scale = b.get(32), b.get(32); b.get()
            if tick and scale:
                candidate = Fraction(scale, 2 * tick)
                if 1 <= candidate <= 240:
                    fps = candidate
    return dict(id=sid, width=width, height=height, frame_bits=frame_bits, poc=poc,
                poc_bits=poc_bits, delta_zero=delta_zero, frame_only=frame_only,
                separate=separate, max_mb=mbw * mbh * (2 - frame_only), fps=fps)


def pps_info(nal):
    b = Bits(rbsp(nal[1:])); pid, sid = b.ue(), b.ue(); b.get(); bottom = b.get()
    if pid > 255 or sid > 31 or b.ue() != 0:
        raise InvalidMP4('invalid PPS or unsupported slice groups')
    return dict(id=pid, sps=sid, bottom=bottom)


class AVC:
    def __init__(self, parameters):
        self.sps, self.pps = {}, {}
        for nal in parameters:
            self.add(nal)

    def add(self, nal):
        kind = nal[0] & 31
        if kind == 7:
            s = sps_info(nal); self.sps[s['id']] = s
        elif kind == 8:
            p = pps_info(nal); self.pps[p['id']] = p

    def picture(self, nal):
        kind = nal[0] & 31
        b = Bits(rbsp(nal[1:128])); first, typ, pid = b.ue(), b.ue(), b.ue()
        if typ > 9 or pid not in self.pps or self.pps[pid]['sps'] not in self.sps:
            raise InvalidMP4('unknown/corrupt slice configuration')
        p = self.pps[pid]; s = self.sps[p['sps']]
        if first >= s['max_mb']:
            raise InvalidMP4('invalid macroblock address')
        if s['separate']:
            b.get(2)
        frame = b.get(s['frame_bits']); field = bottom = 0
        if kind == 5 and (typ % 5 not in (2, 4) or frame != 0 or nal[0] >> 5 == 0):
            raise InvalidMP4('invalid IDR slice')
        if not s['frame_only']:
            field = b.get()
            if field:
                bottom = b.get()
        iid = b.ue() if kind == 5 else 0
        order = []
        if s['poc'] == 0:
            order.append(b.get(s['poc_bits']))
            if p['bottom'] and not field:
                order.append(b.se())
        elif s['poc'] == 1 and not s['delta_zero']:
            order.append(b.se())
            if p['bottom'] and not field:
                order.append(b.se())
        key = (pid, frame, field, bottom, kind == 5, iid, nal[0] >> 5 != 0, *order)
        # POC LSB는 key 끝이 아니라 따로 돌려준다. bottom_field_pic_order 플래그가 켜진
        # PPS에서는 key[-1]이 delta 값이다(#129).
        poc_lsb = order[0] if s['poc'] == 0 else None
        return first, key, frame, 1 << s['frame_bits'], nal[0] >> 5 != 0, typ % 5, poc_lsb


@dataclass
class Frame:
    offset: int
    size: int
    sample: int
    nals: list[tuple[int, int]] = field(default_factory=list)
    idr: bool = False
    key: tuple = ()
    frame_num: int = 0
    modulus: int = 0
    reference: bool = False
    b_picture: bool = False
    cra: bool = False
    rasl: bool = False
    poc_lsb: int | None = None

    @property
    def random_access(self):
        return self.idr or self.cra


def nal_kind(nal, codec):
    # Ambarella AVC appends a small zero alignment/CABAC tail inside the
    # declared NAL. Do not mistake legal padding for a zero-filled picture.
    body = nal[1:].rstrip(b'\0')
    if (not nal or nal[0] & 0x80 or len(nal) - 1 - len(body) > 64
            or b'\0\0\0' in body):
        raise InvalidMP4('zero-filled/invalid NAL')
    if codec == 'h264':
        kind = nal[0] & 31
        if kind not in (1, 5, 6, 7, 8, 9, 10, 11, 12):
            raise InvalidMP4('unsupported AVC NAL type')
    else:
        if len(nal) < 3 or not nal[1] & 7 or ((nal[0] & 1) << 5 | nal[1] >> 3):
            raise InvalidMP4('invalid/multilayer HEVC NAL')
        kind = (nal[0] >> 1) & 63
        if kind > 40:
            raise InvalidMP4('unsupported HEVC NAL type')
    return kind


def indexed_frames(buf, track, limit, media, check, stats, forbidden=()):
    avc = AVC(track.parameters) if track.codec == 'h264' else None
    waiting = True; previous_num = None; suppress_rasl = False
    for offset, size, sample in track.samples:
        check()
        try:
            if (offset < 0 or offset + size > limit or size > MAX_NAL * 2
                    or any(offset < b and a < offset + size for a, b in forbidden)):
                raise InvalidMP4('sample outside current media')
            p, end = offset, offset + size; frame = Frame(offset, size, sample)
            starts = 0
            while p < end:
                check()
                if p + track.length_size > end:
                    raise InvalidMP4('incomplete sample NAL length')
                n = int.from_bytes(buf[p:p + track.length_size], 'big'); p += track.length_size
                if not 0 < n <= MAX_NAL or p + n > end:
                    raise InvalidMP4('invalid sample NAL extent')
                nal = bytes(buf[p:p + n]); kind = nal_kind(nal, track.codec)
                if avc and kind in (7, 8):
                    avc.add(nal)
                if track.codec == 'h264' and kind in (1, 5):
                    first, key, number, modulus, ref, slice_type, poc_lsb = avc.picture(nal)
                    starts += first == 0
                    if frame.key and frame.key != key:
                        raise InvalidMP4('multiple pictures in one sample')
                    frame.key, frame.frame_num, frame.modulus, frame.reference = key, number, modulus, ref
                    frame.poc_lsb = poc_lsb
                    frame.b_picture = slice_type == 1
                    frame.idr |= kind == 5
                elif track.codec == 'hevc' and kind < 32:
                    starts += bool(nal[2] & 0x80)
                    frame.idr |= kind in (16, 17, 18, 19, 20)
                    frame.cra |= kind == 21
                    frame.rasl |= kind in (8, 9)
                frame.nals.append((p, n)); p += n
            if starts != 1:
                raise InvalidMP4('missing/multiple picture starts')
            if offset in track.break_offsets or (previous_num is not None and sample != previous_num + 1):
                waiting = True; stats['prediction_breaks'] += 1
            previous_num = sample
            if frame.random_access:
                # CRA starts a clean random-access sequence only after its
                # RASL leading pictures are discarded. In an intact sequence
                # those pictures still have their references and are retained.
                suppress_rasl = waiting and frame.cra
                waiting = False
            if suppress_rasl and frame.rasl:
                stats['discarded_rasl'] = stats.get('discarded_rasl', 0) + 1
                continue
            if waiting:
                stats['waiting_for_idr'] += 1
                continue
            yield frame
        except (InvalidMP4, ValueError, IndexError):
            waiting = True; stats['damaged_samples'] += 1; stats['prediction_breaks'] += 1


RAW_MARKER = re.compile(rb'\x00[\x00-\xff]{3}[\x01\x21\x41\x61\x05\x25\x45\x65\x06\x27\x47\x67\x28\x48\x68\x09]')


def identical_run(buf, begin, end):
    """[begin, end)에 같은 바이트(0x00 또는 0xFF)가 IDENTICAL_RUN 이상 이어지는지."""
    if end - begin < IDENTICAL_RUN:
        return False
    return (buf.find(bytes(IDENTICAL_RUN), begin, end) != -1
            or buf.find(b'\xff' * IDENTICAL_RUN, begin, end) != -1)


class NalChain:
    """카빙 중 '직전 NAL 끝에 이어지는' 후보만 믿기 위한 사슬 상태.

    - end: 마지막으로 받아들인(또는 대기 중이라 건너뛴) 실제 NAL의 끝. 여기에 정확히
      이어지는 후보(linked)는 같은 샘플의 다음 NAL이다. 그 밖의 후보는 음성/텍스트 안의
      우연한 일치일 수 있으므로 길이만큼 건너뛰지 않고 파라미터셋으로도 쓰지 않는다(#38).
    - pending: 사슬 밖에서 시작한 비VCL 후보들(AUD/SEI/SPS/PPS…). 바로 이어지는 VCL 첫
      슬라이스가 나타나야 실제 샘플 머리로 확정되어 파라미터셋 비교에 쓰인다.
    """
    def __init__(self):
        self.end = None; self.pending = []; self.pending_end = None

    def linked(self, p):
        return self.end is not None and p == self.end

    def accept(self, p, n):
        self.end = p + 4 + n; self.pending = []; self.pending_end = None

    def offer(self, p, n, nal, kind):
        """비VCL 후보를 임시 사슬에 넣는다. 사슬 안쪽 후보는 무시한다."""
        if self.pending and p == self.pending_end:
            self.pending.append((nal, kind)); self.pending_end = p + 4 + n
        elif self.pending and p < self.pending_end:
            return
        else:
            self.pending = [(nal, kind)]; self.pending_end = p + 4 + n

    def confirm(self, p):
        """VCL 첫 슬라이스가 임시 사슬에 이어지면 그 파라미터셋을 돌려준다."""
        confirmed = list(self.pending) if self.pending and p == self.pending_end else []
        self.pending = []; self.pending_end = None
        return confirmed


def carved_frames(buf, track, media, check, stats, *, fixed_parameters=False, stop_at_break=False):
    """Length-prefixed NALs only; PCM/text gaps do not become video bytes.

    - 참조·moov의 파라미터셋이 있으면 그 집합을 고정한다(#35). 사슬에 이어진 다른
      SPS/PPS가 나오면 다른 채널의 혼입 가능성이 있어 생성을 보류한다.
    - 모든 그림(비참조 포함)에 frame_num = 직전 참조 + 1을 요구한다(#32).
    - 같은 그림의 다음 슬라이스는 직전 NAL 끝에 이어질 때만 붙인다(#37). 이어지지
      않으면 사이가 손실된 것이므로 그림을 버리고 다음 IDR까지 기다린다.
    - stop_at_break: 본문 경계를 확인할 수 없는 mdat(크기 0·잘림)에서는 연속성이 끊긴
      뒤를 슬랙으로 보고 카빙을 멈춘다(#33). stats['slack_after']에 위치를 기록한다.
    """
    if track.codec == 'hevc':
        from core.hevc_recovery import carved_hevc_frames
        yield from carved_hevc_frames(buf, track, media, check, stats, stop_at_break=stop_at_break)
        return
    if track.codec != 'h264' or track.length_size != 4:
        stats['withheld'] = 'moov 없는 NAL 카빙은 4바이트 길이 H.264만 지원합니다.'
        return
    avc = AVC(track.parameters)
    fixed = fixed_parameters or bool(track.parameters)

    def accept_parameter(nal, kind):
        if fixed and nal not in track.parameters:
            raise ValueError('AVC parameters changed inside unindexed media; channel mixing withheld')
        if kind == 7:
            sps = sps_info(nal)
            if track.width and (track.width, track.height) != (sps['width'], sps['height']):
                stats['withheld'] = '영상 본문에서 다른 해상도의 SPS가 발견되어 트랙 혼입 방지를 위해 생성을 보류했습니다.'
                raise ValueError(stats['withheld'])
            track.width, track.height = sps['width'], sps['height']
            track.fps = track.fps or sps['fps']
        avc.add(nal)
        if nal not in track.parameters:
            track.parameters.append(nal)

    for begin, end in media:
        check(); current = None; waiting = True; previous_ref = None; pos = begin
        chain = NalChain(); halted = False

        def broken(where):
            nonlocal waiting, current, halted
            waiting = True; current = None; stats['prediction_breaks'] += 1
            if stop_at_break:
                stats['slack_after'] = where; halted = True

        while pos + 5 <= end and not halted:
            check()
            match = RAW_MARKER.search(buf, pos, min(end, pos + 1024 * 1024))
            if not match:
                pos = min(end, pos + 1024 * 1024 - 8)
                continue
            p = match.start(); pos = p + 1; n = int.from_bytes(buf[p:p + 4], 'big')
            if not 1 <= n <= MAX_NAL or p + 4 + n > end:
                continue
            nal = bytes(buf[p + 4:p + 4 + n]); linked = chain.linked(p)
            try:
                kind = nal_kind(nal, 'h264')
                if kind not in (1, 5):
                    # AUD/SEI/SPS/PPS: 사슬에 이어진 것만 샘플의 일부로 믿는다.
                    if linked:
                        if kind in (7, 8):
                            accept_parameter(nal, kind)
                        chain.accept(p, n); pos = p + 4 + n
                    else:
                        chain.offer(p, n, nal, kind)
                    continue
                first, key, number, modulus, ref, slice_type, poc_lsb = avc.picture(nal)
                if first == 0:
                    for pending_nal, pending_kind in chain.confirm(p):
                        if pending_kind in (7, 8):
                            accept_parameter(pending_nal, pending_kind)
                    if n < 16 and not linked:
                        continue
                    if current and not waiting:
                        yield current
                    current = None
                    run = chain.end is not None and identical_run(buf, chain.end, p)
                    lost = run
                    if kind == 5:
                        # 경계가 확인된 본문에서 손실 뒤의 IDR은 정상 재개다. 경계를 확인할
                        # 수 없는 본문에서는 그 뒤가 옛 녹화일 수 있어 슬랙으로 본다.
                        lost = run and stop_at_break
                        if not lost:
                            waiting = False; previous_ref = None
                    elif previous_ref is not None:
                        expected = (previous_ref + 1) % modulus
                        # 비참조 그림도 frame_num = PrevRefFrameNum + 1 이어야 한다.
                        # 필드 쌍의 둘째 필드만 같은 값을 가질 수 있다.
                        lost = lost or (number != expected and not (key[2] and number == previous_ref))
                    if lost:
                        broken(p)
                        if halted:
                            break
                    chain.accept(p, n); pos = p + 4 + n
                    if waiting:
                        stats['waiting_for_idr'] += 1
                        continue
                    current = Frame(p, n + 4, 0, idr=kind == 5, key=key, frame_num=number,
                                    modulus=modulus, reference=ref, b_picture=slice_type == 1, poc_lsb=poc_lsb)
                    current.nals.append((p + 4, n))
                    if ref:
                        previous_ref = number
                elif current is None or current.key != key:
                    continue
                elif not linked:
                    # 같은 그림의 슬라이스가 직전 NAL에 이어지지 않는다: 사이가 손실됐다.
                    stats['damaged_samples'] += 1
                    broken(p)
                else:
                    current.nals.append((p + 4, n)); current.size = p + 4 + n - current.offset
                    chain.accept(p, n); pos = p + 4 + n
            except (InvalidMP4, IndexError):
                # A bad candidate inside audio is not proof of video loss. The
                # next slice frame number plus full decode decides continuity.
                continue
        if current and not waiting and not halted:
            yield current


def carve_metadata(buf, work, cancel, boxes=(), limit=None, tracks=()):
    from core.paths import ensure_vendor_importable
    ensure_vendor_importable()
    from integration_mp4 import try_parse_nmea, classify_segment
    limit = len(buf) if limit is None else limit
    owners = sorted((b for b in boxes if b.kind != b'ftyp'), key=lambda b: b.start)
    owner_starts = [b.start for b in owners]
    text = sorted((a, a+n) for t in tracks if t.handler in (b'text', b'sbtl', b'subt', b'meta')
                  for a, n, _ in t.samples if 0 <= a < a+n <= limit)
    text_starts = [a for a, _ in text]
    def region(begin, end):
        if begin >= limit:
            return 'after_first_recording'
        i = bisect_right(owner_starts, begin) - 1
        owner = owners[i] if i >= 0 and end <= owners[i].end else None
        if owner and owner.kind in (b'free', b'skip'):
            return 'free_or_skip'
        j = bisect_right(text_starts, begin) - 1
        if owner and owner.kind == b'mdat' and j >= 0 and end <= text[j][1]:
            return 'surviving_metadata_sample'
        if owner and begin >= owner.payload:
            return owner.kind.decode('ascii', 'replace') + '_unindexed'
        return 'unallocated_or_damaged'
    gps = sensor = 0
    with (work / 'recovered_gps.csv').open('x', newline='', encoding='utf-8-sig') as gf:
        w = csv.writer(gf)
        w.writerow(['source_offset', 'gps_date_utc', 'gps_time_utc', 'latitude', 'longitude',
                    'speed_kmh', 'scope', 'raw_nmea', 'source_region', 'idas_outlier_reason'])
        for m in bounded_matches(NMEA, buf, lambda: _check(cancel)):
            _check(cancel); raw = m.group().decode('ascii'); rec = try_parse_nmea(raw)
            if (not rec or rec['checksum_ok'] is not True or not rec['status_valid'] or rec.get('mode') == 'N'
                    or rec['parse_warnings'] or rec['lat'] is None or rec['lon'] is None
                    or not math.isfinite(rec['lat']) or not math.isfinite(rec['lon'])
                    or abs(rec['lat']) > 90 or abs(rec['lon']) > 180
                    or (abs(rec['lat']) < 1e-6 and abs(rec['lon']) < 1e-6)):
                continue
            # GGA has no UTC date. Preserve it as unknown, never borrow a date.
            try:
                datetime.fromisoformat((rec['date'] or '2000-01-01') + 'T' + rec['utc_time'])
            except ValueError:
                continue
            speed = rec.get('speed_kmh')
            outlier = ('invalid_speed' if speed is not None and
                       (not math.isfinite(speed) or not 0 <= speed <= 300) else '')
            w.writerow([m.start(), rec['date'], rec['utc_time'], rec['lat'], rec['lon'],
                        speed, 'whole_file_untrusted', raw, region(m.start(), m.end()), outlier]); gps += 1
            if gps > MAX_TABLE:
                raise ValueError('GPS 회수 수가 안전 한도를 초과했습니다.')
    with (work / 'recovered_gsensor.csv').open('x', newline='', encoding='utf-8-sig') as sf:
        w = csv.writer(sf)
        w.writerow(['source_offset', 'x_raw', 'y_raw', 'z_raw', 'scale', 'x_g', 'y_g', 'z_g',
                    'scope', 'raw_sensor', 'source_region'])
        for m in bounded_matches(SENSOR, buf, lambda: _check(cancel)):
            _check(cancel); raw = m.group().decode('ascii'); kind, rec = classify_segment(raw)
            values = [rec.get(k) for k in ('x_g', 'y_g', 'z_g')]
            if kind != 'gsensor' or not rec.get('scale') or not all(
                    isinstance(v, (float, int)) and math.isfinite(v) and abs(v) <= 1000 for v in values):
                continue
            w.writerow([m.start(), rec['x_raw'], rec['y_raw'], rec['z_raw'], rec['scale'], *values,
                        'whole_file_untrusted', raw, region(m.start(), m.end())]); sensor += 1
            if sensor > MAX_TABLE:
                raise ValueError('G센서 회수 수가 안전 한도를 초과했습니다.')
    return gps, sensor


def find_tool(name, cancel=None):
    """PATH에서 검증 도구를 찾되, 현재 디렉터리와 증거·출력 폴더 아래의 실행 파일은 거부한다.

    Windows의 shutil.which는 현재 디렉터리를 먼저 찾는다. 증거 폴더에 놓인 ffmpeg.exe가
    검증 도구로 실행되면 안 된다. 거부되면 도구가 없는 것으로 취급한다(not_run).
    """
    exe = shutil.which(name)
    if not exe:
        return None
    try:
        resolved = Path(exe).resolve()
        protected = [Path.cwd().resolve()] + list(getattr(cancel, 'protected_dirs', ()))
        for base in protected:
            if resolved == base or base in resolved.parents:
                return None
    except OSError:
        return None
    return exe


def command_timeout(cancel, timeout=None):
    """명령별 제한 시간: 전체 시간 제한이 있으면 그 남은 시간, 없으면 120초(또는 지정값)."""
    if isinstance(cancel, RecoveryDeadline):
        return cancel.remaining if timeout is None else min(timeout, cancel.remaining)
    return 120 if timeout is None else timeout


def run_command(args, cancel, timeout=None, *, capture_stdout=False):
    """No shell/network, cancellable, bounded log/progress files on disk.

    명령별 제한 시간은 남은 전체 시간이다(#89). 고정 120초는 긴 녹화의 디코딩 검증을
    중간에 끊어 정상 결과를 지웠다. 전체 시간 제한이 없으면 120초를 쓴다.
    """
    _check(cancel)
    timeout = command_timeout(cancel, timeout)
    with tempfile.TemporaryFile() as log, tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output, stderr=log)
        start = time.monotonic(); reason = ''
        try:
            while proc.poll() is None:
                _check(cancel)
                if time.monotonic() - start > timeout:
                    reason = 'timeout'; break
                if os.fstat(log.fileno()).st_size > 8 * 1024**2 or os.fstat(output.fileno()).st_size > 8 * 1024**2:
                    reason = 'log_limit'; break
                time.sleep(.03)
            _check(cancel)
            # A short-lived process can exit between polls. Its final output
            # must satisfy the same limits as a still-running process.
            if os.fstat(log.fileno()).st_size > 8 * 1024**2 or os.fstat(output.fileno()).st_size > 8 * 1024**2:
                reason = 'log_limit'
            if reason:
                return dict(status=reason, error='FFmpeg 실행 한도 초과', decoded_frames=0,
                            returncode=proc.poll(), elapsed_seconds=round(time.monotonic()-start, 3))
            log.seek(0); errors = log.read()
            # Inspect the complete bounded log, keeping only a short preview
            # in the manifest. Whitespace before an error cannot hide it.
            error = errors.decode('utf-8', 'replace').strip()[:4096]
            output.seek(0); stdout = output.read(); frames = re.findall(rb'frame=(\d+)', stdout)
            result = dict(status='passed' if proc.returncode == 0 and not error.strip() else 'failed',
                        decoded_frames=int(frames[-1]) if frames else 0, error=error,
                        returncode=proc.returncode, stderr_bytes=len(errors),
                        elapsed_seconds=round(time.monotonic()-start, 3))
            if capture_stdout:
                result['stdout'] = stdout.decode('utf-8', 'replace')
            return result
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()


def decode_check(path, count, cancel):
    exe = find_tool('ffmpeg', cancel)
    if not exe:
        return dict(status='not_run', decoded_frames=0, reason='FFmpeg 없음')
    check = run_command([exe, '-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode+bitstream+buffer+careful',
        '-threads', '1', '-protocol_whitelist', 'file,pipe', '-i', str(path),
        '-map', '0:v:0', '-progress', 'pipe:1', '-f', 'null', '-'], cancel)
    if check['status'] == 'passed' and check['decoded_frames'] != count:
        check['status'] = 'incomplete'; check['reason'] = '회수 후보 수와 디코딩 프레임 수 불일치'
    return check


def probe_check(path, track, count, cancel):
    exe = find_tool('ffprobe', cancel)
    if not exe:
        return dict(status='not_run', reason='FFprobe 없음')
    result = run_command([exe, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-count_packets',
                          '-show_streams', '-of', 'json', str(path)], cancel, capture_stdout=True)
    stdout = result.pop('stdout', '')
    if result['status'] != 'passed':
        return result
    try:
        streams = json.loads(stdout)['streams']
        if (len(streams) != 1 or streams[0]['codec_type'] != 'video'
                or streams[0]['codec_name'] != track.codec
                or (streams[0]['width'], streams[0]['height']) != (track.width, track.height)
                or int(streams[0]['nb_read_packets']) != count
                or Fraction(streams[0]['r_frame_rate']) != track.fps):
            raise ValueError('independent container/sample count mismatch')
        result['video_packets'] = count
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        result.update(status='failed', reason=str(exc))
    return result


def verification_tools(cancel):
    result = {}
    for name in ('ffmpeg', 'ffprobe'):
        exe = find_tool(name, cancel)
        if not exe:
            located = shutil.which(name)
            result[name] = dict(available=False, **(dict(rejected_path=located,
                reason='현재 디렉터리·증거 폴더 아래의 실행 파일은 검증 도구로 쓰지 않습니다.') if located else {}))
            continue
        version = run_command([exe, '-version'], cancel, timeout=5, capture_stdout=True)
        result[name] = dict(available=True, path=exe, sha256=_hash(exe, cancel),
            version=version.pop('stdout', '').splitlines()[:1], version_check=version)
    return result


def write_elementary(path, buf, track, frames, cancel):
    with path.open('wb') as out:
        for frame in frames:
            _check(cancel)
            if frame.random_access:
                for nal in track.parameters:
                    out.write(b'\0\0\0\1' + nal)
            for p, n in frame.nals:
                out.write(b'\0\0\0\1'); out.write(buf[p:p + n])


def mux(buf, target, track, frames, cancel):
    from core.mp4_writer import write_mp4
    try:
        write_mp4(target, buf, track, frames, lambda: _check(cancel))
        return dict(status='passed')
    except InvalidMP4 as exc:
        target.unlink(missing_ok=True)
        return dict(status='not_run', reason=str(exc))


def audit_mp4(path, source, track, frames, cancel):
    """Reopen the result and compare every copied NAL to its evidence extent.

    Decode success alone cannot prove provenance. Check the actual output
    sample map, codec configuration and byte content before accepting it.
    """
    try:
        source_hash, output_hash = hashlib.sha256(), hashlib.sha256()
        with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            boxes = []; p = 0
            while p < len(data):
                _check(cancel)
                if len(boxes) >= 3:
                    raise InvalidMP4('unexpected recovered container contents')
                b = box_at(data, p, len(data)); boxes.append(b); p = b.end
            if [b.kind for b in boxes] != [b'ftyp', b'mdat', b'moov']:
                raise InvalidMP4('unexpected recovered container layout')
            tracks, _, errors = parse_moov(data, boxes[2], lambda: _check(cancel))
            if errors or len(tracks) != 1 or tracks[0].handler != b'vide':
                raise InvalidMP4('recovered container must have exactly one video track')
            actual = tracks[0]
            parameters = ([n for kind in (7, 8) for n in track.parameters if n[0] & 31 == kind]
                          if track.codec == 'h264' else track.parameters)
            if (actual.table_error or len(actual.samples) != len(frames)
                    or (actual.codec, actual.width, actual.height, actual.length_size, actual.fps)
                    != (track.codec, track.width, track.height, 4, track.fps)
                    or actual.parameters != parameters):
                raise InvalidMP4('recovered sample map/configuration mismatch')
            total = 0
            for (offset, size, _), frame in zip(actual.samples, frames):
                _check(cancel)
                if not boxes[1].payload <= offset < offset + size <= boxes[1].end:
                    raise InvalidMP4('recovered sample outside media')
                p = offset
                for source_offset, length in frame.nals:
                    if (not 0 <= source_offset < source_offset + length <= len(source)
                            or p + 4 + length > offset + size
                            or int.from_bytes(data[p:p+4], 'big') != length):
                        raise InvalidMP4('recovered NAL extent mismatch')
                    expected = source[source_offset:source_offset+length]
                    copied = data[p+4:p+4+length]
                    if expected != copied:
                        raise InvalidMP4('recovered NAL differs from evidence bytes')
                    prefix = length.to_bytes(4, 'big')
                    source_hash.update(prefix); source_hash.update(expected)
                    output_hash.update(prefix); output_hash.update(copied)
                    total += 1; p += 4 + length
                if p != offset + size:
                    raise InvalidMP4('unexpected bytes in recovered sample')
        return dict(status='passed', matched_frames=len(frames), matched_nals=total,
                    source_nal_sha256=source_hash.hexdigest(), output_nal_sha256=output_hash.hexdigest())
    except (InvalidMP4, ValueError, IndexError, struct.error) as exc:
        return dict(status='failed', reason=str(exc))


MAX_DECODE_ATTEMPTS = 256


def export_video(work, buf, track, frames, cancel, progress):
    """후보 전체를 검증하고, 실패하면 GOP 단위 이분 탐색으로 통과하는 접두를 찾는다(#36).

    GOP마다 따로 검증하면 긴 녹화에서 시도 횟수·시간 상한에 걸려 멀쩡한 GOP까지 버렸다.
    여기서는 '지금까지 통과한 생존 집합 + 다음 GOP들'을 이분 탐색해 처음 실패하는 GOP를
    찾고, 그 GOP 안에서만 프레임 접두를 이분 탐색한다. 생존 집합은 항상 실제로 mux·대조·
    디코딩을 통과한 집합이며, 상한에 걸리면 그때까지의 생존 집합으로 결과를 만든다.
    """
    raw = work / f'recovered_track{track.id:02d}.{track.codec}'
    target = work / f'recovered_track{track.id:02d}.mp4'
    initial_count = len(frames); attempts = 0
    deadline = time.monotonic() + 300
    limited = None

    def validate(candidate, *, final=False):
        nonlocal attempts, limited
        _check(cancel)
        if not candidate:
            return dict(status='passed', decoded_frames=0), dict(status='not_run')
        if not final and (attempts >= MAX_DECODE_ATTEMPTS or time.monotonic() >= deadline
                          or limited is not None):
            limited = limited or dict(status='verification_limit', reason='트랙 검증 시간/횟수 한도 초과')
            return limited, dict(status='not_run')
        attempts += 1
        result = mux(buf, target, track, candidate, cancel)
        if result['status'] != 'passed':
            return result, dict(status='not_run')
        payload = audit_mp4(target, buf, track, candidate, cancel)
        if payload['status'] != 'passed':
            return dict(status='failed', reason='원본 NAL 대조 실패', decoded_frames=0), payload
        probe = probe_check(target, track, len(candidate), cancel)
        if probe['status'] not in ('passed', 'not_run'):
            return dict(status='failed', reason='독립 MP4 구조 검증 실패', probe_check=probe), payload
        decoded = decode_check(target, len(candidate), cancel)
        decoded['probe_check'] = probe
        return decoded, payload

    def flatten(groups):
        return [f for g in groups for f in g]

    check, payload = validate(frames)
    discarded = []
    if check['status'] in ('failed', 'incomplete'):
        progress(f'{track.id}번 영상: 독립 GOP별로 재검증 중...')
        groups = []; group = []
        for frame in frames:
            if frame.random_access and group:
                groups.append(group); group = []
            group.append(frame)
        if group:
            groups.append(group)
        survivors = []; i = 0; previous_truncated = False
        while i < len(groups) and limited is None:
            _check(cancel)
            # 직전 GOP를 버리거나 잘랐으면 이어지는 CRA의 RASL은 참조를 잃었다.
            if previous_truncated and groups[i][0].cra:
                kept = [f for f in groups[i] if not f.rasl]
                if len(kept) != len(groups[i]):
                    discarded.append(dict(gop=i, candidate_frames=len(groups[i]), kept_prefix_frames=len(kept),
                        source_offset=groups[i][0].offset, reason='CRA random access: RASL excluded'))
                groups[i] = kept
            # survivors + groups[i:k]가 통과하는 최대 k를 이분 탐색한다.
            low, high = i, len(groups)
            # 첫 반복의 '남은 전체'는 방금 실패한 전체 후보와 같으므로 다시 검증하지 않는다.
            test = check if i == 0 else validate(survivors + flatten(groups[i:high]))[0]
            if test['status'] == 'passed':
                survivors += flatten(groups[i:high]); i = high
                break
            if test['status'] not in ('failed', 'incomplete'):
                break
            while high - low > 1:
                middle = (low + high) // 2
                test, _ = validate(survivors + flatten(groups[i:middle]))
                if test['status'] == 'passed':
                    low = middle
                elif test['status'] in ('failed', 'incomplete'):
                    high = middle
                else:
                    break
            if limited is not None:
                survivors += flatten(groups[i:low]); i = low
                break
            survivors += flatten(groups[i:low])
            bad = groups[low]; i = low + 1
            # 처음 실패한 GOP 안에서 통과하는 프레임 접두를 이분 탐색한다. 첫 손상
            # 그림은 뒤의 예측을 깨지만 앞의 멀쩡한 접두까지 지울 필요는 없다.
            frame_low, frame_high = 0, len(bad); last = None
            for _ in range(16):
                if frame_high - frame_low <= 1:
                    break
                middle = (frame_low + frame_high) // 2
                test, group_payload = validate(survivors + bad[:middle])
                if test['status'] == 'passed':
                    frame_low = middle
                elif test['status'] in ('failed', 'incomplete'):
                    frame_high = middle; last = (test, group_payload)
                else:
                    break
            survivors += bad[:frame_low]; previous_truncated = True
            discarded.append(dict(gop=low, candidate_frames=len(bad), kept_prefix_frames=frame_low,
                source_offset=bad[0].offset, decode_check=last[0] if last else dict(status='failed'),
                payload_check=last[1] if last else dict(status='not_run')))
        if i < len(groups):
            discarded.append(dict(gop=i, candidate_frames=sum(len(g) for g in groups[i:]), kept_prefix_frames=0,
                source_offset=groups[i][0].offset, decode_check=limited or dict(status='not_run'),
                reason='검증 한도에 걸려 뒤쪽 GOP는 검사하지 않았습니다.'))
        frames = survivors
        if frames:
            check, payload = validate(frames, final=True)
        else:
            check = dict(status='failed', decoded_frames=0, reason='검증에 성공한 독립 GOP 없음')
    # A failed mux can leave a header-only or corrupt file. Do not present it as recovery.
    if check['status'] not in ('passed', 'not_run'):
        target.unlink(missing_ok=True)
    if frames:
        write_elementary(raw, buf, track, frames, cancel)
    return frames, dict(file=target.name if target.exists() else None, stream=track.id,
        codec=track.codec, candidate_frames=len(frames), initial_candidate_frames=initial_count,
        sha256=_hash(target, cancel) if target.exists() else None, fps=str(track.fps) if track.fps else None,
        fps_basis='surviving_or_reference_configuration',
        elementary_file=raw.name if raw.exists() else None,
        elementary_sha256=_hash(raw, cancel) if raw.exists() else None, decode_check=check,
        payload_check=payload, decode_attempts=attempts, discarded_gops=discarded,
        verification_limit=limited)


def publish_result(work, output, cancel):
    """Exclusively reserve the destination; publish the manifest last.

    Directory rename replaces an empty destination on POSIX. mkdir provides
    the same no-clobber rule on Windows and Linux, including competing jobs.
    """
    output.mkdir()
    moved = []
    try:
        for path in sorted(work.iterdir(), key=lambda p: (p.name == 'recovery.json', p.name)):
            _check(cancel)
            target = output/path.name
            os.rename(path, target); moved.append(target)
        work.rmdir()
    except BaseException:
        for path in reversed(moved):
            path.unlink(missing_ok=True)
        try:
            output.rmdir()
        except OSError:
            pass  # Preserve any file created by another process.
        raise


def recover_mp4(source, output_dir, reference=None, cancel=None, progress=None, *, fps=None,
                timeout=DEFAULT_TIMEOUT):
    """New result directory, verified evidence copy, reference CONFIGURATION ONLY.

    Compressed source pictures are remuxed without encoding, sound, or GPS. The
    original timeline is always marked unpreserved, even for a complete recovery.
    """
    source, output = Path(source).resolve(), Path(output_dir).resolve()
    reference = Path(reference).resolve() if reference else None
    if output.exists():
        raise FileExistsError('출력 폴더가 이미 있습니다. 새 폴더를 선택하세요.')
    size = source.stat().st_size
    if source.suffix.lower() not in ('.mp4', '.mov', '.m4v') or not 8 <= size <= MAX_FILE:
        raise ValueError('지원 대상: 8바이트 이상, 16GiB 이하의 MP4/MOV/M4V')
    if reference == source:
        raise ValueError('정상 참조 영상은 손상 원본과 달라야 합니다.')
    requested_fps = Fraction(str(fps)) if fps is not None else None
    if requested_fps is not None and not 1 <= requested_fps <= 240:
        raise ValueError('FPS는 1~240 범위로 지정하세요.')
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError('전체 복원 시간 제한은 0초 초과, 3600초 이하여야 합니다.')
    started = time.monotonic()
    cancel = RecoveryDeadline(cancel, timeout, protected_dirs=(source.parent, output))
    _check(cancel); output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='.mp4-recovery-', dir=output.parent))
    report = progress or (lambda _: None)
    try:
        report('원본 SHA-256 및 복원 사본 확인 중...')
        original_hash = _hash(source, cancel); copied = work / 'damaged_source.mp4'
        with source.open('rb') as src, copied.open('xb') as dst:
            while block := src.read(1024 * 1024):
                _check(cancel); dst.write(block)
        if copied.stat().st_size != size or _hash(copied, cancel) != original_hash:
            raise ValueError('원본과 복원용 사본의 SHA-256이 다릅니다.')
        manifest = dict(schema=2, container='mp4', created_at_utc=datetime.now(timezone.utc).isoformat(),
            source_path=str(source), source_sha256=original_hash, source_size=size, source_copy=copied.name,
            copy_sha256_verified=True, copy_sha256_verified_at_completion=False,
            original_timeline_preserved=False, warning=TIMING_WARNING,
            verification_tools=verification_tools(cancel),
            reference=None, videos=[], jpeg_stills=0, diagnostics=[], video_boundary_verified=False,
            video_withheld_reason=None)
        manifest['execution_limits'] = dict(total_timeout_seconds=timeout, command_timeout_seconds='remaining_total',
            max_decode_attempts_per_track=MAX_DECODE_ATTEMPTS, metadata_scan_chunk_bytes=1024*1024)
        reference_tracks, reference_defaults = [], {}
        if reference:
            if not 8 <= reference.stat().st_size <= MAX_FILE:
                raise ValueError('참조 영상 크기가 지원 한도를 초과했습니다.')
            rhash = _hash(reference, cancel)
            with reference.open('rb') as rf, mmap.mmap(rf.fileno(), 0, access=mmap.ACCESS_READ) as ref:
                rb, _, _ = discover(ref, lambda: _check(cancel))
                rmoov = next((b for b in rb if b.kind == b'moov'), None)
                if rmoov is None:
                    raise ValueError('참조 MP4에 유효한 moov가 없습니다.')
                reference_tracks, reference_defaults, _ = parse_moov(ref, rmoov, lambda: _check(cancel))
                if any(b.kind == b'moof' for b in rb):
                    # A fragmented reference has an empty stts in its init
                    # moov. Derive only its actual cadence before discarding
                    # every reference position, picture and timestamp.
                    fragment_samples(ref, rb, reference_tracks, reference_defaults,
                                     lambda: _check(cancel))
                reference_tracks = [t for t in reference_tracks if t.handler == b'vide']
                if not reference_tracks:
                    raise ValueError('참조 영상에 지원하는 H.264/HEVC 영상 트랙이 없습니다.')
                for t in reference_tracks:
                    t.samples.clear()  # Never transplant the reference's sample layout.
                    t.composition.clear()
                    t.decode_times.clear(); t.durations.clear()
                    t.break_offsets.clear()
            manifest['reference'] = dict(path=str(reference), sha256=rhash, usage='codec_settings_only')
        with copied.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            report('MP4 박스·샘플 표 및 손상 이후의 데이터 탐색 중...')
            boxes, limit, gaps = discover(buf, lambda: _check(cancel))
            moov = next((b for b in boxes if b.kind == b'moov'), None)
            tracks, defaults = [], {}
            # 영상 채널 수: 파싱에 실패한 trak(둘째 카메라의 avcC 손상 등)과 trak이 없는
            # traf도 센다(#34). 구별할 수 없는 채널이 둘 이상이면 카빙을 보류한다.
            unknown_channels = []
            if moov:
                tracks, defaults, errors = parse_moov(buf, moov, lambda: _check(cancel))
                manifest['diagnostics'].extend(errors)
                handlers = [h for _, h in trak_handlers(buf, moov, lambda: _check(cancel))]
                # 파싱에 성공한 trak의 handler를 하나씩 빼면 실패한 trak의 handler만 남는다.
                for t in tracks:
                    if t.handler in handlers:
                        handlers.remove(t.handler)
                unknown_channels.extend(f'trak(handler={h.decode("ascii", "replace") if h else "?"})'
                                        for h in handlers if h in (None, b'vide'))
            elif reference_tracks:
                tracks, defaults = reference_tracks, reference_defaults
            fragment_state = {}
            if tracks and any(b.kind == b'moof' for b in boxes):
                manifest['diagnostics'].extend(fragment_samples(buf, boxes, tracks, defaults,
                                                               lambda: _check(cancel), fragment_state))
                unknown_channels.extend(f'traf(track_id={tid})' for tid in sorted(fragment_state.get('unknown_track_ids', ())))
            recording_end = fragment_state.get('recording_end')
            if recording_end is not None:
                # 옛 fragment(순번 역행·되돌아간 tfdt) 뒤는 현재 녹화가 아니다(#12).
                limit = min(limit, recording_end)
                boxes = [b for b in boxes if b.start < limit]
                gaps = [(a, min(b, limit)) for a, b in gaps if a < limit]
                manifest['recording_end_offset'] = recording_end
            media = [(b.payload, min(b.end, limit)) for b in boxes if b.kind == b'mdat' and b.payload < limit]
            if unknown_channels:
                manifest['unknown_video_channels'] = unknown_channels
            videos = [t for t in tracks if t.handler == b'vide']
            channel_count = len(videos) + len(unknown_channels)
            for t in videos:
                if t.table_error:
                    manifest['diagnostics'].append(f'track {t.id}: {t.table_error}')
            # Header-free but surviving moov: verified source tables bound reading.
            indexed = any(t.samples for t in videos)
            boundary_unverified = [b.start for b in boxes if b.kind == b'mdat' and (b.truncated or b.unbounded)]
            if indexed:
                manifest['video_boundary_verified'] = True
                manifest['video_scan_range'] = dict(end=limit, basis='source_sample_tables',
                    media_ranges=media, moov_offset=moov.start if moov else None)
            elif media:
                # With no sample map, an embedded recording cannot be safely
                # joined. Keep only media before its first validated ftyp.
                media = [(a, min([b] + [x.start for x in boxes
                                       if x.kind == b'ftyp' and a <= x.start < b])) for a, b in media]
                if boundary_unverified:
                    # 크기 0이거나 파일보다 큰 mdat는 경계 근거가 아니다(#33). 카빙은 하되
                    # 연속성이 끊기는 지점 뒤는 슬랙으로 분리하고 경계 미확인으로 기록한다.
                    manifest['video_boundary_verified'] = False
                    manifest['video_scan_range'] = dict(end=limit, basis='unverified_mdat_extent', media_ranges=media,
                        unbounded_mdat_offsets=boundary_unverified)
                    manifest['diagnostics'].append(
                        'mdat 크기가 0이거나 파일을 넘어 현재 녹화의 끝을 확인할 수 없습니다. 연속성이 끊기는 '
                        '지점 뒤는 슬랙으로 분리했지만, 그 앞까지 옛 녹화가 이어 붙어 있을 가능성은 배제하지 못합니다.')
                else:
                    manifest['video_boundary_verified'] = True
                    manifest['video_scan_range'] = dict(end=limit, basis='surviving_mdat_extent', media_ranges=media)
                if not videos:
                    if not reference_tracks:
                        # moov도 참조도 없으면 채널 수와 설정을 알 수 없다(#35). 본문 안의
                        # SPS/PPS만으로는 설정이 같은 두 카메라를 구별할 수 없으므로 보류한다.
                        videos = []
                        manifest['video_withheld_reason'] = ('moov와 참조 영상이 모두 없어 영상 채널 수와 코덱 설정을 '
                            '확인할 수 없습니다. 같은 기기·설정의 정상 참조 영상(--reference)을 지정하세요.')
                    else:
                        videos = reference_tracks
                if videos and channel_count != 1 or len(videos) > 1:
                    videos = []
                    manifest['video_withheld_reason'] = '샘플 표 없이 여러 영상 트랙을 구별할 수 없어 영상 생성을 보류했습니다.'
            else:
                videos = []
                manifest['video_withheld_reason'] = '현재 영상의 본문 경계를 확인할 수 없어 슬랙 혼입 방지를 위해 영상 생성을 보류했습니다.'
                manifest['video_scan_range'] = dict(end=limit, basis='unverified', media_ranges=[])
            if reference_tracks:
                for t in videos:
                    r = next((r for r in reference_tracks if r.id == t.id), None)
                    if r and (r.codec, r.width, r.height, r.parameters) != (t.codec, t.width, t.height, t.parameters):
                        raise ValueError('참조 코덱 설정이 손상 파일의 살아 있는 설정과 다릅니다.')
                    if r:
                        t.fps = t.fps or r.fps
            all_rows, nal_rows = [], []
            for t in videos:
                _check(cancel); stats = dict(damaged_samples=0, waiting_for_idr=0, prediction_breaks=0)
                if not t.samples and channel_count > 1:
                    reason = (f'{t.id}번 영상의 샘플 표가 소실되어 여러 영상 채널을 구별할 수 없습니다. '
                              '채널 혼입 방지를 위해 해당 영상 생성을 보류했습니다.')
                    manifest['diagnostics'].append(reason)
                    manifest['video_withheld_reason'] = '\n'.join(filter(None, (
                        manifest['video_withheld_reason'], reason)))
                    continue
                t.fps = requested_fps or t.fps
                if t.codec == 'h264':
                    try:
                        avc = AVC(t.parameters)
                        if not t.fps:
                            t.fps = next((s['fps'] for s in avc.sps.values() if s['fps']), None)
                    except InvalidMP4 as exc:
                        manifest['diagnostics'].append(f'track {t.id} codec: {exc}')
                        continue
                forbidden = [(b.start, b.end) for b in boxes if b.kind not in (b'mdat',)]
                iterator = indexed_frames(buf, t, limit, media, lambda: _check(cancel), stats, forbidden) if t.samples else (
                    carved_frames(buf, t, media, lambda: _check(cancel), stats,
                                  stop_at_break=not manifest['video_boundary_verified']))
                frames = []
                orphaned = []
                withheld_ranges = []
                try:
                    for frame in iterator:
                        if len(frames) >= MAX_TABLE:
                            raise ValueError('프레임 회수 수가 안전 한도를 초과했습니다.')
                        frames.append(frame)
                    # A missing moof/trun does not imply that its mdat pictures
                    # are gone. Only a single verified video channel may carve
                    # media with no declarations; never scan free/slack bytes.
                    if t.samples and channel_count > 1 and unknown_channels:
                        manifest['diagnostics'].append(
                            f'track {t.id}: 구별할 수 없는 영상 채널({", ".join(unknown_channels)})이 있어 '
                            '색인 없는 mdat 구간의 카빙을 보류했습니다.')
                    if t.samples and channel_count == 1:
                        starts = sorted(a for a, _, _ in t.samples)
                        for a, b in media:
                            j = bisect_right(starts, a-1)
                            if j == len(starts) or starts[j] >= b:
                                stop = min([b]+[x.start for x in boxes
                                    if x.kind == b'ftyp' and a <= x.start < b])
                                if a < stop:
                                    orphaned.append((a, stop))
                        if orphaned:
                            for a, b in orphaned:
                                # Carving has weaker channel evidence than source
                                # tables. Commit a complete range only after its
                                # settings agree; failed ranges cannot erase indexed
                                # frames or contaminate their codec configuration.
                                trial = replace(t, parameters=list(t.parameters))
                                pending = []
                                previous_reason = stats.pop('withheld', None)
                                try:
                                    for frame in carved_frames(buf, trial, [(a, b)],
                                            lambda: _check(cancel), stats, fixed_parameters=True):
                                        if len(frames) + len(pending) >= MAX_TABLE:
                                            raise ValueError('프레임 회수 수가 안전 한도를 초과했습니다.')
                                        pending.append(frame)
                                except (InvalidMP4, ValueError) as exc:
                                    stats['withheld'] = str(exc)
                                    pending = []
                                reason = stats.get('withheld')
                                if reason:
                                    withheld_ranges.append(dict(start=a, end=b, reason=reason))
                                    manifest['diagnostics'].append(f'track {t.id} unindexed [{a}, {b}): {reason}')
                                elif previous_reason:
                                    stats['withheld'] = previous_reason
                                frames.extend(pending)
                        frames.sort(key=lambda f: f.offset)
                except (InvalidMP4, ValueError) as exc:
                    manifest['diagnostics'].append(f'track {t.id}: {exc}'); frames = []
                if not frames:
                    if stats.get('withheld'):
                        manifest['diagnostics'].append(stats['withheld'])
                    continue
                report(f'{t.id}번 영상: {len(frames)}개 후보 MP4 생성·전체 디코딩 검증 중...')
                frames, video = export_video(work, buf, t, frames, cancel, report)
                video['exclusions'] = stats; video['width'], video['height'] = t.width, t.height
                raw_method = 'bounded_hevc_nal_carving' if t.codec == 'hevc' else 'bounded_avc_nal_carving'
                mixed = bool(t.samples and any(f.sample == 0 for f in frames))
                video['recovery_method'] = ('hybrid_sample_tables_and_bounded_nal_carving' if mixed else
                    'source_sample_tables' if t.samples else raw_method)
                video['unindexed_media_ranges'] = orphaned
                video['withheld_unindexed_media_ranges'] = withheld_ranges
                if stats.get('slack_after') is not None:
                    video['slack_after_offset'] = stats['slack_after']
                    manifest['diagnostics'].append(f'track {t.id}: 오프셋 {stats["slack_after"]} 이후는 연속성이 끊겨 '
                                                   '슬랙(옛 녹화 가능)으로 제외했습니다.')
                video['carved_frames'] = sum(f.sample == 0 for f in frames)
                video['source_sample_number_basis'] = ('surviving_fragment_declarations'
                    if t.samples and any(b.kind == b'moof' for b in boxes) else 'source_stbl_ordinal'
                    if t.samples else 'unknown')
                if requested_fps:
                    video['fps_basis'] = 'user_supplied'
                manifest['videos'].append(video)
                for i, frame in enumerate(frames):
                    number = frame.sample - 1
                    dts = t.decode_times[number] if 0 <= number < len(t.decode_times) else None
                    duration = t.durations[number] if 0 <= number < len(t.durations) else None
                    pts = dts + (t.composition[number] if number < len(t.composition) else 0) if dts is not None else None
                    all_rows.append([t.id, i, frame.offset, frame.size, frame.sample,
                        'source_sample_tables' if frame.sample else raw_method,
                        video['source_sample_number_basis'] if frame.sample else 'unknown',
                        dts, pts, duration, t.timescale if dts is not None else None])
                    for p, n in frame.nals:
                        nal_rows.append([t.id, i, p, n])
            report('체크섬이 유효한 GPS 및 G센서 별도 회수 중...')
            manifest['gps_records'], manifest['gsensor_records'] = carve_metadata(buf, work, cancel, boxes, limit, tracks)
            manifest['metadata_scope'] = 'whole_file_untrusted'
            manifest['damaged_box_gaps'] = gaps[:1000]
        for name, header, rows in [
                ('frame_offsets.csv', ['stream', 'recovered_frame_index', 'source_payload_offset',
                                      'source_payload_extent', 'source_sample_number', 'method', 'source_sample_number_basis',
                                      'source_dts_ticks', 'source_pts_ticks', 'source_duration_ticks', 'source_timescale'], all_rows),
                ('nal_offsets.csv', ['stream', 'recovered_frame_index', 'source_nal_offset', 'source_nal_size'], nal_rows)]:
            with (work / name).open('x', newline='', encoding='utf-8-sig') as log:
                w = csv.writer(log); w.writerow(header); w.writerows(rows)
        manifest['status'] = ('video_recovered' if any(v['decode_check']['status'] == 'passed' for v in manifest['videos'])
            else 'video_candidates_unverified' if any(v['candidate_frames'] for v in manifest['videos']) else 'metadata_only'
            if manifest['gps_records'] or manifest['gsensor_records'] else 'nothing_recovered')
        manifest['artifact_sha256'] = {p.name: _hash(p, cancel) for p in work.iterdir() if p.suffix == '.csv'}
        if _hash(source, cancel) != original_hash:
            raise ValueError('복원 도중 원본이 변경됐습니다. 결과를 폐기합니다.')
        if copied.stat().st_size != size or _hash(copied, cancel) != original_hash:
            raise ValueError('복원 도중 사본이 변경됐습니다. 결과를 폐기합니다.')
        manifest['copy_sha256_verified_at_completion'] = True
        for video in manifest['videos']:
            for filename, expected in ((video['file'], video['sha256']),
                                       (video['elementary_file'], video['elementary_sha256'])):
                if filename and (not (work/filename).is_file() or _hash(work/filename, cancel) != expected):
                    raise ValueError('검증 이후 복원 결과가 변경됐습니다. 결과를 폐기합니다.')
        if reference and _hash(reference, cancel) != manifest['reference']['sha256']:
            raise ValueError('복원 도중 참조 영상이 변경됐습니다.')
        for tool in manifest['verification_tools'].values():
            if tool['available'] and _hash(tool['path'], cancel) != tool['sha256']:
                raise ValueError('복원 도중 검증 도구가 변경됐습니다. 결과를 폐기합니다.')
        (work / 'READ_ME.txt').write_text(TIMING_WARNING + '\n\n상태: ' + manifest['status'] + '\n'
            '원본 NAL 바이트 대조 및 FFmpeg 전체 디코딩 검증이 모두 passed인 MP4만 검증된 복원본입니다.\n'
            'FFprobe가 있으면 코덱·해상도·FPS·실제 패킷 수도 독립적으로 확인합니다.\n'
            '디코딩 성공은 손상 전 화소·실제 시각의 일치나 모든 비트 손상 검출을 보증하지 않습니다.\n'
            '참조 영상의 코덱/FPS 설정만 사용하며 영상·GPS·샘플 표를 복사하지 않습니다.\n'
            '소실된 음성·GPS·영상 바이트와 원래 시간축을 만들어 넣지 않습니다.\n'
            'NAL 카빙 프레임의 extent에는 음성/텍스트 틈이 포함될 수 있습니다. 실제 복사된 영상 바이트는 '
            'nal_offsets.csv의 각 구간입니다.\n'
            'frame_offsets.csv의 source DTS/PTS는 살아 있는 구조 값이며 UTC나 GPS 동기화 시각이 아닙니다. '
            '소실된 fragment의 샘플 번호는 추정하지 않습니다.\n'
            'GPS/G센서의 source_region은 바이트 위치 분류이며 현재 녹화·시각 동기화를 보증하지 않습니다.\n'
            'idas_outlier_reason이 있는 GPS는 이상치입니다. 모든 회수 메타데이터를 지도·통계·위험운전 판정에 '
            '자동 사용하지 않습니다. GGA의 날짜는 미상으로 남깁니다.\n' + (manifest['video_withheld_reason'] or ''), encoding='utf-8')
        manifest['artifact_sha256']['READ_ME.txt'] = _hash(work/'READ_ME.txt', cancel)
        manifest['elapsed_seconds'] = round(time.monotonic()-started, 3)
        (work / 'recovery.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        for name, expected in manifest['artifact_sha256'].items():
            if _hash(work/name, cancel) != expected:
                raise ValueError('복원 결과 기록이 변경됐습니다. 결과를 폐기합니다.')
        _check(cancel)
        publish_result(work, output, cancel)
        return dict(manifest, output_dir=str(output))
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def main():
    import sys
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    p = argparse.ArgumentParser(description='손상 MP4/fMP4 영상·GPS·G센서 회수 (원본 보존)')
    p.add_argument('source'); p.add_argument('output', help='존재하지 않는 새 결과 폴더')
    p.add_argument('--reference', help='같은 기기/설정의 정상 MP4 (코덱 설정만 사용)')
    p.add_argument('--fps', help='실제 촬영 FPS를 아는 경우만 지정 (예: 30000/1001)')
    p.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT,
                   help='전체 복원 시간 제한(초), 기본 180초. 최대 3600초')
    args = p.parse_args()
    try:
        result = recover_mp4(args.source, args.output, args.reference, progress=print, fps=args.fps,
                             timeout=args.timeout)
    except (RecoveryTimedOut, RecoveryCancelled) as exc:
        p.exit(2, str(exc)+'\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
