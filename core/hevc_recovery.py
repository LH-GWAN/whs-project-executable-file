"""Small HEVC headers for bounded, single-channel MP4 NAL recovery.

ITU-T H.265 7.3.2.2, 7.3.2.3, 7.3.6.1. The raw path deliberately requires
zero picture reordering in every SPS sublayer. Indexed HEVC with B pictures
continues to use its surviving source composition table.
"""
from core.mp4_structure import InvalidMP4


def sps_header(nal):
    from core.mp4_recovery import Bits, rbsp
    data = rbsp(nal[2:]); b = Bits(data)
    b.get(4); layers = b.get(3); nested = b.get()
    if layers > 6:
        raise InvalidMP4('invalid HEVC temporal layers')
    ptl = data[1:13]
    b.get(8); b.get(32); b.get(32); b.get(16); b.get(8)
    sub = [(b.get(), b.get()) for _ in range(layers)]
    if layers:
        b.get(2 * (8-layers))
    for profile, level in sub:
        if profile:
            b.get(32); b.get(32); b.get(24)
        if level:
            b.get(8)
    sid, chroma = b.ue(), b.ue()
    if sid > 15 or chroma > 3:
        raise InvalidMP4('invalid HEVC SPS id/chroma')
    separate = b.get() if chroma == 3 else 0
    width, height = b.ue(), b.ue()
    crop = [b.ue() for _ in range(4)] if b.get() else [0]*4
    depth_luma, depth_chroma, poc_bits = b.ue(), b.ue(), b.ue()+4
    if depth_luma > 8 or depth_chroma > 8 or not 4 <= poc_bits <= 16:
        raise InvalidMP4('invalid HEVC bit depth/POC')
    subw, subh = ((1, 1) if chroma in (0, 3) else (2, 2) if chroma == 1 else (2, 1))
    display_w = width - (crop[0]+crop[1])*subw
    display_h = height - (crop[2]+crop[3])*subh
    if not 0 < display_w <= 16384 or not 0 < display_h <= 16384:
        raise InvalidMP4('invalid HEVC dimensions')
    ordering = b.get(); reorder = []
    for _ in range(0 if ordering else layers, layers+1):
        buffering, count = b.ue()+1, b.ue(); b.ue()
        if not 1 <= buffering <= 16 or count >= buffering:
            raise InvalidMP4('invalid HEVC reorder configuration')
        reorder.append(count)
    cb = b.ue()+3; diff = b.ue()
    if not 3 <= cb <= 6 or not cb <= cb+diff <= 6:
        raise InvalidMP4('invalid HEVC coding block size')
    block = 1 << (cb+diff)
    ctbs = ((width+block-1)//block) * ((height+block-1)//block)
    return dict(id=sid, width=display_w, height=display_h, poc_bits=poc_bits,
                separate=separate, reorder=max(reorder), address_bits=(ctbs-1).bit_length(),
                ptl=ptl, layers=layers+1, nested=nested, chroma=chroma,
                depth_luma=depth_luma, depth_chroma=depth_chroma)


def pps_header(nal):
    from core.mp4_recovery import Bits, rbsp
    b = Bits(rbsp(nal[2:]))
    pid, sid = b.ue(), b.ue()
    if pid > 63 or sid > 15:
        raise InvalidMP4('invalid HEVC PPS id')
    return dict(id=pid, sps=sid, dependent=b.get(), output=b.get(), extra=b.get(3))


class HEVC:
    def __init__(self, parameters):
        self.sps, self.pps = {}, {}
        for nal in parameters:
            kind = (nal[0] >> 1) & 63
            if kind == 33:
                s = sps_header(nal); self.sps[s['id']] = s
            elif kind == 34:
                p = pps_header(nal); self.pps[p['id']] = p
        if not self.sps or not self.pps or any(p['sps'] not in self.sps for p in self.pps.values()):
            raise InvalidMP4('HEVC VPS/SPS/PPS 설정이 부족합니다.')
        if any(s['reorder'] for s in self.sps.values()):
            raise InvalidMP4('샘플 표 없는 HEVC B-picture의 표시 순서는 추정하지 않습니다.')

    def picture(self, nal):
        from core.mp4_recovery import Bits, rbsp
        kind = (nal[0] >> 1) & 63; b = Bits(rbsp(nal[2:128]))
        first = b.get()
        if 16 <= kind <= 23:
            b.get()
        pid = b.ue()
        if pid not in self.pps:
            raise InvalidMP4('unknown HEVC slice PPS')
        p = self.pps[pid]; s = self.sps[p['sps']]
        dependent = 0
        if not first:
            dependent = b.get() if p['dependent'] else 0
            b.get(s['address_bits'])
        if dependent:
            return first, pid, None, 1 << s['poc_bits']
        b.get(p['extra']); typ = b.ue()
        if typ not in (1, 2):
            raise InvalidMP4('unsupported/raw reordered HEVC slice')
        if p['output'] and not b.get():
            raise InvalidMP4('non-output HEVC picture')
        if s['separate']:
            b.get(2)
        poc = 0 if kind in (19, 20) else b.get(s['poc_bits'])
        return first, pid, poc, 1 << s['poc_bits']


def carved_hevc_frames(buf, track, media, check, stats):
    from core.mp4_recovery import Frame, MAX_NAL, nal_kind
    import re
    if track.length_size != 4:
        stats['withheld'] = '샘플 표 없는 HEVC는 4바이트 NAL 길이만 지원합니다.'
        return
    try:
        parser = HEVC(track.parameters)
        if any((s['width'], s['height']) != (track.width, track.height) for s in parser.sps.values()):
            raise InvalidMP4('HEVC SPS dimensions differ from source/reference settings')
    except InvalidMP4 as exc:
        stats['withheld'] = str(exc)
        return
    # Single-layer, temporal-id-zero AVC-style length-prefixed HEVC. A match
    # is only a candidate: validate its extent, parameter ids and POC, then
    # require the same full decode and byte audit as indexed recovery.
    marker = re.compile(rb'\x00[\x00-\xff]{3}[\x00-\x50]\x01')
    for begin, end in media:
        pos = begin; current = None; waiting = True; previous_poc = None; suppress_rasl = False
        while pos+6 <= end:
            check()
            m = marker.search(buf, pos, min(end, pos+1024*1024))
            if not m:
                pos = min(end, pos+1024*1024-8); continue
            p = m.start(); pos = p+1; n = int.from_bytes(buf[p:p+4], 'big')
            if not 3 <= n <= MAX_NAL or p+4+n > end:
                continue
            nal = bytes(buf[p+4:p+4+n])
            try:
                kind = nal_kind(nal, 'hevc')
                if kind in (32, 33, 34):
                    if nal not in track.parameters:
                        raise ValueError('HEVC parameters changed inside unindexed media; channel mixing withheld')
                    pos = p+4+n; continue
                if kind >= 32:
                    pos = p+4+n; continue
                if n < 16:
                    continue
                first, pid, poc, modulus = parser.picture(nal)
                if first:
                    if current and not waiting:
                        yield current
                    current = None
                    random_access = kind in (16, 17, 18, 19, 20, 21)
                    if random_access:
                        suppress_rasl = waiting and kind == 21
                        waiting = False; previous_poc = None
                    elif previous_poc is not None and poc != (previous_poc+1) % modulus:
                        waiting = True; stats['prediction_breaks'] += 1
                    if suppress_rasl and kind in (8, 9):
                        stats['discarded_rasl'] = stats.get('discarded_rasl', 0)+1
                        pos = p+4+n; continue
                    if waiting:
                        stats['waiting_for_idr'] += 1; pos = p+4+n; continue
                    current = Frame(p, n+4, 0, idr=kind in (16, 17, 18, 19, 20), cra=kind==21,
                                    rasl=kind in (8, 9), key=(pid, poc), frame_num=poc, modulus=modulus)
                    previous_poc = poc
                elif current is None or current.key[0] != pid or (poc is not None and current.key[1] != poc):
                    continue
                current.nals.append((p+4, n)); current.size = p+4+n-current.offset; pos = p+4+n
            except (InvalidMP4, IndexError):
                continue
        if current and not waiting:
            yield current
