"""Non-destructive AVI salvage. Recovered media has NO original playback time axis.

Classic RIFF AVI, MJPEG and Annex-B H.264 video; NMEA RMC is recovered separately.
This is deliberately independent of the normal/slack analysis pipeline. See README.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from datetime import datetime, timezone

MAX_FILE = 0xFFFFFFFF - 32 * 1024 * 1024
MAX_PACKET = 64 * 1024 * 1024
MAX_HEADER = 4 * 1024 * 1024
MAX_RECORDS = 2_000_000
CHUNK = re.compile(rb"[0-9]{2}(?:dc|db)|\xff\xd8\xff")
NMEA = re.compile(rb"\$[A-Z]{2}RMC,[\x20-\x7e]{10,180}?\*[0-9A-Fa-f]{2}")
TIMING_WARNING = ("회수된 프레임을 빈 구간 없이 이어 붙인 검토용 영상입니다. 원래 영상 시각·길이, "
                  "GPS 동기화, 급가감속 판정에 사용하지 마세요. GPS CSV는 파일 전체에서 별도로 "
                  "회수했으며 슬랙/과거 녹화가 포함될 수 있습니다.")


class RecoveryCancelled(Exception):
    pass


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise RecoveryCancelled("복원이 취소되었습니다.")


def _hash(path, cancel=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while block := f.read(1024 * 1024):
            _check(cancel)
            h.update(block)
    return h.hexdigest()


def _u32(buf, p):
    return struct.unpack_from('<I', buf, p)[0]


def _chunk(tag, payload):
    return tag + struct.pack('<I', len(payload)) + payload + (b'\0' if len(payload) & 1 else b'')


def _children(buf, begin, end):
    while begin + 8 <= end:
        size = _u32(buf, begin + 4)
        stop = begin + 8 + size
        if stop > end:
            return
        yield bytes(buf[begin:begin + 4]), begin + 8, stop
        begin = stop + (size & 1)


def _headers(buf):
    """Find a validated hdrl independently of damaged RIFF/movi sizes/signatures."""
    limit = min(len(buf), MAX_HEADER)
    pos = 0
    while True:
        pos = buf.find(b'hdrl', pos, limit)
        if pos < 0:
            return []
        if pos >= 8 and buf[pos-8:pos-4] == b'LIST':
            size = _u32(buf, pos-4)
            end = pos + size
            if 4 <= size <= MAX_HEADER and end <= len(buf):
                streams = []
                for tag, a, b in _children(buf, pos+4, end):
                    if tag != b'LIST' or buf[a:a+4] != b'strl':
                        continue
                    h = fmt = None
                    for sub, x, y in _children(buf, a+4, b):
                        if sub == b'strh' and y-x >= 56:
                            h = bytes(buf[x:x+56])
                        if sub == b'strf' and y-x <= MAX_HEADER:
                            fmt = bytes(buf[x:y])
                    streams.append((h, fmt))
                if streams and len(streams) <= 100:
                    return streams
        pos += 4


def _video_spec(header, fmt):
    if not header or not fmt or header[:4] != b'vids' or len(fmt) < 40:
        return None
    size, w, h = struct.unpack_from('<Iii', fmt)
    scale, rate = struct.unpack_from('<II', header, 20)
    codec = fmt[16:20].upper()
    if not (40 <= size <= len(fmt) and 0 < w <= 16384 and 0 < abs(h) <= 16384
            and w*abs(h) <= 33_554_432 and scale and rate and .1 <= rate/scale <= 1000):
        return None
    if codec not in (b'MJPG', b'JPEG', b'H264', b'X264', b'AVC1'):
        return None
    return {'header': header, 'format': fmt, 'width': w, 'height': abs(h),
            'scale': scale, 'rate': rate, 'codec': codec.decode('ascii')}


def _jpeg_size(data):
    """Read JPEG SOF without allocating pixels. A complete JPEG also needs EOI."""
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        return None
    p = 2
    while p + 4 <= len(data):
        if data[p] != 255:
            return None
        while p < len(data) and data[p] == 255:
            p += 1
        if p >= len(data):
            return None
        marker = data[p]
        p += 1
        if marker in (0xD8, 0xD9, 0xDA):
            return None
        if marker == 1 or 0xD0 <= marker <= 0xD7:
            continue
        if p+2 > len(data):
            return None
        size = int.from_bytes(data[p:p+2], 'big')
        if size < 2 or p+size > len(data):
            return None
        if marker in (0xC0, 0xC1, 0xC2) and size >= 8:
            h = int.from_bytes(data[p+3:p+5], 'big')
            w = int.from_bytes(data[p+5:p+7], 'big')
            return (w, h) if 0 < w <= 16384 and 0 < h <= 16384 and w*h <= 33_554_432 else None
        p += size
    return None


def _nal_types(data):
    return {data[m.end()] & 31 for m in re.finditer(b'\x00\x00\x01', data)
            if m.end() < len(data) and not data[m.end()] & 128}


def _index_boundary(buf, cancel=None, limit=None, *, details=False, media_limit=None,
                    excluded_indexes=()):
    """Find idx1 backed by several surviving chunk headers, not a bare signature.

    Offsets may be absolute or relative to the lost movi marker. Infer one
    common base and require agreement across dispersed index entries.
    """
    limit = len(buf) if limit is None else limit
    media_limit = limit if media_limit is None else media_limit
    pos = 0
    candidates = 0
    while True:
        _check(cancel)
        pos = buf.find(b'idx1', pos, limit)
        if pos < 0 or pos + 8 > len(buf):
            return None
        index = pos
        pos += 4
        candidates += 1
        if candidates > 256:
            return None
        if any(a <= index < b for a, b in excluded_indexes):
            continue
        size = _u32(buf, index + 4)
        if size < 48 or size % 16 or size // 16 > MAX_RECORDS or index + 8 + size > limit:
            continue
        count = size // 16
        entries = [struct.unpack_from('<4sIII', buf, index + 8 + n*16)
                   for n in sorted({i*(count-1)//min(count-1, 15) for i in range(min(count, 16))})]
        if any(not re.fullmatch(rb'[0-9]{2}(?:dc|db|wb|st|tx|pc)', tag)
               or not 0 < length <= MAX_PACKET for tag, _, off, length in entries):
            continue
        bases = {0}
        # Find a surviving anchor near the start; later probes test the same base.
        # Dispersed probes can all lie beyond MAX_HEADER in long recordings.
        # Use early index entries as anchors, including survivors after 8KB loss.
        anchors = [struct.unpack_from('<4sIII', buf, index+8+n*16)
                   for n in range(min(count, 32))]
        for tag, _, off, length in anchors:
            if not re.fullmatch(rb'[0-9]{2}(?:dc|db|wb|st|tx|pc)', tag):
                continue
            p = 0
            for _ in range(128):
                p = buf.find(tag, p, min(index, MAX_HEADER))
                if p < 0:
                    break
                if p+8 <= index and _u32(buf, p+4) == length and p >= off:
                    bases.add(p-off)
                p += 1
        for base in sorted(bases):
            hits = 0
            for tag, _, off, length in entries:
                p = base + off
                if p+8+length <= index and buf[p:p+4] == tag and _u32(buf, p+4) == length:
                    hits += 1
            if hits >= max(3, (len(entries)+1)//2):
                # A preallocated movi can contain old recordings BEFORE idx1.
                # Bound by all index references, not by idx1's physical address.
                media_end = 0
                for n in range(count):
                    if n % 4096 == 0:
                        _check(cancel)
                    tag, _, off, length = struct.unpack_from('<4sIII', buf, index+8+n*16)
                    stop = base+off+8+length
                    if (not re.fullmatch(rb'[0-9]{2}(?:dc|db|wb|st|tx|pc)', tag)
                            or not 0 < length <= MAX_PACKET
                            or stop > min(index, media_limit)):
                        break
                    if length & 1 and stop < min(index, media_limit) and buf[stop:stop+1] == b'\0':
                        stop += 1
                    media_end = max(media_end, stop)
                else:
                    return (index, media_end) if details else index
    return None


def _media_bounds(buf, cancel=None):
    """Bound media independently of reference headers; never widen into slack."""
    # A second complete AVI is a separate recording, never boundary evidence
    # for the damaged first recording. This also prevents borrowing its idx1.
    limit = len(buf)
    excluded_indexes = []
    for other in re.finditer(rb'RIFF....AVI LIST....hdrl', buf, re.DOTALL):
        if other.start() > 0:
            limit = min(limit, other.start())
            other_end = other.start()+8+_u32(buf, other.start()+4)
            if other.start()+24 <= other_end <= len(buf):
                excluded_indexes.append((other.start(), other_end))
    # An embedded stale header must not hide the current recording's index.
    # Accept only an index whose references all precede that stale header.
    validated = _index_boundary(buf, cancel, details=True, media_limit=limit,
                                excluded_indexes=excluded_indexes)
    index, indexed_end = validated if validated else (None, None)
    for match in re.finditer(b'LIST....movi', buf[:min(limit, MAX_HEADER)], re.DOTALL):
        _check(cancel)
        p = match.start()
        size = _u32(buf, p+4)
        if size < 4:
            continue
        end = p+8+size
        if index is not None and index >= p+12:
            return p+12, indexed_end, 'movi' if end == indexed_end else 'idx1_validated'
        if limit < len(buf) and end > limit:
            # With idx1 lost and conflicting embedded AVI headers, retain only
            # the contiguous chunk prefix. Never resync into the old recording.
            cursor, count = p+12, 0
            while cursor+8 <= limit:
                _check(cancel)
                tag = bytes(buf[cursor:cursor+4])
                length = _u32(buf, cursor+4)
                if (not re.fullmatch(rb'[0-9]{2}(?:dc|db|wb|st|tx|pc)|JUNK', tag)
                        or length > MAX_PACKET or cursor+8+length > limit):
                    break
                cursor += 8+length
                count += 1
                if length & 1 and buf[cursor:cursor+1] == b'\0':
                    cursor += 1
            if count >= 3:
                return p+12, cursor, 'contiguous_before_embedded_avi'
            break
        if end <= limit:
            following = bytes(buf[end:end+4])
            # A surviving size remains a boundary even if idx1 itself is erased.
            if end == limit or following in (b'idx1', b'JUNK', b'RIFF', b'LIST', b'\0'*4):
                return p+12, end, 'movi'
        elif (limit == len(buf) and end <= MAX_FILE and buf[:4] == b'RIFF'
              and buf[8:12] == b'AVI ' and _u32(buf, 4)+8 >= end):
            return p+12, len(buf), 'truncated_movi'
        break
    if index is not None:
        return 0, indexed_end, 'idx1_validated'
    return 0, len(buf), 'whole_file_untrusted'


def _packets(buf, specs, cancel, scope):
    start, end, _ = scope
    pos = start
    count = 0
    while pos + 8 <= end:
        _check(cancel)
        match = CHUNK.search(buf, pos, min(end, pos+1024*1024))
        if not match:
            pos += 1024*1024-3
            continue
        p = match.start()
        # A damaged chunk ID/length must not hide an otherwise complete JPEG.
        # Map only when exactly one declared MJPEG stream matches its dimensions.
        if buf[p:p+3] == b'\xff\xd8\xff':
            stop = buf.find(b'\xff\xd9', p+3, min(end, p+MAX_PACKET))
            if stop < 0:
                pos = p+3
                continue
            data = bytes(buf[p:stop+2])
            dimensions = _jpeg_size(data)
            matching = [i for i, spec in specs.items() if spec['codec'] in ('MJPG', 'JPEG')
                        and dimensions == (spec['width'], spec['height'])]
            if len(matching) != 1:
                pos = p+3
                continue
            sid, offset, next_pos = matching[0], p, stop+2
        else:
            sid = int(buf[p:p+2])
            size = _u32(buf, p+4) if p+8 <= end else 0
            spec = specs.get(sid)
            if not spec or not (0 < size <= MAX_PACKET) or p+8+size > end:
                pos = p+1
                continue
            data = bytes(buf[p+8:p+8+size])
            jpeg = spec['codec'] in ('MJPG', 'JPEG')
            valid = (_jpeg_size(data) == (spec['width'], spec['height'])) if jpeg else bool(_nal_types(data) & {1, 5, 7, 8})
            if not valid:
                pos = p+1
                continue
            offset, next_pos = p+8, p+8+size
            # Some dashcams omit RIFF word padding even between video chunks.
            # Do not skip the first byte of the following chunk's identifier.
            if size & 1 and next_pos < end and buf[next_pos:next_pos+1] == b'\0':
                next_pos += 1
        count += 1
        if count > MAX_RECORDS:
            raise ValueError('복원 청크 수가 안전 한도를 초과했습니다.')
        yield sid, offset, data
        pos = next_pos


def _intact_gap(buf, start, end, sid):
    """Recognize interleaved chunks without mistaking silent audio for damage.

    A missing/rejected video chunk or unparseable gap breaks H.264 prediction.
    Accept the unpadded chunk layout used by some dashcams as well as RIFF pads.
    """
    while start < end:
        if buf[start:start+1] == b'\0':
            start += 1
            if start == end:
                return True
        if start+8 > end:
            return False
        tag = bytes(buf[start:start+4])
        size = _u32(buf, start+4)
        if not (tag == b'JUNK' or re.fullmatch(rb'[0-9]{2}(?:dc|db|wb|st|tx|pc)', tag)):
            return False
        if tag in (f'{sid:02d}dc'.encode(), f'{sid:02d}db'.encode()):
            return False
        if size > MAX_PACKET or start+8+size > end:
            return False
        start += 8+size
    return True


class _AVIWriter:
    """One video stream only: no synthetic GPS/audio interleaving or old indices."""
    def __init__(self, path, spec):
        self.spec, self.path = spec, path
        self.count = self.maximum = 0
        self.index = tempfile.TemporaryFile()
        self.f = open(path, 'xb')
        self.f.write(self._prefix())
        self.movi = self.f.tell()
        self.f.write(b'LIST\0\0\0\0movi')
        self.sps = self.pps = False
        self.started = spec['codec'] in ('MJPG', 'JPEG')
        self.config = []
        self.damaged_packets = self.dependent_packets = self.discontinuities = 0

    def break_prediction(self):
        self.started = False
        self.config.clear()
        self.discontinuities += 1

    def _prefix(self):
        s = self.spec
        avih = struct.pack('<14I', round(1_000_000*s['scale']/s['rate']), 0, 0, 0x10,
                           self.count, 0, 1, self.maximum, s['width'], s['height'], 0, 0, 0, 0)
        h = bytearray(s['header'])
        struct.pack_into('<I', h, 8, 0)  # discard old stream flags
        struct.pack_into('<III', h, 28, 0, self.count, self.maximum)
        struct.pack_into('<I', h, 44, 0)  # video samples are variable-size
        hdrl = _chunk(b'LIST', b'hdrl'+_chunk(b'avih', avih)+
                      _chunk(b'LIST', b'strl'+_chunk(b'strh', h)+_chunk(b'strf', s['format'])))
        return b'RIFF\0\0\0\0AVI ' + hdrl

    def add(self, data):
        if self.spec['codec'] not in ('MJPG', 'JPEG'):
            # Treat long zero runs as suspected overwrites, conservatively
            # excluding padded packets too. Resume only at the next IDR.
            if b'\0'*32 in data:
                self.damaged_packets += 1
                self.break_prediction()
                return False
            types = _nal_types(data)
            self.sps |= 7 in types
            self.pps |= 8 in types
            if not self.started:
                if (7 in types or 8 in types) and not types & {1, 5}:
                    if len(self.config) < 16:
                        self.config.append(data)
                    return False
                if not (5 in types and self.sps and self.pps):
                    self.dependent_packets += 1
                    return False
                data = b''.join(self.config) + data
                self.config.clear()
                self.started = True
            key = 0x10 if 5 in types else 0
        else:
            key = 0x10
        offset = self.f.tell() - (self.movi+8)
        self.index.write(struct.pack('<4sIII', b'00dc', key, offset, len(data)))
        self.f.write(_chunk(b'00dc', data))
        self.count += 1
        self.maximum = max(self.maximum, len(data))
        return True

    def finish(self):
        end = self.f.tell()
        self.f.write(b'idx1'+struct.pack('<I', self.count*16))
        self.index.seek(0)
        shutil.copyfileobj(self.index, self.f)
        total = self.f.tell()
        if total > 0xFFFFFFFF:
            raise ValueError('복원본이 AVI 1.0 크기 한도를 초과했습니다.')
        self.f.seek(0)
        self.f.write(self._prefix())
        self.f.seek(4)
        self.f.write(struct.pack('<I', total-8))
        self.f.seek(self.movi+4)
        self.f.write(struct.pack('<I', end-self.movi-8))
        self.close()
        if not self.count:
            self.path.unlink()

    def close(self):
        self.index.close()
        self.f.close()


def _gps(buf, path, cancel):
    # Reuse vetted coordinate parsing; require checksum/date/time/fix validity.
    from core.paths import ensure_vendor_importable
    ensure_vendor_importable()
    from integration_avi import nmea_checksum_ok, parse_rmc
    count = 0
    with open(path, 'x', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['source_offset', 'gps_date_utc', 'gps_time_utc', 'latitude',
                         'longitude', 'speed_kmh', 'scope', 'raw_nmea'])
        for m in NMEA.finditer(buf):
            _check(cancel)
            raw = m.group().decode('ascii')
            if not nmea_checksum_ok(raw):
                continue
            fields = raw[1:].split('*')[0].split(',')
            rec = parse_rmc(fields)
            if not rec or not rec['status_valid'] or rec['mode'] == 'N' or rec['parse_warnings']:
                continue
            if rec['lat'] == 0 and rec['lon'] == 0:
                continue
            try:
                datetime.fromisoformat(rec['date']+'T'+rec['utc_time'])
            except ValueError:
                continue
            writer.writerow([m.start(), rec['date'], rec['utc_time'], rec['lat'], rec['lon'],
                             rec['speed_kmh'], 'whole_file_untrusted', raw])
            count += 1
            if count >= MAX_RECORDS:
                raise ValueError('GPS 기록 수가 안전 한도를 초과했습니다.')
    return count


def _decode_check(path, cancel):
    exe = shutil.which('ffmpeg')
    if not exe:
        return {'status': 'not_run', 'reason': 'FFmpeg 없음: 재생 가능 여부는 미검증'}
    # Full decode, bounded stderr on disk, cancellable. No network protocols, no shell.
    import time
    with tempfile.TemporaryFile() as log, tempfile.TemporaryFile() as progress:
        proc = subprocess.Popen([exe, '-nostdin', '-v', 'error', '-xerror',
            '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:v:0',
            '-progress', 'pipe:1', '-f', 'null', '-'], stdout=progress, stderr=log)
        start = time.monotonic()
        try:
            while proc.poll() is None:
                _check(cancel)
                if time.monotonic()-start > 120:
                    return {'status': 'timeout'}
                time.sleep(.05)
            progress.seek(0)
            frames = re.findall(rb'frame=(\d+)', progress.read())
            log.seek(0)
            error = log.read(4096).decode('utf-8', 'replace')
            decoded = int(frames[-1]) if frames else 0
            # Some FFmpeg builds return zero even after decoder-thread errors.
            # At loglevel=error any stderr means full decoding was not clean.
            return {'status': 'passed' if proc.returncode == 0 and decoded > 0 and not error.strip() else 'failed',
                    'decoded_frames': decoded, 'error': error}
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def recover_avi(source, output_dir, reference=None, cancel=None, progress=None):
    """Create a NEW directory atomically; never overwrite evidence or a prior result.

    Reference supplies headers only; no frames/GPS are copied from it. If no usable
    header survives, export JPEG stills and GPS instead of inventing frame rate.
    """
    source = Path(source).resolve()
    output = Path(output_dir).resolve()
    reference = Path(reference).resolve() if reference else None
    if output.exists():
        raise FileExistsError('출력 폴더가 이미 있습니다. 새 폴더를 선택하세요.')
    size = source.stat().st_size
    if not 12 <= size <= MAX_FILE:
        raise ValueError('지원 크기: 12바이트 이상, 약 3.9GiB 이하의 AVI입니다.')
    if source.suffix.lower() != '.avi':
        raise ValueError('이 복원기는 AVI 전용입니다. MP4 복원은 지원하지 않습니다.')
    if reference == source:
        raise ValueError('정상 참조 영상은 손상 원본과 달라야 합니다.')
    _check(cancel)
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='.avi-recovery-', dir=output.parent))
    writers = {}
    report = progress or (lambda _: None)
    try:
        report('원본 SHA-256 및 복원 사본 확인 중...')
        original_hash = _hash(source, cancel)
        copied = work/'damaged_source.avi'
        with source.open('rb') as src, copied.open('xb') as dst:
            while block := src.read(1024*1024):
                _check(cancel)
                dst.write(block)
        if copied.stat().st_size != size or _hash(copied, cancel) != original_hash:
            raise ValueError('원본과 복원용 사본의 SHA-256이 다릅니다.')
        manifest = {'schema': 1, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
                    'source_path': str(source), 'source_sha256': original_hash,
                    'source_size': size, 'source_copy': copied.name,
                    'copy_sha256_verified': True, 'original_timeline_preserved': False,
                    'warning': TIMING_WARNING, 'reference': None, 'videos': []}
        with copied.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            streams = _headers(buf)
            if reference:
                if reference.stat().st_size > MAX_FILE:
                    raise ValueError('참조 영상 크기가 지원 한도를 초과했습니다.')
                ref_hash = _hash(reference, cancel)
                with reference.open('rb') as rf:
                    ref = rf.read(MAX_HEADER)
                if not ref.startswith(b'RIFF') or ref[8:12] != b'AVI ':
                    raise ValueError('정상 AVI 참조 영상이 아닙니다.')
                streams = _headers(ref)
                manifest['reference'] = {'path': str(reference), 'sha256': ref_hash,
                                         'usage': 'headers_only_user_selected'}
            specs = {i: s for i, (h, fmt) in enumerate(streams) if (s := _video_spec(h, fmt))}
            scope = _media_bounds(buf, cancel)
            bounded = scope[2] != 'whole_file_untrusted'
            manifest['video_boundary_verified'] = bounded
            manifest['video_withheld_reason'] = (None if bounded else
                '영상 경계를 확인할 수 없어 슬랙 혼입 방지를 위해 AVI 생성을 보류했습니다.')
            if not bounded:
                specs = {}
            manifest['video_scan_range'] = {'start': scope[0], 'end': scope[1], 'basis': scope[2]}
            report('손상 구간 뒤의 영상 청크와 GPS를 탐색 중...')
            for sid, spec in specs.items():
                writers[sid] = _AVIWriter(work/f'recovered_stream{sid:02d}.avi', spec)
            with (work/'frame_offsets.csv').open('x', newline='', encoding='utf-8-sig') as log:
                rows = csv.writer(log)
                rows.writerow(['stream', 'recovered_frame_index', 'source_payload_offset', 'source_payload_size'])
                previous_ends = {}
                for sid, offset, data in _packets(buf, specs, cancel, scope):
                    writer = writers[sid]
                    if (writer.spec['codec'] not in ('MJPG', 'JPEG') and sid in previous_ends
                            and not _intact_gap(buf, previous_ends[sid], offset-8, sid)):
                        writer.break_prediction()
                    previous_ends[sid] = offset+len(data)
                    if writer.add(data):
                        rows.writerow([sid, writer.count-1, offset, len(data)])
            for writer in writers.values():
                writer.finish()
            manifest['gps_records'] = _gps(buf, work/'recovered_gps.csv', cancel)
            # Header-free MJPEG salvage: keep stills, never guess frame rate/camera identity.
            manifest['jpeg_stills'] = 0
            manifest['jpeg_scope'] = scope[2]
            if not any(w.count for w in writers.values()):
                pos, end = scope[0], scope[1]
                stills = work/'jpeg_stills'
                with (work/'jpeg_offsets.csv').open('x', newline='', encoding='utf-8-sig') as log:
                    rows = csv.writer(log)
                    rows.writerow(['file', 'source_offset', 'size', 'width', 'height'])
                    while pos < end:
                        _check(cancel)
                        a = buf.find(b'\xff\xd8\xff', pos, min(end, pos+1024*1024))
                        if a < 0:
                            pos += 1024*1024-2
                            continue
                        b = buf.find(b'\xff\xd9', a+3, min(end, a+MAX_PACKET))
                        if b < 0:
                            pos = a+3
                            continue
                        data = bytes(buf[a:b+2])
                        dimensions = _jpeg_size(data)
                        if dimensions:
                            n = manifest['jpeg_stills']
                            if n >= 100_000:
                                raise ValueError('JPEG 회수 수가 안전 한도를 초과했습니다.')
                            stills.mkdir(exist_ok=True)
                            name = f'frame{n:06d}.jpg'
                            (stills/name).write_bytes(data)
                            rows.writerow([name, a, len(data), *dimensions])
                            manifest['jpeg_stills'] += 1
                            pos = b+2
                        else:
                            pos = a+3
        for sid, writer in writers.items():
            if not writer.count:
                continue
            report(f'{sid}번 스트림 복원본 전체 디코딩 검증 중...')
            check = _decode_check(writer.path, cancel)
            if check['status'] == 'passed' and check['decoded_frames'] != writer.count:
                check['status'] = 'incomplete'
                check['reason'] = '회수 후보 수와 실제 디코딩 프레임 수가 다릅니다.'
            manifest['videos'].append({'file': writer.path.name, 'stream': sid,
                'candidate_frames': writer.count, 'sha256': _hash(writer.path, cancel),
                'h264_exclusions': {'zero_filled_packets': writer.damaged_packets,
                    'waiting_for_idr_packets': writer.dependent_packets,
                    'prediction_breaks': writer.discontinuities},
                'codec': writer.spec['codec'], 'decode_check': check})
        manifest['status'] = ('video_recovered' if any(v['decode_check']['status'] == 'passed' for v in manifest['videos'])
            else 'video_candidates_unverified' if manifest['videos'] else 'metadata_or_stills_only'
            if manifest['gps_records'] or manifest['jpeg_stills'] else 'nothing_recovered')
        # Final read catches source changes during recovery; no false verified evidence.
        if _hash(source, cancel) != original_hash:
            raise ValueError('복원 도중 원본이 변경됐습니다. 결과를 폐기합니다.')
        if reference and _hash(reference, cancel) != manifest['reference']['sha256']:
            raise ValueError('복원 도중 참조 영상이 변경됐습니다.')
        (work/'recovery.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        (work/'READ_ME.txt').write_text(TIMING_WARNING+'\n\n상태: '+manifest['status']+
            '\n'+(manifest['video_withheld_reason'] or '')+
            '\nJPEG 경계 근거: '+manifest['jpeg_scope']+
            '\n영상·GPS·JPEG의 원본 위치는 *_offsets.csv 및 recovered_gps.csv에 기록됩니다.\n'
            'FFmpeg 검증이 passed인 영상만 실제 디코딩 성공이 확인된 것입니다.\n'
            '참조 영상은 같은 기기·해상도·코덱·FPS·채널 순서여야 합니다.\n', encoding='utf-8')
        _check(cancel)
        if output.exists():
            raise FileExistsError("출력 폴더가 이미 있습니다.")
        os.rename(work, output)
        return dict(manifest, output_dir=str(output))
    except BaseException:
        for writer in writers.values():
            writer.close()
        shutil.rmtree(work, ignore_errors=True)
        raise


def main():
    # Redirected Windows consoles may default to cp1252, which cannot print Korean.
    import sys
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    p = argparse.ArgumentParser(description='손상 AVI에서 영상/GPS 회수 (원본 보존)')
    p.add_argument('source')
    p.add_argument('output', help='존재하지 않는 새 결과 폴더')
    p.add_argument('--reference', help='동일 기기/설정의 정상 AVI (헤더 소실 시)')
    args = p.parse_args()
    result = recover_avi(args.source, args.output, args.reference, progress=print)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
