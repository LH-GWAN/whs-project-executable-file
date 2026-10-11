"""Verify independent recovery after zero-filled H.264 prediction loss."""
import csv
import struct
import subprocess
import pytest
from core.avi_recovery import recover_avi, _intact_gap
from test_avi_recovery import media, packets


def pixels(path):
    result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror',
        '-i', str(path), '-map', '0:v:0', '-f', 'framemd5', '-'],
        capture_output=True, check=True, timeout=30)
    assert not result.stderr
    return [line.split(',')[-1].strip() for line in result.stdout.decode().splitlines()
            if line and not line.startswith('#')]


@pytest.mark.parametrize('damage', ['partial_payload', 'whole_chunk', 'multiple_chunks'])
def test_resume_at_idr_with_exact_original_pixels(media, tmp_path, damage):
    source = media['libx264']
    raw = bytearray(source.read_bytes())
    frames = packets(raw)
    if damage == 'partial_payload':
        start = frames[22][1]+len(frames[22][2])//2
        end = frames[22][1]+len(frames[22][2])
        first_lost, resume = 22, 30
    else:
        first_lost, resume = (22, 30) if damage == 'whole_chunk' else (20, 40)
        start = frames[first_lost][1]-8
        end = frames[23 if damage == 'whole_chunk' else 35][1]-8
    raw[start:end] = bytes(end-start)
    damaged = tmp_path/'damaged.avi'; damaged.write_bytes(raw)
    out = tmp_path/'out'
    result = recover_avi(damaged, out)
    video = result['videos'][0]
    original_pixels = pixels(source)
    expected = original_pixels[:first_lost]+original_pixels[resume:]
    assert video['decode_check']['status'] == 'passed'
    assert video['candidate_frames'] == video['decode_check']['decoded_frames'] == len(expected)
    assert pixels(out/video['file']) == expected
    assert video['h264_exclusions']['prediction_breaks'] > 0
    rows = list(csv.DictReader((out/'frame_offsets.csv').open(encoding='utf-8-sig')))
    offsets = [int(row['source_payload_offset']) for row in rows]
    assert offsets == [p for n, (_, p, _) in enumerate(frames) if n < first_lost or n >= resume]


def test_silent_audio_and_vendor_unpadded_metadata_are_not_prediction_loss():
    audio = b'01wb'+struct.pack('<I', 1024)+bytes(1024)
    odd = b'02st'+struct.pack('<I', 3)+b'GPS'
    for gap in (audio, odd+audio, odd+b'\0'+audio, b''):
        assert _intact_gap(gap, 0, len(gap), 0)
    assert not _intact_gap(bytes(1024), 0, 1024, 0)
