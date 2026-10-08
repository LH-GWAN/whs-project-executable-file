"""Old video/header fragments may precede the current idx1 in allocated AVI."""
import csv
import struct
import pytest
from core.avi_recovery import recover_avi, _index_boundary, _media_bounds
from test_avi_recovery import media, packets
from test_avi_h264_damage import pixels


@pytest.mark.parametrize('codec', ['mjpeg', 'libx264'])
@pytest.mark.parametrize('damage', ['intact', 'header', 'cut', 'middle', 'header_middle'])
def test_current_index_after_embedded_old_header(media, tmp_path, codec, damage):
    original = media[codec].read_bytes()
    frames = packets(original)
    movi, idx = original.find(b'movi'), original.rfind(b'idx1')
    # Unallocated remainder starts in the middle of stale payload, followed
    # by intact old chunks and another old AVI header before the current idx1.
    old_header = bytearray(original[:idx])
    # The embedded header is a partial old allocated file, not a complete
    # second AVI owning the current recording's index.
    struct.pack_into('<I', old_header, 4, 0x10000000)
    stale = b'\x81'*137+original[movi+4:idx]+old_header
    raw = bytearray(original[:idx]+stale+original[idx:])
    new_index = idx+len(stale)
    struct.pack_into('<I', raw, movi-4, new_index-movi)
    struct.pack_into('<I', raw, 4, len(raw)-8)
    if 'header' in damage:
        raw[:movi+4] = bytes(movi+4)
    if 'middle' in damage:
        a,b = frames[20][1]-8, frames[35][1]-8
        raw[a:b] = bytes(b-a)
    if damage == 'cut':
        raw = raw[:new_index]
    source = tmp_path/'damaged.avi'; source.write_bytes(raw)
    out = tmp_path/'out'
    result = recover_avi(source, out, media[codec] if 'header' in damage else None)
    assert result['video_scan_range']['end'] == idx
    expected = [n for n in range(60) if 'middle' not in damage
                or not 20 <= n < (40 if codec == 'libx264' else 35)]
    rows = list(csv.DictReader((out/'frame_offsets.csv').open(encoding='utf-8-sig')))
    assert [int(r['source_payload_offset']) for r in rows] == [frames[n][1] for n in expected]
    v = result['videos'][0]
    assert v['decode_check']['status'] == 'passed'
    assert v['decode_check']['decoded_frames'] == len(expected)
    original_pixels = pixels(media[codec])
    assert pixels(out/v['file']) == [original_pixels[n] for n in expected]


def test_long_recording_header_loss_uses_early_index_anchors(media, monkeypatch):
    raw = bytearray(media['mjpeg'].read_bytes())
    frames = packets(raw)
    idx = raw.rfind(b'idx1')
    # First dispersed probe was erased; subsequent dispersed probes are past
    # the anchor search window. A nearby early index entry still survives.
    monkeypatch.setattr('core.avi_recovery.MAX_HEADER', frames[2][1]-8)
    raw[:frames[1][1]-8] = bytes(frames[1][1]-8)
    assert _index_boundary(raw) == idx


def test_foreign_index_cannot_bound_headerless_first_recording(media):
    original = media['mjpeg'].read_bytes()
    raw = bytearray(original)
    movi,idx = raw.find(b'movi'),raw.rfind(b'idx1')
    raw[:movi+4] = bytes(movi+4)
    raw[idx:] = bytes(len(raw)-idx)
    raw.extend(original)
    assert _media_bounds(raw)[2] == 'whole_file_untrusted'


@pytest.mark.parametrize('codec', ['mjpeg', 'libx264'])
def test_unpadded_adjacent_video_chunks_keep_every_frame(media, tmp_path, codec):
    original = media[codec].read_bytes()
    frames = packets(original)
    movi = original.find(b'movi')
    raw = bytearray(original[:movi+4])
    index = bytearray()
    assert any(len(data) & 1 for _,_,data in frames)
    for _,_,data in frames:
        index.extend(struct.pack('<4sIII', b'00dc', 16, len(raw)-movi, len(data)))
        raw.extend(b'00dc'+struct.pack('<I', len(data))+data)
    struct.pack_into('<I', raw, movi-4, len(raw)-movi)
    raw.extend(b'idx1'+struct.pack('<I', len(index))+index)
    struct.pack_into('<I', raw, 4, len(raw)-8)
    assert len(packets(raw)) == 60
    source = tmp_path/'unpadded.avi'; source.write_bytes(raw)
    out = tmp_path/'out'
    result = recover_avi(source, out)
    v = result['videos'][0]
    assert v['candidate_frames'] == v['decode_check']['decoded_frames'] == 60
    assert v['decode_check']['status'] == 'passed'
    assert pixels(out/v['file']) == pixels(media[codec])
