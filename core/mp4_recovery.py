"""Non-destructive MP4/fMP4 salvage with separate, untrusted metadata carving.

Uses surviving sample tables first. With moov lost, codec-only reference settings
or in-band AVC parameters allow bounded NAL carving. Predictive pictures after
loss are discarded until an IDR. Output is review media, NOT the source time axis.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
import json
import math
import mmap
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from core.avi_recovery import RecoveryCancelled, _check, _hash
from core.mp4_structure import (InvalidMP4, MAX_TABLE, Track, discover,
                                fragment_samples, parse_moov)

MAX_FILE = 16 * 1024**3
MAX_NAL = 32 * 1024**2
TIMING_WARNING = ('회수된 프레임을 빈 구간 없이 이어 붙인 검토용 영상입니다. 원래 시각·길이, '
                  'GPS 동기화, 사고 시각·급가감속 판정에 사용하지 마세요. GPS·G센서는 파일 '
                  '전체에서 별도로 회수하며 슬랙/과거 녹화가 포함될 수 있습니다.')
NMEA = re.compile(rb'(?<![A-Za-z])\$?[A-Z]{2}(?:RMC|GGA),[\x20-\x7e]{10,180}?\*[0-9A-Fa-f]{2}')
SENSOR = re.compile(rb'gsensor[a-z]*,-?\d{1,8},\d{1,8},-?\d{1,8},-?\d{1,8},-?\d{1,8}(?=[;\r\n\x00])', re.I)


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
        return first, key, frame, 1 << s['frame_bits'], nal[0] >> 5 != 0, typ % 5


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
    waiting = True; previous_num = None
    for offset, size, sample in track.samples:
        check()
        try:
            if (offset < 0 or offset + size > limit or size > MAX_NAL * 2
                    or any(offset < b and a < offset + size for a, b in forbidden)):
                raise InvalidMP4('sample outside current media')
            p, end = offset, offset + size; frame = Frame(offset, size, sample)
            starts = 0
            while p < end:
                if p + track.length_size > end:
                    raise InvalidMP4('incomplete sample NAL length')
                n = int.from_bytes(buf[p:p + track.length_size], 'big'); p += track.length_size
                if not 0 < n <= MAX_NAL or p + n > end:
                    raise InvalidMP4('invalid sample NAL extent')
                nal = bytes(buf[p:p + n]); kind = nal_kind(nal, track.codec)
                if avc and kind in (7, 8):
                    avc.add(nal)
                if track.codec == 'h264' and kind in (1, 5):
                    first, key, number, modulus, ref, slice_type = avc.picture(nal)
                    starts += first == 0
                    if frame.key and frame.key != key:
                        raise InvalidMP4('multiple pictures in one sample')
                    frame.key, frame.frame_num, frame.modulus, frame.reference = key, number, modulus, ref
                    frame.b_picture = slice_type == 1
                    frame.idr |= kind == 5
                elif track.codec == 'hevc' and kind < 32:
                    starts += bool(nal[2] & 0x80)
                    frame.idr |= kind in (16, 17, 18, 19, 20)
                frame.nals.append((p, n)); p += n
            if starts != 1:
                raise InvalidMP4('missing/multiple picture starts')
            if offset in track.break_offsets or (previous_num is not None and sample != previous_num + 1):
                waiting = True; stats['prediction_breaks'] += 1
            previous_num = sample
            if frame.idr:
                waiting = False
            if waiting:
                stats['waiting_for_idr'] += 1
                continue
            yield frame
        except (InvalidMP4, ValueError, IndexError):
            waiting = True; stats['damaged_samples'] += 1; stats['prediction_breaks'] += 1


RAW_MARKER = re.compile(rb'\x00[\x00-\xff]{3}[\x01\x21\x41\x61\x05\x25\x45\x65\x06\x27\x47\x67\x28\x48\x68\x09]')


def carved_frames(buf, track, media, check, stats):
    """AVC length-prefixed NALs only; PCM/text gaps do not become video bytes."""
    if track.codec != 'h264' or track.length_size != 4:
        stats['withheld'] = 'moov 없는 NAL 카빙은 4바이트 길이 H.264만 지원합니다.'
        return
    avc = AVC(track.parameters); current = None; waiting = True; previous_ref = None
    for begin, end in media:
        check(); current = None; waiting = True; previous_ref = None; pos = begin
        while pos + 5 <= end:
            check()
            match = RAW_MARKER.search(buf, pos, min(end, pos + 1024 * 1024))
            if not match:
                pos = min(end, pos + 1024 * 1024 - 8)
                continue
            p = match.start(); pos = p + 1; n = int.from_bytes(buf[p:p + 4], 'big')
            if not 1 <= n <= MAX_NAL or p + 4 + n > end:
                continue
            nal = bytes(buf[p + 4:p + 4 + n])
            try:
                kind = nal_kind(nal, 'h264')
                if kind in (7, 8):
                    if kind == 7:
                        s = sps_info(nal)
                        if track.width and (track.width, track.height) != (s['width'], s['height']):
                            stats['withheld'] = '영상 본문에서 다른 해상도의 SPS가 발견되어 트랙 혼입 방지를 위해 생성을 보류했습니다.'
                            raise ValueError(stats['withheld'])
                        track.width, track.height = s['width'], s['height']
                        track.fps = track.fps or s['fps']
                    avc.add(nal)
                    if nal not in track.parameters:
                        track.parameters.append(nal)
                    pos = p + 4 + n
                    continue
                if kind not in (1, 5):
                    # AUD/SEI are not needed to synthesize access units or timing.
                    continue
                if n < 16:
                    continue
                first, key, number, modulus, ref, slice_type = avc.picture(nal)
                if first == 0:
                    if current and not waiting:
                        yield current
                    current = None
                    if kind == 5:
                        waiting = False; previous_ref = None
                    elif ref and previous_ref is not None and number not in (previous_ref, (previous_ref + 1) % modulus):
                        waiting = True; stats['prediction_breaks'] += 1
                    if waiting:
                        stats['waiting_for_idr'] += 1
                        pos = p + 4 + n
                        continue
                    current = Frame(p, n + 4, 0, idr=kind == 5, key=key, frame_num=number,
                                    modulus=modulus, reference=ref, b_picture=slice_type == 1)
                    if ref:
                        previous_ref = number
                elif current is None or current.key != key:
                    continue
                current.nals.append((p + 4, n)); current.size = p + 4 + n - current.offset
                pos = p + 4 + n
            except (InvalidMP4, IndexError):
                # A bad candidate inside audio is not proof of video loss. The
                # next slice frame number plus full decode decides continuity.
                continue
        if current and not waiting:
            yield current


def carve_metadata(buf, work, cancel):
    from core.paths import ensure_vendor_importable
    ensure_vendor_importable()
    from integration_mp4 import try_parse_nmea, classify_segment
    gps = sensor = 0
    with (work / 'recovered_gps.csv').open('x', newline='', encoding='utf-8-sig') as gf:
        w = csv.writer(gf)
        w.writerow(['source_offset', 'gps_date_utc', 'gps_time_utc', 'latitude', 'longitude',
                    'speed_kmh', 'scope', 'raw_nmea'])
        for m in NMEA.finditer(buf):
            _check(cancel); raw = m.group().decode('ascii'); rec = try_parse_nmea(raw)
            if (not rec or rec['checksum_ok'] is not True or not rec['status_valid'] or rec.get('mode') == 'N'
                    or rec['parse_warnings'] or rec['lat'] is None or rec['lon'] is None
                    or (rec['lat'] == 0 and rec['lon'] == 0)):
                continue
            # GGA has no UTC date. Preserve it as unknown, never borrow a date.
            try:
                datetime.fromisoformat((rec['date'] or '2000-01-01') + 'T' + rec['utc_time'])
            except ValueError:
                continue
            w.writerow([m.start(), rec['date'], rec['utc_time'], rec['lat'], rec['lon'],
                        rec.get('speed_kmh'), 'whole_file_untrusted', raw]); gps += 1
            if gps > MAX_TABLE:
                raise ValueError('GPS 회수 수가 안전 한도를 초과했습니다.')
    with (work / 'recovered_gsensor.csv').open('x', newline='', encoding='utf-8-sig') as sf:
        w = csv.writer(sf)
        w.writerow(['source_offset', 'x_raw', 'y_raw', 'z_raw', 'scale', 'x_g', 'y_g', 'z_g',
                    'scope', 'raw_sensor'])
        for m in SENSOR.finditer(buf):
            _check(cancel); raw = m.group().decode('ascii'); kind, rec = classify_segment(raw)
            values = [rec.get(k) for k in ('x_g', 'y_g', 'z_g')]
            if kind != 'gsensor' or not rec.get('scale') or not all(
                    isinstance(v, (float, int)) and math.isfinite(v) and abs(v) <= 1000 for v in values):
                continue
            w.writerow([m.start(), rec['x_raw'], rec['y_raw'], rec['z_raw'], rec['scale'], *values,
                        'whole_file_untrusted', raw]); sensor += 1
            if sensor > MAX_TABLE:
                raise ValueError('G센서 회수 수가 안전 한도를 초과했습니다.')
    return gps, sensor


def run_command(args, cancel, timeout=120):
    """No shell/network, cancellable, bounded log/progress files on disk."""
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
            if reason:
                return dict(status=reason, error='FFmpeg 실행 한도 초과', decoded_frames=0)
            log.seek(0); error = log.read(4096).decode('utf-8', 'replace')
            output.seek(0); frames = re.findall(rb'frame=(\d+)', output.read())
            return dict(status='passed' if proc.returncode == 0 and not error.strip() else 'failed',
                        decoded_frames=int(frames[-1]) if frames else 0, error=error)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()


def decode_check(path, count, cancel):
    exe = shutil.which('ffmpeg')
    if not exe:
        return dict(status='not_run', decoded_frames=0, reason='FFmpeg 없음')
    check = run_command([exe, '-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode',
        '-ec', '0', '-threads', '1', '-protocol_whitelist', 'file,pipe', '-i', str(path),
        '-map', '0:v:0', '-progress', 'pipe:1', '-f', 'null', '-'], cancel)
    if check['status'] == 'passed' and check['decoded_frames'] != count:
        check['status'] = 'incomplete'; check['reason'] = '회수 후보 수와 디코딩 프레임 수 불일치'
    return check


def write_elementary(path, buf, track, frames, cancel):
    with path.open('wb') as out:
        for frame in frames:
            _check(cancel)
            if frame.idr:
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


def export_video(work, buf, track, frames, cancel, progress):
    raw = work / f'recovered_track{track.id:02d}.{track.codec}'
    target = work / f'recovered_track{track.id:02d}.mp4'
    write_elementary(raw, buf, track, frames, cancel)
    result = mux(buf, target, track, frames, cancel)
    check = decode_check(target, len(frames), cancel) if result['status'] == 'passed' else result
    discarded = []
    if check['status'] in ('failed', 'incomplete'):
        progress(f'{track.id}번 영상: 독립 GOP별로 재검증 중...')
        groups = []; group = []
        for frame in frames:
            if frame.idr and group:
                groups.append(group); group = []
            group.append(frame)
        if group:
            groups.append(group)
        survivors = []
        for i, group in enumerate(groups):
            _check(cancel)
            write_elementary(raw, buf, track, group, cancel)
            result = mux(buf, target, track, group, cancel)
            test = decode_check(target, len(group), cancel) if result['status'] == 'passed' else result
            if test['status'] == 'passed':
                survivors.extend(group)
            else:
                discarded.append(dict(gop=i, candidate_frames=len(group), source_offset=group[0].offset,
                                      decode_check=test))
        frames = survivors
        write_elementary(raw, buf, track, frames, cancel)
        if frames:
            result = mux(buf, target, track, frames, cancel)
            check = decode_check(target, len(frames), cancel) if result['status'] == 'passed' else result
        else:
            check = dict(status='failed', decoded_frames=0, reason='검증에 성공한 독립 GOP 없음')
    # A failed mux can leave a header-only or corrupt file. Do not present it as recovery.
    if check['status'] not in ('passed', 'not_run'):
        target.unlink(missing_ok=True)
    return frames, dict(file=target.name if target.exists() else None, stream=track.id,
        codec=track.codec, candidate_frames=len(frames),
        sha256=_hash(target, cancel) if target.exists() else None, fps=str(track.fps) if track.fps else None,
        fps_basis='surviving_or_reference_configuration',
        elementary_file=raw.name, elementary_sha256=_hash(raw, cancel), decode_check=check,
        discarded_gops=discarded)


def recover_mp4(source, output_dir, reference=None, cancel=None, progress=None, *, fps=None):
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
        manifest = dict(schema=1, container='mp4', created_at_utc=datetime.now(timezone.utc).isoformat(),
            source_path=str(source), source_sha256=original_hash, source_size=size, source_copy=copied.name,
            copy_sha256_verified=True, original_timeline_preserved=False, warning=TIMING_WARNING,
            reference=None, videos=[], jpeg_stills=0, diagnostics=[], video_boundary_verified=False,
            video_withheld_reason=None)
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
                reference_tracks, reference_defaults, _ = parse_moov(ref, rmoov)
                reference_tracks = [t for t in reference_tracks if t.handler == b'vide']
                if not reference_tracks:
                    raise ValueError('참조 영상에 지원하는 H.264/HEVC 영상 트랙이 없습니다.')
                for t in reference_tracks:
                    t.samples.clear()  # Never transplant the reference's sample layout.
                    t.composition.clear()
                    t.break_offsets.clear()
            manifest['reference'] = dict(path=str(reference), sha256=rhash, usage='codec_settings_only')
        with copied.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            report('MP4 박스·샘플 표 및 손상 이후의 데이터 탐색 중...')
            boxes, limit, gaps = discover(buf, lambda: _check(cancel))
            media = [(b.payload, min(b.end, limit)) for b in boxes if b.kind == b'mdat']
            moov = next((b for b in boxes if b.kind == b'moov'), None)
            tracks, defaults = [], {}
            if moov:
                tracks, defaults, errors = parse_moov(buf, moov); manifest['diagnostics'].extend(errors)
            elif reference_tracks:
                tracks, defaults = reference_tracks, reference_defaults
            if tracks and any(b.kind == b'moof' for b in boxes):
                manifest['diagnostics'].extend(fragment_samples(buf, boxes, tracks, defaults,
                                                               lambda: _check(cancel)))
            videos = [t for t in tracks if t.handler == b'vide']
            for t in videos:
                if t.table_error:
                    manifest['diagnostics'].append(f'track {t.id}: {t.table_error}')
            # Header-free but surviving moov: verified source tables bound reading.
            indexed = any(t.samples for t in videos)
            if indexed:
                manifest['video_boundary_verified'] = True
                manifest['video_scan_range'] = dict(end=limit, basis='source_sample_tables',
                    media_ranges=media, moov_offset=moov.start if moov else None)
            elif media:
                # With no sample map, an embedded recording cannot be safely
                # joined. Keep only media before its first validated ftyp.
                media = [(a, min([b] + [x.start for x in boxes
                                       if x.kind == b'ftyp' and a <= x.start < b])) for a, b in media]
                manifest['video_boundary_verified'] = True
                manifest['video_scan_range'] = dict(end=limit, basis='surviving_mdat_extent', media_ranges=media)
                if not videos:
                    videos = reference_tracks or [Track(0, b'vide', codec='h264')]
                if len(videos) != 1:
                    videos = []
                    manifest['video_withheld_reason'] = '샘플 표 없이 여러 영상 트랙을 구별할 수 없어 영상 생성을 보류했습니다.'
            else:
                videos = []
                manifest['video_withheld_reason'] = '현재 영상의 본문 경계를 확인할 수 없어 슬랙 혼입 방지를 위해 영상 생성을 보류했습니다.'
                manifest['video_scan_range'] = dict(end=limit, basis='unverified', media_ranges=[])
            if indexed and reference_tracks:
                for t in videos:
                    r = next((r for r in reference_tracks if r.id == t.id), None)
                    if r and (r.codec, r.width, r.height, r.parameters) != (t.codec, t.width, t.height, t.parameters):
                        raise ValueError('참조 코덱 설정이 손상 파일의 살아 있는 설정과 다릅니다.')
            all_rows, nal_rows = [], []
            for t in videos:
                _check(cancel); stats = dict(damaged_samples=0, waiting_for_idr=0, prediction_breaks=0)
                if not t.samples and len(videos) > 1:
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
                    carved_frames(buf, t, media, lambda: _check(cancel), stats))
                frames = []
                try:
                    for frame in iterator:
                        if len(frames) >= MAX_TABLE:
                            raise ValueError('프레임 회수 수가 안전 한도를 초과했습니다.')
                        frames.append(frame)
                except (InvalidMP4, ValueError) as exc:
                    manifest['diagnostics'].append(f'track {t.id}: {exc}'); frames = []
                if not frames:
                    if stats.get('withheld'):
                        manifest['diagnostics'].append(stats['withheld'])
                    continue
                report(f'{t.id}번 영상: {len(frames)}개 후보 MP4 생성·전체 디코딩 검증 중...')
                frames, video = export_video(work, buf, t, frames, cancel, report)
                video['exclusions'] = stats; video['width'], video['height'] = t.width, t.height
                video['recovery_method'] = 'source_sample_tables' if t.samples else 'bounded_avc_nal_carving'
                if requested_fps:
                    video['fps_basis'] = 'user_supplied'
                manifest['videos'].append(video)
                for i, frame in enumerate(frames):
                    all_rows.append([t.id, i, frame.offset, frame.size, frame.sample, video['recovery_method']])
                    for p, n in frame.nals:
                        nal_rows.append([t.id, i, p, n])
            report('체크섬이 유효한 GPS 및 G센서 별도 회수 중...')
            manifest['gps_records'], manifest['gsensor_records'] = carve_metadata(buf, work, cancel)
            manifest['metadata_scope'] = 'whole_file_untrusted'
            manifest['damaged_box_gaps'] = gaps[:1000]
        for name, header, rows in [
                ('frame_offsets.csv', ['stream', 'recovered_frame_index', 'source_payload_offset',
                                      'source_payload_extent', 'source_sample_number', 'method'], all_rows),
                ('nal_offsets.csv', ['stream', 'recovered_frame_index', 'source_nal_offset', 'source_nal_size'], nal_rows)]:
            with (work / name).open('x', newline='', encoding='utf-8-sig') as log:
                w = csv.writer(log); w.writerow(header); w.writerows(rows)
        manifest['status'] = ('video_recovered' if any(v['decode_check']['status'] == 'passed' for v in manifest['videos'])
            else 'video_candidates_unverified' if manifest['videos'] else 'metadata_only'
            if manifest['gps_records'] or manifest['gsensor_records'] else 'nothing_recovered')
        manifest['artifact_sha256'] = {p.name: _hash(p, cancel) for p in work.iterdir() if p.suffix == '.csv'}
        if _hash(source, cancel) != original_hash:
            raise ValueError('복원 도중 원본이 변경됐습니다. 결과를 폐기합니다.')
        if reference and _hash(reference, cancel) != manifest['reference']['sha256']:
            raise ValueError('복원 도중 참조 영상이 변경됐습니다.')
        (work / 'recovery.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        (work / 'READ_ME.txt').write_text(TIMING_WARNING + '\n\n상태: ' + manifest['status'] + '\n'
            'FFmpeg 전체 디코딩 검증이 passed인 MP4만 검증된 복원본입니다.\n'
            '참조 영상의 코덱/FPS 설정만 사용하며 영상·GPS·샘플 표를 복사하지 않습니다.\n'
            '소실된 음성·GPS·영상 바이트와 원래 시간축을 만들어 넣지 않습니다.\n'
            'NAL 카빙 프레임의 extent에는 음성/텍스트 틈이 포함될 수 있습니다. 실제 복사된 영상 바이트는 '
            'nal_offsets.csv의 각 구간입니다.\n' + (manifest['video_withheld_reason'] or ''), encoding='utf-8')
        _check(cancel)
        if output.exists():
            raise FileExistsError('출력 폴더가 이미 있습니다.')
        os.rename(work, output)
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
    args = p.parse_args()
    result = recover_mp4(args.source, args.output, args.reference, progress=print, fps=args.fps)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
