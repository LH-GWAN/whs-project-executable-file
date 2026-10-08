"""A minimal review-only MP4 muxer: copy compressed NALs, rebuild all tables.

Video only, co64 offsets, signed composition offsets, no reference media data.
AVC POC reconstruction supports progressive type-0 streams when ctts is lost.
"""
import struct
from core.mp4_structure import InvalidMP4


def box(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def full(kind, payload=b'', version=0, flags=0):
    return box(kind, bytes([version]) + flags.to_bytes(3, 'big') + payload)


MATRIX = struct.pack('>9I', 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def composition_offsets(track, frames, tick, scale):
    if track.composition and all(0 < f.sample <= len(track.composition) for f in frames):
        return [round(track.composition[f.sample - 1] * scale / track.timescale) for f in frames]
    if track.codec != 'h264':
        return [0] * len(frames)
    if not any(f.b_picture for f in frames):
        return [0] * len(frames)
    from core.mp4_recovery import AVC
    avc = AVC(track.parameters)
    # A missing ctts must not invent B-picture display order. Indexed non-B
    # HEVC uses zero above; raw HEVC is explicitly unsupported by the carver.
    if not avc.sps or any(s['poc'] != 0 or not s['frame_only'] for s in avc.sps.values()):
        raise InvalidMP4('ctts 없는 카빙 MP4는 progressive AVC POC type 0만 지원합니다.')
    result, start = [], 0
    while start < len(frames):
        stop = start + 1
        while stop < len(frames) and not frames[stop].idr:
            stop += 1
        group = frames[start:stop]; orders = []; msb = previous = 0
        for f in group:
            pid = f.key[0]; s = avc.sps[avc.pps[pid]['sps']]; maximum = 1 << s['poc_bits']
            lsb = f.key[-1]
            current_msb = msb
            if lsb < previous and previous - lsb >= maximum // 2:
                current_msb += maximum
            elif lsb > previous and lsb - previous > maximum // 2:
                current_msb -= maximum
            orders.append(current_msb + lsb)
            if f.reference:
                msb, previous = current_msb, lsb
        if len(set(orders)) != len(orders):
            raise InvalidMP4('중복 POC: 원래 picture 순서를 확인할 수 없습니다.')
        ranked = {value: i for i, value in enumerate(sorted(orders))}
        result.extend((ranked[value] - i) * tick for i, value in enumerate(orders))
        start = stop
    return result


def codec_config(track):
    if track.codec == 'hevc':
        data = bytearray(track.configuration)
        if len(data) < 23:
            raise InvalidMP4('HEVC 설정이 없습니다.')
        data[21] = (data[21] & ~3) | 3
        return box(b'hvcC', bytes(data))
    sps = [n for n in track.parameters if n[0] & 31 == 7]
    pps = [n for n in track.parameters if n[0] & 31 == 8]
    if not sps or not pps or len(sps) > 31 or len(pps) > 255 or len(sps[0]) < 4:
        raise InvalidMP4('AVC SPS/PPS 설정이 없습니다.')
    data = b'\1' + sps[0][1:4] + b'\xff' + bytes([0xe0 | len(sps)])
    for n in sps:
        data += struct.pack('>H', len(n)) + n
    data += bytes([len(pps)])
    for n in pps:
        data += struct.pack('>H', len(n)) + n
    return box(b'avcC', data)


def write_mp4(target, buf, track, frames, check=lambda: None):
    if not track.fps or not 1 <= track.fps <= 240:
        raise InvalidMP4('FPS를 확인할 수 없어 MP4 생성 보류')
    scale, tick = track.fps.numerator, track.fps.denominator
    if scale > 1_000_000_000 or tick > 1_000_000_000 or not 0 < track.width <= 16384 or not 0 < track.height <= 16384:
        raise InvalidMP4('지원 범위 밖의 FPS/해상도')
    offsets = composition_offsets(track, frames, tick, scale)
    if any(not -(1 << 31) <= n < (1 << 31) for n in offsets):
        raise InvalidMP4('composition offset 범위 초과')
    config = codec_config(track); sizes, positions = [], []
    ftyp = box(b'ftyp', b'isom' + struct.pack('>I', 512) + b'isomiso6mp41')
    with target.open('wb') as out:
        out.write(ftyp); mdat = out.tell(); out.write(struct.pack('>I4sQ', 1, b'mdat', 0))
        for frame in frames:
            check(); positions.append(out.tell()); size = 0
            for p, n in frame.nals:
                out.write(struct.pack('>I', n)); out.write(buf[p:p+n]); size += 4 + n
            sizes.append(size)
        end = out.tell(); out.seek(mdat + 8); out.write(struct.pack('>Q', end - mdat)); out.seek(end)
        duration = len(frames) * tick
        mvhd = full(b'mvhd', struct.pack('>QQIQ', 0, 0, scale, duration) +
                    struct.pack('>IH', 0x10000, 0x100) + bytes(10) + MATRIX + bytes(24) + struct.pack('>I', 2), version=1)
        tkhd = full(b'tkhd', struct.pack('>QQIIQ', 0, 0, 1, 0, duration) + bytes(16) + MATRIX +
                    struct.pack('>II', track.width << 16, track.height << 16), version=1, flags=7)
        mdhd = full(b'mdhd', struct.pack('>QQIQHH', 0, 0, scale, duration, 0x55c4, 0), version=1)
        hdlr = full(b'hdlr', bytes(4) + b'vide' + bytes(12) + b'IDAS recovered video\0')
        entry = bytes(6) + struct.pack('>H', 1) + bytes(16) + struct.pack('>HHII', track.width,
                    track.height, 0x480000, 0x480000) + bytes(4) + struct.pack('>H', 1) + bytes(32) + struct.pack('>Hh', 24, -1)
        entry = box(b'avc1' if track.codec == 'h264' else b'hvc1', entry + config)
        stsd = full(b'stsd', struct.pack('>I', 1) + entry)
        stts = full(b'stts', struct.pack('>III', 1, len(frames), tick))
        stsc = full(b'stsc', struct.pack('>IIII', 1, 1, 1, 1))
        stsz = full(b'stsz', struct.pack('>II', 0, len(sizes)) + b''.join(struct.pack('>I', n) for n in sizes))
        co64 = full(b'co64', struct.pack('>I', len(positions)) + b''.join(struct.pack('>Q', n) for n in positions))
        keys = [i+1 for i, f in enumerate(frames) if f.idr]
        stss = full(b'stss', struct.pack('>I', len(keys)) + b''.join(struct.pack('>I', n) for n in keys))
        ctts = full(b'ctts', struct.pack('>I', len(offsets)) + b''.join(struct.pack('>Ii', 1, n) for n in offsets), version=1)
        stbl = box(b'stbl', stsd + stts + stsc + stsz + co64 + stss + ctts)
        dinf = box(b'dinf', full(b'dref', struct.pack('>I', 1) + full(b'url ', flags=1)))
        minf = box(b'minf', full(b'vmhd', bytes(8), flags=1) + dinf + stbl)
        trak = box(b'trak', tkhd + box(b'mdia', mdhd + hdlr + minf))
        out.write(box(b'moov', mvhd + trak)); check()
