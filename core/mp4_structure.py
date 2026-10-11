"""Bounded ISO BMFF readers for recovery, independent of broken top-level sizes.

No reference sample offsets are ever used to read a damaged recording. Reject
external data references and sample description changes rather than guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter
from fractions import Fraction
import re
import struct

MAX_TABLE = 2_000_000
MAX_BOXES = 200_000
MAX_MOOV = 64 * 1024 * 1024


class InvalidMP4(ValueError):
    pass


def u32(data, pos):
    return struct.unpack_from('>I', data, pos)[0]


@dataclass(frozen=True)
class Box:
    kind: bytes
    start: int
    payload: int
    end: int
    truncated: bool = False


def box_at(data, pos, end, *, truncated_mdat=False):
    if pos < 0 or pos + 8 > end:
        raise InvalidMP4('incomplete box header')
    size, kind = struct.unpack_from('>I4s', data, pos)
    header = 8
    if size == 1:
        if pos + 16 > end:
            raise InvalidMP4('incomplete extended box header')
        size = struct.unpack_from('>Q', data, pos + 8)[0]
        header = 16
    elif size == 0:
        if kind != b'mdat':
            raise InvalidMP4('unbounded non-media box')
        size = end - pos
    if size < header:
        raise InvalidMP4('invalid box size')
    stop = pos + size
    truncated = stop > end
    if truncated and not (truncated_mdat and kind == b'mdat'):
        raise InvalidMP4('box outside enclosing extent')
    return Box(kind, pos, pos + header, min(stop, end), truncated)


def checked_range(count, check):
    """Keep large declared tables interruptible without checking every integer."""
    check()
    for i in range(count):
        if i and i % 1024 == 0:
            check()
        yield i


def scanned_matches(pattern, data, start, end, check):
    """Scan four-byte box markers in bounded windows, including split markers."""
    for begin in range(start, end, 1024 * 1024):
        check()
        stop = min(end, begin + 1024 * 1024)
        for match in pattern.finditer(data, begin, min(end, stop + 3)):
            if match.start() >= stop:
                break
            yield match


def children(data, start, end, check=lambda: None):
    result = []
    check()
    while start < end:
        if len(result) % 1024 == 0:
            check()
        if len(result) >= MAX_BOXES:
            raise InvalidMP4('too many boxes')
        b = box_at(data, start, end)
        result.append(b)
        start = b.end
    return result


def child(data, parent, kind, check=lambda: None):
    found = [b for b in children(data, parent.payload, parent.end, check) if b.kind == kind]
    if len(found) != 1:
        raise InvalidMP4('missing or duplicate ' + kind.decode('ascii', 'replace'))
    return found[0]


def table(data, b, width):
    if b.payload + 8 > b.end or bytes(data[b.payload:b.payload + 4]) != bytes(4):
        raise InvalidMP4('invalid table version/header')
    count = u32(data, b.payload + 4)
    if count > MAX_TABLE or b.payload + 8 + count * width != b.end:
        raise InvalidMP4('invalid table count/extent')
    return b.payload + 8, count


def config_nals(data, codec):
    """Return length-prefix width and the actual avcC/hvcC parameter sets."""
    nals = []
    if codec == 'h264':
        if len(data) < 7 or data[0] != 1:
            raise InvalidMP4('invalid avcC')
        width, p = (data[4] & 3) + 1, 6
        for group in range(2):
            if group == 0:
                count = data[5] & 31
            else:
                if p >= len(data):
                    raise InvalidMP4('incomplete avcC PPS')
                count, p = data[p], p + 1
            for _ in range(count):
                if p + 2 > len(data):
                    raise InvalidMP4('incomplete parameter length')
                size = int.from_bytes(data[p:p + 2], 'big'); p += 2
                if not size or p + size > len(data):
                    raise InvalidMP4('incomplete parameter set')
                nal = bytes(data[p:p + size]); p += size
                if nal[0] & 31 != (7 if group == 0 else 8):
                    raise InvalidMP4('wrong AVC parameter type')
                nals.append(nal)
    else:
        if len(data) < 23 or data[0] != 1:
            raise InvalidMP4('invalid hvcC')
        width, p = (data[21] & 3) + 1, 23
        if data[22] > 64:
            raise InvalidMP4('too many HEVC parameter arrays')
        for _ in range(data[22]):
            if p + 3 > len(data):
                raise InvalidMP4('incomplete HEVC array')
            kind, count = data[p] & 63, int.from_bytes(data[p + 1:p + 3], 'big'); p += 3
            if count > 512:
                raise InvalidMP4('too many HEVC parameter sets')
            for _ in range(count):
                if p + 2 > len(data):
                    raise InvalidMP4('incomplete HEVC length')
                size = int.from_bytes(data[p:p + 2], 'big'); p += 2
                if size < 2 or p + size > len(data):
                    raise InvalidMP4('incomplete HEVC parameter')
                nal = bytes(data[p:p + size]); p += size
                if (nal[0] >> 1) & 63 != kind:
                    raise InvalidMP4('wrong HEVC parameter type')
                if kind in (32, 33, 34):
                    nals.append(nal)
    if width not in (1, 2, 4):
        raise InvalidMP4('unsupported NAL length width')
    return width, nals


@dataclass
class Track:
    id: int
    handler: bytes
    codec: str = ''
    width: int = 0
    height: int = 0
    length_size: int = 4
    parameters: list[bytes] = field(default_factory=list)
    configuration: bytes = b''
    timescale: int = 0
    fps: Fraction | None = None
    samples: list[tuple[int, int, int]] = field(default_factory=list)
    table_error: str = ''
    break_offsets: set[int] = field(default_factory=set)
    composition: list[int] = field(default_factory=list)
    decode_times: list[int | None] = field(default_factory=list)
    durations: list[int] = field(default_factory=list)


def sample_sizes(data, stbl, check=lambda: None):
    sizes = [b for b in children(data, stbl.payload, stbl.end, check) if b.kind in (b'stsz', b'stz2')]
    if len(sizes) != 1:
        raise InvalidMP4('missing/duplicate sample sizes')
    sz = sizes[0]; p = sz.payload
    if p + 12 > sz.end or bytes(data[p:p+4]) != bytes(4):
        raise InvalidMP4('invalid sample size header')
    count = u32(data, p + 8)
    if count > MAX_TABLE:
        raise InvalidMP4('invalid sample size count')
    if sz.kind == b'stsz':
        fixed = u32(data, p + 4)
        if p + 12 + (0 if fixed else count * 4) != sz.end:
            raise InvalidMP4('invalid sample size extent')
        return [fixed if fixed else u32(data, p + 12 + i * 4) for i in checked_range(count, check)]
    width = data[p + 7]
    if (bytes(data[p+4:p+7]) != bytes(3) or width not in (4, 8, 16)
            or p + 12 + (count * width + 7) // 8 != sz.end):
        raise InvalidMP4('invalid compact sample sizes')
    if width == 4:
        return [(data[p+12+i//2] >> (4 if i % 2 == 0 else 0)) & 15 for i in checked_range(count, check)]
    return [int.from_bytes(data[p+12+i*(width//8):p+12+(i+1)*(width//8)], 'big')
            for i in checked_range(count, check)]


def parse_track(data, trak, check=lambda: None):
    tkhd = child(data, trak, b'tkhd', check)
    p = tkhd.payload + (20 if data[tkhd.payload] == 1 else 12)
    if p + 4 > tkhd.end:
        raise InvalidMP4('short tkhd')
    mdia = child(data, trak, b'mdia', check)
    hdlr, mdhd = child(data, mdia, b'hdlr', check), child(data, mdia, b'mdhd', check)
    if hdlr.payload + 12 > hdlr.end:
        raise InvalidMP4('short handler')
    t = Track(u32(data, p), bytes(data[hdlr.payload + 8:hdlr.payload + 12]))
    p = mdhd.payload + (20 if data[mdhd.payload] == 1 else 12)
    if p + 4 > mdhd.end:
        raise InvalidMP4('short mdhd')
    t.timescale = u32(data, p)
    if not 0 < t.timescale <= 1_000_000_000:
        raise InvalidMP4('invalid timescale')
    minf = child(data, mdia, b'minf', check)
    # A reference URL must never cause the recovery process to read another file.
    dinf = child(data, minf, b'dinf', check); dref = child(data, dinf, b'dref', check)
    p, count = dref.payload + 8, u32(data, dref.payload + 4)
    refs = children(data, p, dref.end, check)
    if count != len(refs) or not refs or any(
            r.kind not in (b'url ', b'alis') or r.end - r.payload != 4 or u32(data, r.payload) != 1 for r in refs):
        raise InvalidMP4('external or unsupported data reference')
    stbl = child(data, minf, b'stbl', check); stsd = child(data, stbl, b'stsd', check)
    if stsd.payload + 8 > stsd.end or u32(data, stsd.payload + 4) != 1:
        raise InvalidMP4('multiple/invalid sample descriptions')
    descs = children(data, stsd.payload + 8, stsd.end, check)
    if len(descs) != 1:
        raise InvalidMP4('invalid sample description')
    desc = descs[0]
    if desc.payload + 8 > desc.end or int.from_bytes(data[desc.payload + 6:desc.payload + 8], 'big') != 1:
        raise InvalidMP4('invalid sample data reference')
    if t.handler == b'vide':
        codecs = {b'avc1': 'h264', b'avc3': 'h264', b'hvc1': 'hevc', b'hev1': 'hevc'}
        t.codec = codecs.get(desc.kind, '')
        if not t.codec or desc.payload + 78 > desc.end:
            raise InvalidMP4('unsupported video codec/entry')
        t.width, t.height = struct.unpack_from('>HH', data, desc.payload + 24)
        cc = [b for b in children(data, desc.payload + 78, desc.end, check)
              if b.kind == (b'avcC' if t.codec == 'h264' else b'hvcC')]
        if len(cc) != 1:
            raise InvalidMP4('missing codec configuration')
        t.configuration = bytes(data[cc[0].payload:cc[0].end])
        t.length_size, t.parameters = config_nals(t.configuration, t.codec)
    if t.handler not in (b'vide', b'text', b'sbtl', b'subt', b'meta'):
        return t
    try:
        sizes = sample_sizes(data, stbl, check); count = len(sizes)
        stsc = child(data, stbl, b'stsc', check); p, n = table(data, stsc, 12)
        mapping = [struct.unpack_from('>III', data, p + i * 12) for i in checked_range(n, check)]
        offsets = [b for b in children(data, stbl.payload, stbl.end, check) if b.kind in (b'stco', b'co64')]
        if len(offsets) != 1:
            raise InvalidMP4('missing/duplicate chunk offsets')
        off = offsets[0]; width = 4 if off.kind == b'stco' else 8
        p, n = table(data, off, width)
        chunks = [int.from_bytes(data[p + i * width:p + (i + 1) * width], 'big')
                  for i in checked_range(n, check)]
        if count and (not mapping or mapping[0][0] != 1 or not chunks):
            raise InvalidMP4('missing chunk mapping')
        if any(a[0] >= b[0] for a, b in zip(mapping, mapping[1:])) or any(
                not 1 <= first <= len(chunks) or not 1 <= number <= MAX_TABLE or desc != 1
                for first, number, desc in mapping):
            raise InvalidMP4('invalid chunk mapping')
        i, m = 0, 0
        previous_end = -1
        for c, pos in enumerate(chunks, 1):
            if c % 1024 == 1:
                check()
            while m + 1 < len(mapping) and mapping[m + 1][0] <= c:
                m += 1
            if pos < previous_end:
                raise InvalidMP4('overlapping/non-monotonic chunks')
            number = mapping[m][1]
            if i + number > count:
                raise InvalidMP4('sample count mismatch')
            for size in sizes[i:i + number]:
                if i % 1024 == 0:
                    check()
                if not 0 < size <= 64 * 1024 * 1024:
                    raise InvalidMP4('invalid media sample size')
                t.samples.append((pos, size, i + 1)); pos += size; i += 1
            previous_end = pos
        if i != count:
            raise InvalidMP4('incomplete sample mapping')
        stts = child(data, stbl, b'stts', check); p, n = table(data, stts, 8)
        runs = [struct.unpack_from('>II', data, p + i * 8) for i in checked_range(n, check)]
        if sum(a for a, _ in runs) != count or any(not a or not b for a, b in runs):
            raise InvalidMP4('invalid sample durations')
        if count:
            t.fps = Fraction(count * t.timescale, sum(a * b for a, b in runs))
        ticks = 0
        for number, duration in runs:
            for _ in checked_range(number, check):
                t.decode_times.append(ticks); t.durations.append(duration); ticks += duration
        ctts = [b for b in children(data, stbl.payload, stbl.end, check) if b.kind == b'ctts']
        if ctts:
            ct = ctts[0]; p = ct.payload
            if len(ctts) != 1 or p + 8 > ct.end or data[p] not in (0, 1):
                raise InvalidMP4('invalid composition table')
            n = u32(data, p + 4)
            if n > MAX_TABLE or p + 8 + n * 8 != ct.end:
                raise InvalidMP4('invalid composition table extent')
            for i in checked_range(n, check):
                first = p + 8 + i * 8
                repeat = u32(data, first)
                value = int.from_bytes(data[first + 4:first + 8], 'big', signed=data[p] == 1)
                if not repeat or len(t.composition) + repeat > count:
                    raise InvalidMP4('invalid composition run')
                t.composition.extend([value] * repeat)
            if len(t.composition) != count:
                raise InvalidMP4('composition sample count mismatch')
    except (InvalidMP4, struct.error, IndexError) as exc:
        t.samples.clear(); t.composition.clear(); t.decode_times.clear(); t.durations.clear(); t.table_error = str(exc)
    return t


def parse_moov(data, box, check=lambda: None):
    if box.end - box.start > MAX_MOOV:
        raise InvalidMP4('moov exceeds recovery limit')
    parts = children(data, box.payload, box.end, check)
    mvhd = [b for b in parts if b.kind == b'mvhd']
    tracks, errors = [], []
    if len(mvhd) != 1 or mvhd[0].end - mvhd[0].payload < 96:
        # Movie-level duration/clock is not used to recover track samples or
        # playback rate. Intact, independently checked tracks can survive it.
        errors.append('invalid movie header; independently validated track tables used')
    for b in parts:
        check()
        if b.kind != b'trak':
            continue
        try:
            tracks.append(parse_track(data, b, check))
        except (InvalidMP4, struct.error, IndexError) as exc:
            errors.append(str(exc))
    if not tracks or len(tracks) > 32 or len({t.id for t in tracks}) != len(tracks):
        raise InvalidMP4('invalid movie tracks')
    defaults = {}
    for b in parts:
        if b.kind == b'mvex':
            for tr in children(data, b.payload, b.end, check):
                if tr.kind == b'trex' and tr.end - tr.payload == 24:
                    tid, desc, duration, size, flags = struct.unpack_from('>IIIII', data, tr.payload + 4)
                    if desc == 1:
                        defaults[tid] = (duration, size, flags)
    return tracks, defaults, errors


SIGNATURE = re.compile(rb'ftyp|moov|moof|mdat')


def valid_fragment(data, box, check=lambda: None):
    parts = children(data, box.payload, box.end, check)
    return (any(x.kind == b'mfhd' and x.end-x.payload == 8 for x in parts)
            and any(x.kind == b'traf' for x in parts))


def discover(data, cancel_check=lambda: None):
    """Walk intact extents, resync validated boxes only through damaged gaps.

    Intact mdat/free payloads are not searched for movie headers. A separate
    ftyp search bounds the first recording even if it has a zero-size mdat.
    """
    end, p, boxes, gaps = len(data), 0, [], []
    while p + 8 <= end:
        cancel_check()
        if len(boxes) >= MAX_BOXES:
            raise InvalidMP4('too many top-level boxes')
        try:
            b = box_at(data, p, end, truncated_mdat=True)
            if b.kind not in (b'ftyp', b'moov', b'moof', b'mdat', b'free', b'skip', b'wide',
                              b'sidx', b'mfra', b'pdin', b'uuid', b'prft', b'emsg', b'styp'):
                raise InvalidMP4('unknown top-level box in recovery')
            if b.kind == b'moov':
                parse_moov(data, b, cancel_check)
            elif b.kind == b'moof':
                if not valid_fragment(data, b, cancel_check):
                    raise InvalidMP4('invalid fragment header')
            elif b.kind == b'ftyp' and (b.end - b.payload < 8 or (b.end - b.payload) % 4):
                raise InvalidMP4('invalid ftyp')
            if b.kind == b'mdat' and b.truncated:
                # A corrupted oversized mdat must not hide the source's intact
                # moov. Accept only a complete movie whose live sample extent
                # lies before it, then use that independently checked boundary.
                for match in scanned_matches(re.compile(b'moov'), data, b.payload, b.end, cancel_check):
                    cancel_check()
                    try:
                        candidate = box_at(data, match.start() - 4, end)
                        ts, _, _ = parse_moov(data, candidate, cancel_check)
                        video = [t for t in ts if t.handler == b'vide' and t.samples]
                        if video and all(b.payload <= a and a + n <= candidate.start
                                         for t in video for a, n, _ in t.samples):
                            b = Box(b.kind, b.start, b.payload, candidate.start, True)
                            break
                    except (InvalidMP4, struct.error, IndexError):
                        continue
            boxes.append(b); p = b.end
        except (InvalidMP4, struct.error, IndexError):
            # At a previously established box boundary, a damaged fragment
            # header can still delimit its following media. Do not accept
            # arbitrary 'mdat' words encountered in audio/free/slack payloads.
            try:
                broken = box_at(data, p, end)
                anchored = any(x.end == p for x in boxes)
                if anchored and broken.kind in (b'moof', bytes(4)) and 16 <= broken.end-p <= 1024*1024:
                    if valid_fragment(data, broken, cancel_check):
                        boxes.append(Box(b'moof', broken.start, broken.payload, broken.end))
                        gaps.append((p+4, p+8)); p = broken.end; continue
                    following = box_at(data, broken.end, end, truncated_mdat=True)
                    if following.kind == b'mdat':
                        next_box = box_at(data, following.end, end) if following.end < end else None
                        if (next_box is None or next_box.kind in (b'free', b'skip')
                                or next_box.kind == b'moof' and valid_fragment(data, next_box, cancel_check)):
                            boxes.append(following); gaps.append((p, broken.end)); p = following.end; continue
            except (InvalidMP4, struct.error, IndexError):
                pass
            found = None
            for match in SIGNATURE.finditer(data, p + 5, min(end, p + 1024 * 1024)):
                q = match.start() - 4
                try:
                    b = box_at(data, q, end, truncated_mdat=True)
                    if b.kind == b'moov':
                        parse_moov(data, b, cancel_check)
                    elif b.kind == b'moof':
                        if not valid_fragment(data, b, cancel_check):
                            continue
                    elif b.kind == b'ftyp':
                        if b.end - b.payload < 8 or (b.end - b.payload) % 4:
                            continue
                    else:
                        # A random mdat word cannot establish an allocation boundary.
                        continue
                    found = q; break
                except (InvalidMP4, struct.error, IndexError):
                    continue
            q = found if found is not None else min(end, p + 1024 * 1024 - 8)
            gaps.append((p, q)); p = q
    ftyps, embedded = [], []
    for match in scanned_matches(re.compile(b'ftyp'), data, 0, end, cancel_check):
        cancel_check()
        try:
            b = box_at(data, match.start() - 4, end)
            if (8 <= b.end - b.payload <= 256 and (b.end - b.payload) % 4 == 0
                    and bytes(data[b.payload:b.payload + 4]) in
                    (b'isom', b'iso2', b'iso4', b'iso5', b'iso6', b'avc1', b'mp41', b'mp42',
                     b'M4V ', b'qt  ', b'dash', b'msdh', b'msix')):
                owner = next((x for x in boxes if x.kind in (b'mdat', b'free', b'skip', b'moov')
                              and x.payload <= b.start < x.end), None)
                if owner is None:
                    ftyps.append(b.start)
                elif owner.kind == b'mdat':
                    embedded.append(b)
        except (InvalidMP4, struct.error):
            pass
    limit = next((p for p in sorted(ftyps) if p > 0), end)
    return sorted([b for b in boxes + embedded if b.start < limit], key=lambda b: b.start), limit, gaps


def fragment_samples(data, boxes, tracks, defaults, check=lambda: None):
    """Resolve tfhd/trun offsets, including 64-bit bases and per-track defaults."""
    byid = {t.id: t for t in tracks}
    media = [(b.payload, b.end) for b in boxes if b.kind == b'mdat']
    duration_counts, errors, dts_ends = {}, [], {}
    sample_numbers = {t.id: max((n for _, _, n in t.samples), default=0) for t in tracks}
    broken_tracks = set()
    for moof in (b for b in boxes if b.kind == b'moof'):
        check(); previous_end = moof.start
        for traf in (b for b in children(data, moof.payload, moof.end, check) if b.kind == b'traf'):
            tid = None
            try:
                tfhd = child(data, traf, b'tfhd', check); p = tfhd.payload
                if p + 8 > tfhd.end or data[p] != 0:
                    raise InvalidMP4('invalid tfhd')
                flags, tid = u32(data, p) & 0xFFFFFF, u32(data, p + 4); p += 8
                if flags & ~0x03003B:
                    raise InvalidMP4('unsupported tfhd flags')
                duration, size, _ = defaults.get(tid, (0, 0, 0))
                base = moof.start if flags & 0x020000 else previous_end
                if flags & 1:
                    base = struct.unpack_from('>Q', data, p)[0]; p += 8
                if flags & 2:
                    if u32(data, p) != 1:
                        raise InvalidMP4('unsupported fragment sample description')
                    p += 4
                if flags & 8:
                    duration = u32(data, p); p += 4
                if flags & 16:
                    size = u32(data, p); p += 4
                if flags & 32:
                    p += 4
                if p != tfhd.end:
                    raise InvalidMP4('invalid tfhd extent')
                t = byid.get(tid); cursor = base; pending = []; ticks = 0
                number = sample_numbers.get(tid, 0)
                pending_composition, pending_times, pending_durations, kept_durations = [], [], [], Counter()
                excluded = 0
                timing = [b for b in children(data, traf.payload, traf.end, check) if b.kind == b'tfdt']
                dts = None
                if timing:
                    td = timing[0]
                    width = 8 if data[td.payload] == 1 else 4
                    if (len(timing) != 1 or td.payload + 4 + width != td.end
                            or data[td.payload] not in (0, 1)):
                        raise InvalidMP4('invalid fragment decode time')
                    dts = int.from_bytes(data[td.payload + 4:td.end], 'big')
                for run in (b for b in children(data, traf.payload, traf.end, check) if b.kind == b'trun'):
                    check()
                    p = run.payload
                    if p + 8 > run.end or data[p] not in (0, 1):
                        raise InvalidMP4('invalid trun')
                    rf, count = u32(data, p) & 0xFFFFFF, u32(data, p + 4); p += 8
                    if count > MAX_TABLE or rf & ~0xF05:
                        raise InvalidMP4('invalid trun count/flags')
                    if rf & 1:
                        cursor = base + struct.unpack_from('>i', data, p)[0]; p += 4
                    if rf & 4:
                        p += 4
                    width = 4 * sum(bool(rf & flag) for flag in (0x100, 0x200, 0x400, 0x800))
                    if p + count * width != run.end:
                        raise InvalidMP4('invalid trun extent')
                    for _ in checked_range(count, check):
                        sd, ss = duration, size
                        if rf & 0x100:
                            sd = u32(data, p); p += 4
                        if rf & 0x200:
                            ss = u32(data, p); p += 4
                        if rf & 0x400:
                            p += 4
                        composition = 0
                        if rf & 0x800:
                            composition = int.from_bytes(data[p:p + 4], 'big', signed=data[run.payload] == 1)
                            p += 4
                        if not 0 < ss <= 64 * 1024 * 1024 or cursor < 0:
                            raise InvalidMP4('invalid fragment sample size/offset')
                        number += 1
                        if number > MAX_TABLE:
                            raise InvalidMP4('too many declared fragment samples')
                        pending_composition.append(composition)
                        pending_times.append(dts + ticks if dts is not None else None)
                        pending_durations.append(sd)
                        if any(a <= cursor and cursor + ss <= b for a, b in media):
                            pending.append((cursor, ss, number))
                            kept_durations[sd] += 1
                        else:
                            excluded += 1
                        ticks += sd
                        cursor += ss
                    if p != run.end:
                        raise InvalidMP4('invalid trun extent')
                if t:
                    if len(t.samples) + len(pending) > MAX_TABLE:
                        raise InvalidMP4('too many fragment samples')
                    if pending and (tid in broken_tracks or dts is None
                                    or (tid in dts_ends and dts != dts_ends[tid])):
                        t.break_offsets.add(pending[0][0])
                    # Keep source sample numbers, including missing samples, so
                    # a skipped mdat cannot silently bridge prediction chains.
                    old_number = sample_numbers.get(tid, 0)
                    if len(t.composition) < old_number:
                        t.composition.extend([0] * (old_number - len(t.composition)))
                    t.composition.extend(pending_composition)
                    if len(t.decode_times) < old_number:
                        t.decode_times.extend([None] * (old_number - len(t.decode_times)))
                        t.durations.extend([0] * (old_number - len(t.durations)))
                    t.decode_times.extend(pending_times); t.durations.extend(pending_durations)
                    sample_numbers[tid] = number
                    t.samples.extend(pending)
                    duration_counts.setdefault(tid, Counter()).update(kept_durations)
                    if excluded:
                        errors.append(f'moof@{moof.start} track {tid}: {excluded} samples outside surviving mdat')
                    if pending:
                        broken_tracks.discard(tid)
                    if dts is not None:
                        dts_ends[tid] = dts + ticks
                previous_end = cursor
            except (InvalidMP4, struct.error, IndexError) as exc:
                errors.append(f'moof@{moof.start}: {exc}')
                if tid in byid:
                    broken_tracks.add(tid)
    for t in tracks:
        counts = duration_counts.get(t.id)
        if counts and not counts.get(0):
            duration, frequency = counts.most_common(1)[0]
            total = sum(counts.values())
            # An audio-alignment extension to one picture must not change a
            # CFR recording's nominal playback rate. Missing pictures do not
            # contribute durations to the review video's rate either.
            t.fps = (Fraction(t.timescale, duration) if frequency * 2 > total else
                     Fraction(total * t.timescale, sum(d * n for d, n in counts.items())))
    return errors
