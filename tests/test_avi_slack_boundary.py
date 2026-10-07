"""Do not stitch surviving trailing recordings into a damaged AVI."""
import csv
import struct
import pytest
from core.avi_recovery import recover_avi, _index_boundary, _media_bounds
from test_avi_recovery import media, packets, nmea, sha


@pytest.mark.parametrize('codec', ['mjpeg', 'libx264'])
@pytest.mark.parametrize('damage', ['header', 'header_middle', 'oversized_movi'])
def test_index_bounds_damaged_video_before_decodable_slack(media, tmp_path, codec, damage):
    reference = media[codec]
    raw = bytearray(reference.read_bytes())
    frames = packets(raw)
    idx = raw.rfind(b'idx1')
    movi = raw.find(b'movi')
    if damage.startswith('header'):
        raw[:movi+4] = bytes(movi+4)
    else:
        struct.pack_into('<I', raw, movi-4, 0xFFFFFFFF)
    if damage == 'header_middle':
        a, b = frames[20][1]-8, frames[35][1]-8
        raw[a:b] = bytes(b-a)
    # Valid frames from another recording, both loose chunks and full AVI.
    raw.extend(reference.read_bytes()[movi+4:idx])
    gps_offset = len(raw)
    raw.extend(nmea('GPRMC,010203.00,A,3700.000,N,12700.000,E,10,0,061026,,,A'))
    raw.extend(reference.read_bytes())
    source = tmp_path/'damaged.avi'
    source.write_bytes(raw)
    before = sha(source)
    out = tmp_path/'out'
    result = recover_avi(source, out, reference)
    assert result['video_scan_range']['end'] == idx
    assert result['video_scan_range']['basis'] == 'idx1_validated'
    assert result['video_boundary_verified']
    rows = list(csv.DictReader((out/'frame_offsets.csv').open(encoding='utf-8-sig')))
    expected_offsets = {p for _, p, data in frames
                        if damage != 'header_middle' or not a <= p < b}
    assert {int(r['source_payload_offset']) for r in rows} == expected_offsets
    assert all(int(r['source_payload_offset'])+int(r['source_payload_size']) <= idx for r in rows)
    assert result['videos'][0]['candidate_frames'] == len(expected_offsets)
    # Middle H.264 loss may break prediction. Never require false decode success.
    if codec == 'mjpeg' or damage != 'header_middle':
        assert result['videos'][0]['decode_check']['status'] == 'passed'
        assert result['videos'][0]['decode_check']['decoded_frames'] == len(expected_offsets)
    gps = list(csv.DictReader((out/'recovered_gps.csv').open(encoding='utf-8-sig')))
    assert len(gps) == 1 and int(gps[0]['source_offset']) == gps_offset
    assert gps[0]['scope'] == 'whole_file_untrusted'
    assert sha(source) == before == result['source_sha256']


@pytest.mark.parametrize('codec', ['mjpeg', 'libx264'])
@pytest.mark.parametrize('slack', ['chunks', 'complete_avi'])
def test_no_boundary_reference_must_not_authorize_video(media, tmp_path, codec, slack):
    reference = media[codec]
    original = reference.read_bytes()
    movi, idx = original.find(b'movi'), original.rfind(b'idx1')
    raw = bytearray(original)
    raw[:movi+4] = bytes(movi+4)
    raw[idx:] = bytes(len(raw)-idx)
    raw.extend(original if slack == 'complete_avi' else original[movi+4:idx])
    source = tmp_path/'no_boundary.avi'
    source.write_bytes(raw)
    result = recover_avi(source, tmp_path/'out', reference)
    assert result['videos'] == []
    assert result['video_boundary_verified'] is False
    assert result['video_withheld_reason']
    assert result['jpeg_scope'] == 'whole_file_untrusted'
    assert not list((tmp_path/'out').glob('recovered_stream*.avi'))


@pytest.mark.parametrize('corruption', ['fake_marker', 'bad_offsets', 'oversized_index'])
def test_index_signature_alone_never_proves_boundary(media, corruption):
    raw = bytearray(media['mjpeg'].read_bytes())
    idx = raw.rfind(b'idx1')
    if corruption == 'fake_marker':
        fake = b'idx1'+struct.pack('<I', 48)+struct.pack('<4sIII', b'00dc', 16, 4, 123)*3
        raw[256:256+len(fake)] = fake
        assert _index_boundary(raw) == idx
    elif corruption == 'bad_offsets':
        for p in range(idx+8, len(raw), 16):
            struct.pack_into('<I', raw, p+8, 0xFFFFFFFF)
        assert _index_boundary(raw) is None
    else:
        struct.pack_into('<I', raw, idx+4, 0xFFFFFFF0)
        assert _index_boundary(raw) is None


def test_absolute_index_offsets_after_header_loss(media):
    raw = bytearray(media['mjpeg'].read_bytes())
    movi, idx = raw.find(b'movi'), raw.rfind(b'idx1')
    for p in range(idx+8, len(raw), 16):
        off = struct.unpack_from('<I', raw, p+8)[0]
        struct.pack_into('<I', raw, p+8, off+movi)
    raw[:movi+4] = bytes(movi+4)
    assert _media_bounds(raw) == (0, idx, 'idx1_validated')
