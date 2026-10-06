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


def _media_bounds(buf):
    """Honor a surviving movi boundary so normal trailing slack is not promoted."""
    for match in re.finditer(b'LIST....movi', buf, re.DOTALL):
        p = match.start()
        size = _u32(buf, p+4)
        if size >= 4:
            end = p+8+size
            if end <= len(buf):
                following = bytes(buf[end:end+4])
                if end == len(buf) or following in (b'idx1', b'JUNK', b'RIFF', b'LIST'):
                    return p+12, end, 'movi'
                return p+12, len(buf), 'invalid_movi_size_untrusted'
            return p+12, len(buf), 'truncated_movi'
    # Signature/size lost. This scope is explicitly untrusted, never a current-track source.
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
            offset, next_pos = p+8, p+8+size+(size & 1)
        count += 1
        if count > MAX_RECORDS:
            raise ValueError('복원 청크 수가 안전 한도를 초과했습니다.')
        yield sid, offset, data
        pos = next_pos


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
            types = _nal_types(data)
            self.sps |= 7 in types
            self.pps |= 8 in types
            if not self.started:
                if (7 in types or 8 in types) and not types & {1, 5}:
                    if len(self.config) < 16:
                        self.config.append(data)
                    return False
                if not (5 in types and self.sps and self.pps):
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
            scope = _media_bounds(buf)
            manifest['video_scan_range'] = {'start': scope[0], 'end': scope[1], 'basis': scope[2]}
            report('손상 구간 뒤의 영상 청크와 GPS를 탐색 중...')
            for sid, spec in specs.items():
                writers[sid] = _AVIWriter(work/f'recovered_stream{sid:02d}.avi', spec)
            with (work/'frame_offsets.csv').open('x', newline='', encoding='utf-8-sig') as log:
                rows = csv.writer(log)
                rows.writerow(['stream', 'recovered_frame_index', 'source_payload_offset', 'source_payload_size'])
                for sid, offset, data in _packets(buf, specs, cancel, scope):
                    writer = writers[sid]
                    if writer.add(data):
                        rows.writerow([sid, writer.count-1, offset, len(data)])
            for writer in writers.values():
                writer.finish()
            manifest['gps_records'] = _gps(buf, work/'recovered_gps.csv', cancel)
            # Header-free MJPEG salvage: keep stills, never guess frame rate/camera identity.
            manifest['jpeg_stills'] = 0
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
    p = argparse.ArgumentParser(description='손상 AVI에서 영상/GPS 회수 (원본 보존)')
    p.add_argument('source')
    p.add_argument('output', help='존재하지 않는 새 결과 폴더')
    p.add_argument('--reference', help='동일 기기/설정의 정상 AVI (헤더 소실 시)')
    args = p.parse_args()
    result = recover_avi(args.source, args.output, args.reference, progress=print)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
