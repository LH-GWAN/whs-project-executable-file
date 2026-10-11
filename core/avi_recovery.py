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
# 미디어 청크 ID(영상 dc/db, 오디오 wb, 텍스트/자막/GPS st/tx/pc). 스펙이 없는 스트림의
# 청크도 통째로 건너뛰어야 하므로(#86) 탐색 정규식에 모두 포함한다.
MEDIA_TAG = re.compile(rb"[0-9]{2}(?:dc|db|wb|st|tx|pc)")
CHUNK = re.compile(rb"[0-9]{2}(?:dc|db|wb|st|tx|pc)|\xff\xd8\xff")
NONZERO = re.compile(rb"[^\0]")
# 이 길이 이상의 0 구간은 섹터 정렬 패딩이 아니라 덮어쓰기·소실로 본다(H.264 패킷 검사와 동일 기준).
ZERO_RUN = 32
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
    """JPEG 헤더의 SOF 크기를 읽되, 엔트로피 구간까지 구조를 끝까지 검사한다.

    청크 경계와 어긋난 덮어쓰기는 앞 프레임의 머리와 뒤 프레임의 꼬리를 한 JPEG처럼 보이게
    만든다(#31). SOF만 보면 이런 합성 프레임이 통과하므로, 스캔 데이터 안에서 두 번째 SOI,
    긴 0 구간, AVI 청크 헤더, 허용되지 않는 마커가 나오면 거부한다. 픽셀은 할당하지 않는다.
    """
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        return None
    n = len(data)
    p = 2
    dims = None
    scanning = scanned = False
    while True:
        if scanning:
            # 엔트로피 구간: FF 뒤에는 00(스터핑)·FF(채움)·RSTn만 올 수 있고, 그 외는 마커다.
            q = p
            while True:
                q = data.find(b'\xff', q)
                if q < 0 or q + 1 >= n:
                    return None
                after = data[q + 1]
                if after == 0 or 0xD0 <= after <= 0xD7:
                    q += 2
                    continue
                if after == 0xFF:
                    q += 1
                    continue
                break
            segment = data[p:q]
            if b'\0' * ZERO_RUN in segment or MEDIA_TAG.search(segment):
                return None
            p = q
            scanning = False
        if p >= n or data[p] != 0xFF:
            return None
        while p < n and data[p] == 0xFF:
            p += 1
        if p >= n:
            return None
        marker = data[p]
        p += 1
        if marker == 0xD9:
            return dims if p == n and scanned else None
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            return None
        if p + 2 > n:
            return None
        size = int.from_bytes(data[p:p + 2], 'big')
        if size < 2 or p + size > n:
            return None
        if marker in (0xC0, 0xC1, 0xC2):
            if dims is not None or size < 8:
                return None
            h = int.from_bytes(data[p + 3:p + 5], 'big')
            w = int.from_bytes(data[p + 5:p + 7], 'big')
            if not (0 < w <= 16384 and 0 < h <= 16384 and w * h <= 33_554_432):
                return None
            dims = (w, h)
        if marker == 0xDA:
            if dims is None:
                return None
            scanning = scanned = True
        p += size
    return None


def _nal_types(data):
    return {data[m.end()] & 31 for m in re.finditer(b'\x00\x00\x01', data)
            if m.end() < len(data) and not data[m.end()] & 128}


SKIPPABLE_TAG = re.compile(rb'rec |JUNK|ix[0-9]{2}')


def _entry(buf, index, n, count):
    """n번째부터 최대 64개 안에서 첫 미디어 엔트리(길이>0)를 돌려준다.

    rec ·JUNK·ix## 엔트리와 길이 0 엔트리는 정상 구조라 건너뛴다(#87). 그 안에서 미디어 엔트리를
    못 찾으면 None, 알 수 없는 태그나 불가능한 길이를 만나면 False(손상·쓰레기 idx1).
    """
    for m in range(n, min(count, n + 64)):
        tag, flags, off, length = struct.unpack_from('<4sIII', buf, index + 8 + m * 16)
        if MEDIA_TAG.fullmatch(tag) and 0 < length <= MAX_PACKET:
            return tag, flags, off, length
        if not (SKIPPABLE_TAG.fullmatch(tag) or (MEDIA_TAG.fullmatch(tag) and length == 0)):
            return False
    return None


