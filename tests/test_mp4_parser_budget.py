"""Real BMFF tables must remain cancellable before allocating declared samples."""
import re
import struct
import threading
import time

import pytest

from core.avi_recovery import RecoveryCancelled, _check
from core.mp4_recovery import RecoveryDeadline, RecoveryTimedOut
from core.mp4_structure import (MAX_TABLE, children, child, discover, fragment_samples,
                                parse_moov, scanned_matches)
from test_mp4_recovery import mp4_media


def box(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def declared_classic(data):
    """Keep real codec settings, but declare two million one-byte samples."""
    def rebuild(start, end):
        out = bytearray()
        for b in children(data, start, end):
            payload = data[b.payload:b.end]
            if b.kind in (b'moov', b'trak', b'mdia', b'minf', b'stbl'):
                payload = rebuild(b.payload, b.end)
            elif b.kind == b'stsz':
                payload = struct.pack('>III', 0, 1, MAX_TABLE)
            elif b.kind == b'stsc':
                payload = struct.pack('>IIIII', 0, 1, 1, MAX_TABLE, 1)
            elif b.kind in (b'stco', b'co64'):
                payload = struct.pack('>II', 0, 1) + (48).to_bytes(4 if b.kind == b'stco' else 8, 'big')
            elif b.kind == b'stts':
                payload = struct.pack('>IIII', 0, 1, MAX_TABLE, 1000)
            out.extend(box(b.kind, payload))
        return bytes(out)
    return rebuild(0, len(data))


def declared_fragment(data):
    """An extent-valid zero-entry-width trun can still declare excessive work."""
    boxes, _, _ = discover(data)
    moov = next(b for b in boxes if b.kind == b'moov')
    init = data[moov.start:moov.end]
    tfhd = box(b'tfhd', struct.pack('>IIII', 0x020018, 1, 1000, 1))
    tfdt = box(b'tfdt', struct.pack('>II', 0, 0))
    def fragment(offset):
        run = box(b'trun', struct.pack('>IIi', 1, MAX_TABLE, offset))
        return box(b'moof', box(b'mfhd', struct.pack('>II', 0, 1)) + box(b'traf', tfhd + tfdt + run))
    moof = fragment(len(fragment(0)) + 8)
    return init + moof + box(b'mdat', bytes(4096))


@pytest.mark.parametrize('structure', ['classic', 'fragmented'])
@pytest.mark.parametrize('control', ['deadline', 'cancel'])
def test_large_declared_tables_stop_during_parsing(mp4_media, structure, control):
    raw = mp4_media['avc' if structure == 'classic' else 'fragmented'].read_bytes()
    data = declared_classic(raw) if structure == 'classic' else declared_fragment(raw)
    # Locate just the movie before installing the deadline: setup time is excluded.
    boxes = children(data, 0, len(data))
    moov = next(b for b in boxes if b.kind == b'moov')
    tracks, defaults = ([], {}) if structure == 'classic' else parse_moov(data, moov)[:2]
    timer = None
    if control == 'deadline':
        token = RecoveryDeadline(None, .01)
        error = RecoveryTimedOut
    else:
        token = threading.Event()
        timer = threading.Timer(.01, token.set)
        timer.start()
        error = RecoveryCancelled
    started = time.monotonic()
    try:
        with pytest.raises(error):
            if structure == 'classic':
                parse_moov(data, moov, lambda: _check(token))
            else:
                fragment_samples(data, boxes, tracks, defaults, lambda: _check(token))
        assert time.monotonic() - started < 2
    finally:
        if timer:
            timer.cancel()
            timer.join()


def test_fragment_entry_extent_is_rejected_before_declared_work(mp4_media):
    data = bytearray(mp4_media['fragmented'].read_bytes())
    boxes, _, _ = discover(data)
    moov = next(b for b in boxes if b.kind == b'moov')
    tracks, defaults, _ = parse_moov(data, moov)
    moof = [b for b in boxes if b.kind == b'moof'][2]
    traf = next(b for b in children(data, moof.payload, moof.end) if b.kind == b'traf')
    run = child(data, traf, b'trun')
    struct.pack_into('>I', data, run.payload + 4, MAX_TABLE)
    errors = fragment_samples(data, boxes, tracks, defaults)
    assert any('invalid trun extent' in e for e in errors)
    assert len(next(t for t in tracks if t.id == 1).samples) == 75


@pytest.mark.parametrize('relative', [-3, -1, 0])
def test_box_marker_at_scan_window_boundary_is_not_lost_or_duplicated(relative):
    data = bytes(1024 * 1024 + relative) + b'ftyp' + bytes(1024 * 1024)
    calls = []
    matches = list(scanned_matches(re.compile(b'ftyp'), data, 0, len(data), lambda: calls.append(1)))
    assert [m.start() for m in matches] == [1024 * 1024 + relative]
    assert len(calls) >= 2