def _index_boundary(buf, cancel=None, limit=None, *, details=False, media_limit=None,
                    excluded_indexes=()):
    """Find idx1 backed by several surviving chunk headers, not a bare signature.

    Offsets may be absolute or relative to the lost movi marker. Infer one
    common base and require agreement across dispersed index entries.

    사전 할당 파일에는 현재 녹화 뒤에 옛 녹화의 idx1이 남을 수 있다(#30). 옛 idx1은 뒤쪽
    엔트리(옛 청크가 남은 구간)만 맞고 앞쪽 엔트리(현재 녹화가 덮어쓴 구간)는 맞지 않으므로,
    분산 표본 과반뿐 아니라 **초기 미디어 엔트리가 연속으로 제자리에 맞을 것**도 요구한다.
    길이 0 엔트리(드롭 프레임 표식)·rec ·JUNK 엔트리는 정상 구조라 검증에서 건너뛴다(#87).
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
        samples = [_entry(buf, index, n, count)
                   for n in sorted({i*(count-1)//min(count-1, 15) for i in range(min(count, 16))})]
        entries = [e for e in samples if e]
        if False in samples or len(entries) < min(3, count):
            continue
        # 초기 영상 엔트리: 앞 128개 안의 영상(dc/db) 엔트리 최대 32개. 기준점(base) 추정과
        # 연속 검증에 쓴다. 오디오·텍스트 청크의 크기 손상이 현재 idx1을 가리면 안 되므로 영상만 본다.
        anchors = []
        for n in range(min(count, 128)):
            tag, flags, off, length = struct.unpack_from('<4sIII', buf, index + 8 + n * 16)
            if re.fullmatch(rb'[0-9]{2}(?:dc|db)', tag) and 0 < length <= MAX_PACKET:
                anchors.append((tag, flags, off, length))
                if len(anchors) >= 32:
                    break
        if not anchors:
            continue
        bases = {0}
        # Find a surviving anchor near the start; later probes test the same base.
        # Dispersed probes can all lie beyond MAX_HEADER in long recordings.
        # Use early index entries as anchors, including survivors after 8KB loss.
        for tag, _, off, length in anchors:
            p = 0
            for _ in range(128):
                p = buf.find(tag, p, min(index, MAX_HEADER))
                if p < 0:
                    break
                if p+8 <= index and _u32(buf, p+4) == length and p >= off:
                    bases.add(p-off)
                p += 1

        def in_place(base, entry):
            tag, _, off, length = entry
            p = base + off
            return p+8+length <= index and buf[p:p+4] == tag and _u32(buf, p+4) == length

        for base in sorted(bases):
            hits = sum(in_place(base, entry) for entry in entries)
            if hits < max(3, (len(entries)+1)//2):
                continue
            # 초기 영상 엔트리가 연속으로 제자리에 맞아야 한다. 앞머리 소실·중간 손상은 허용:
            # 32개 중 8개 연속이거나, 3/4 이상이 맞으면서 4개 연속. 옛 녹화의 idx1은 앞부분이
            # 현재 녹화로 덮여 있어 초기 엔트리가 우연으로만 맞는다.
            run = longest = matched = 0
            for entry in anchors:
                hit = in_place(base, entry)
                matched += hit
                run = run + 1 if hit else 0
                longest = max(longest, run)
            if not (longest >= min(8, len(anchors))
                    or (matched*4 >= len(anchors)*3 and longest >= min(4, len(anchors)))):
                continue
            # A preallocated movi can contain old recordings BEFORE idx1.
            # Bound by all index references, not by idx1's physical address.
            media_end = 0
            for n in range(count):
                if n % 4096 == 0:
                    _check(cancel)
                tag, _, off, length = struct.unpack_from('<4sIII', buf, index+8+n*16)
                if SKIPPABLE_TAG.fullmatch(tag) or (MEDIA_TAG.fullmatch(tag) and length == 0):
                    continue
                stop = base+off+8+length
                if (not MEDIA_TAG.fullmatch(tag) or length > MAX_PACKET
                        or stop > min(index, media_limit)):
                    break
                if length & 1 and stop < min(index, media_limit) and buf[stop:stop+1] == b'\0':
                    stop += 1
                media_end = max(media_end, stop)
            else:
                if media_end:
                    return (index, media_end) if details else index
    return None


def _structures(buf, start, end):
    """start부터 정상 AVI 구조를 차례로 따라가며 (tag, pos, size, next)를 낸다.

    정상 구조(#87 #88): 미디어 청크(길이 0 드롭 프레임 표식 포함), JUNK, LIST rec (안으로
    들어간다), ix## 색인 청크, ZERO_RUN 미만의 0 패딩(RIFF 홀수 패딩·섹터 정렬). 알 수 없는
    바이트나 긴 0 구간을 만나면 멈춘다. end에 정확히 닿으면 마지막에 (None, end, 0, end)를 낸다.
    """
    pos = start
    while True:
        nonzero = NONZERO.search(buf, pos, end)
        p = nonzero.start() if nonzero else end
        if p - pos >= ZERO_RUN:
            return
        pos = p
        if pos >= end:
            yield None, end, 0, end
            return
        if pos + 8 > end:
            return
        tag = bytes(buf[pos:pos+4])
        size = _u32(buf, pos+4)
        if tag == b'LIST':
            if buf[pos+8:pos+12] != b'rec ' or size < 4:
                return
            yield tag, pos, size, pos+12
            pos += 12
            continue
        if not (tag == b'JUNK' or MEDIA_TAG.fullmatch(tag) or re.fullmatch(rb'ix[0-9]{2}', tag)):
            return
        if (tag != b'JUNK' and size > MAX_PACKET) or pos+8+size > end:
            return
        nxt = pos+8+size
        if size & 1 and nxt < end and buf[nxt:nxt+1] == b'\0':
            nxt += 1
        yield tag, pos, size, nxt
        pos = nxt


def _chain(buf, start, end, cancel=None):
    """start부터 이어지는 정상 구조의 끝 위치와 길이>0 미디어 청크 수를 돌려준다."""
    cursor, count = start, 0
    for n, (tag, _, size, nxt) in enumerate(_structures(buf, start, end)):
        if n % 4096 == 0:
            _check(cancel)
        if tag is None:
            cursor = end
            break
        if size and MEDIA_TAG.fullmatch(tag):
            count += 1
        cursor = nxt
    return cursor, count


def _stale_index(buf, index, movi, limit):
    """검증에 실패한 idx1의 분산 표본 중 하나라도 제자리의 청크를 가리키는지 본다.

    맞는 엔트리가 하나도 없으면(청크 헤더 전부 소실, 쓰레기 idx1) 옛 녹화의 증거가 아니다.
    일부만 맞으면 앞부분이 덮어써진 옛 녹화의 idx1이다.
    """
    if index + 8 > len(buf):
        return False
    size = _u32(buf, index + 4)
    if size < 16 or size % 16 or index + 8 + size > len(buf):
        return False
    count = size // 16
    for n in sorted({i*(count-1)//max(1, min(count-1, 15)) for i in range(min(count, 16))}):
        entry = _entry(buf, index, n, count)
        if not entry:
            continue
        tag, _, off, length = entry
        for base in (0, movi):
            q = base + off
            if q+8+length <= min(index, limit) and buf[q:q+4] == tag and _u32(buf, q+4) == length:
                return True
    return False


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
        chain_end = chain_count = 0
        if end <= limit:
            # #30: movi 시작부터 이어지는 청크 사슬이 선언된 movi 끝에 정확히 닿으면 movi 크기가
            # 맞는 것이다. 그 끝을 넘어선 idx1(사전 할당 파일의 옛 녹화 잔재)은 경계로 쓰지 않는다.
            chain_end, chain_count = _chain(buf, p+12, end, cancel)
            if chain_count and chain_end == end:
                return p+12, end, 'movi'
        if index is not None and index >= p+12:
            return p+12, indexed_end, 'movi' if end == indexed_end else 'idx1_validated'
        if limit < len(buf) and end > limit:
            # With idx1 lost and conflicting embedded AVI headers, retain only
            # the contiguous chunk prefix. Never resync into the old recording.
            cursor, count = _chain(buf, p+12, limit, cancel)
            if count >= 3:
                return p+12, cursor, 'contiguous_before_embedded_avi'
            break
        if end <= limit:
            following = bytes(buf[end:end+4])
            if following == b'idx1' and _stale_index(buf, end, p+8, limit):
                # movi 끝의 idx1이 검증에는 실패했지만 일부 엔트리가 실제 청크를 가리킨다: 사전 할당
                # 크기 뒤에 남은 옛 녹화의 idx1이다(#30). 그 movi 크기도 믿지 않고, 헤더에서
                # 이어지는 청크 앞부분만 현재 녹화로 본다.
                if chain_count >= 3:
                    return p+12, chain_end, 'contiguous_before_rejected_index'
                break
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
            # #86: 바로 앞에 다른 스트림의 청크 헤더가 있으면 그 스트림(헤더 없는 후방 등)의 프레임이다.
            head = bytes(buf[p-8:p-4]) if p-8 >= start else b''
            if len(matching) != 1 or (re.fullmatch(rb'[0-9]{2}(?:dc|db)', head)
                                      and int(head[:2]) != matching[0]):
                pos = p+3
                continue
            sid, offset, next_pos = matching[0], p, stop+2
        else:
            tag = bytes(buf[p:p+4])
            sid = int(tag[:2])
            size = _u32(buf, p+4) if p+8 <= end else 0
            spec = specs.get(sid) if tag[2:] in (b'dc', b'db') else None
            if not spec:
                # #86: 스펙이 없는 스트림(선언되지 않은 후방 영상·오디오·GPS 텍스트)의 유효한
                # 청크는 통째로 건너뛴다. 1바이트씩 전진하면 그 안의 JPEG가 전방 프레임으로 섞인다.
                skip = p+8+size
                if size & 1 and skip < end and buf[skip:skip+1] == b'\0':
                    skip += 1
                if size <= MAX_PACKET and skip <= end and _chunk_landing(buf, skip, end):
                    pos = skip
                else:
                    pos = p+1
                continue
            if not (0 < size <= MAX_PACKET) or p+8+size > end:
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


def _chunk_landing(buf, pos, end):
    """청크를 건너뛴 자리가 또 다른 정상 구조(또는 끝)인지 본다. 크기 필드 손상이면 아니다."""
    if pos >= end:
        return True
    nonzero = NONZERO.search(buf, pos, min(end, pos+ZERO_RUN))
    if not nonzero:
        return False
    q = nonzero.start()
    tag = bytes(buf[q:q+4])
    return bool(MEDIA_TAG.fullmatch(tag) or tag in (b'JUNK', b'LIST', b'idx1', b'RIFF')
                or re.fullmatch(rb'ix[0-9]{2}', tag))


def _intact_gap(buf, start, end, sid):
    """Recognize interleaved chunks without mistaking silent audio for damage.

    A missing/rejected video chunk or unparseable gap breaks H.264 prediction.
    Accept the unpadded chunk layout used by some dashcams as well as RIFF pads.
    길이 0 영상 청크·LIST rec ·ix##·JUNK·짧은 0 패딩은 정상 구조다(#88).
    """
    video = (f'{sid:02d}dc'.encode(), f'{sid:02d}db'.encode())
    for tag, _, size, _ in _structures(buf, start, end):
        if tag is None:
            return True
        if tag in video and size:
            return False
    return False


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
            if b'\0'*ZERO_RUN in data:
                self.damaged_packets += 1
                self.break_prediction()
                return False
            types = _nal_types(data)
            self.sps |= 7 in types
            self.pps |= 8 in types
            if not types & {1, 5}:
                # #127: 그림이 없는 파라미터(SPS/PPS)·SEI·AUD 청크는 프레임이 아니다. 녹화 시작
                # 전후를 가리지 않고 다음 슬라이스 패킷 앞에 붙인다.
                if types & {6, 7, 8, 9} and len(self.config) < 16:
                    self.config.append(data)
                return False
            if not self.started:
                if not (5 in types and self.sps and self.pps):
                    self.dependent_packets += 1
                    return False
                self.started = True
            data = b''.join(self.config) + data
            self.config.clear()
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


def _spec_difference(streams, ref_streams):
    """손상본과 참조의 영상 스트림 설정 차이를 한 줄로 요약한다. 같으면 빈 문자열."""
    def summary(items):
        return [(s['codec'], s['width'], s['height'], s['rate']/s['scale'])
                for h, fmt in items if (s := _video_spec(h, fmt))]
    mine, theirs = summary(streams), summary(ref_streams)
    if mine == theirs:
        return ''
    def text(specs):
        return ', '.join(f'{c} {w}x{h} {fps:g}fps' for c, w, h, fps in specs) or '영상 스트림 없음'
    return f'손상본: {text(mine)} / 참조: {text(theirs)}'


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
                manifest['reference'] = {'path': str(reference), 'sha256': ref_hash,
                                         'usage': 'headers_only_user_selected'}
                # #128: 손상본에 쓸 만한 스트림 헤더가 남아 있으면 그것이 증거다. 참조 헤더로
                # 덮어쓰지 않고, 참조와 다르면(FPS·해상도·코덱) 경고만 남긴다.
                ref_streams = _headers(ref)
                if any(_video_spec(h, fmt) for h, fmt in streams):
                    manifest['reference']['usage'] = 'not_used_source_headers_intact'
                    difference = _spec_difference(streams, ref_streams)
                    if difference:
                        manifest['reference']['warning'] = ('손상본의 스트림 헤더와 참조 영상이 다릅니다('
                            + difference + '). 손상본 헤더를 사용했습니다. 참조가 같은 기기·설정인지 확인하세요.')
                else:
                    streams = ref_streams
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
