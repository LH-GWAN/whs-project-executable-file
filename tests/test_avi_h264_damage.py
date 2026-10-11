"""Verify independent recovery after zero-filled H.264 prediction loss."""
import csv
import struct
import subprocess
import pytest
from core.avi_recovery import recover_avi, _intact_gap
from test_avi_recovery import media, packets, rebuild, nal_units


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


# ---- #87 #88 정상 구조(길이 0 청크, LIST rec, ix##, JUNK, 다바이트 패딩)는 손상이 아니다.
@pytest.mark.parametrize('codec', ['mjpeg', 'libx264'])
@pytest.mark.parametrize('damage', ['intact', 'header_lost'])
def test_normal_avi_structures_are_not_damage(media, tmp_path, codec, damage):
    original = media[codec].read_bytes()
    frames = [data for _, _, data in packets(original)]
    items = []
    for n, data in enumerate(frames):
        group = [(b'00dc', data), (b'01wb', bytes(512))]
        if n % 7 == 3:
            group.insert(0, (b'00dc', b''))            # 드롭 프레임 표식(길이 0 영상 청크)
        if n % 5 == 0:
            group.append((b'pad', 3))                  # 다바이트 정렬 패딩
        if n % 9 == 4:
            group.append((b'02st', b'$GPRMC,0*00'))     # 홀수 길이 텍스트 청크
        items.append((b'rec ', group))
        if n % 10 == 9:
            items.append((b'ix00', bytes(64)))           # OpenDML 표준 색인 청크
        if n % 15 == 7:
            items.append((b'JUNK', bytes(2048)))
    raw = bytearray(rebuild(original, items))
    if damage == 'header_lost':
        movi = raw.find(b'movi')
        raw[:movi+4] = bytes(movi+4)
    source = tmp_path/'rec_grouped.avi'
    source.write_bytes(raw)
    out = tmp_path/'out'
    result = recover_avi(source, out, media[codec] if damage == 'header_lost' else None)
    assert result['video_boundary_verified']
    video = result['videos'][0]
    assert video['decode_check']['status'] == 'passed'
    assert video['candidate_frames'] == video['decode_check']['decoded_frames'] == 60
    assert pixels(out/video['file']) == pixels(media[codec])
    if codec == 'libx264':
        assert video['h264_exclusions'] == {'zero_filled_packets': 0, 'waiting_for_idr_packets': 0,
                                            'prediction_breaks': 0}


def test_intact_gap_accepts_normal_structures_and_rejects_damage():
    audio = b'01wb'+struct.pack('<I', 1024)+bytes(1024)
    drop = b'00dc'+struct.pack('<I', 0)
    rec = b'LIST'+struct.pack('<I', 4+len(audio))+b'rec '
    ix = b'ix00'+struct.pack('<I', 32)+bytes(32)
    junk = b'JUNK'+struct.pack('<I', 7)+bytes(7)+b'\0'
    for gap in (drop, rec+audio, ix+audio, junk, bytes(3)+audio+bytes(31), drop+audio+ix+rec):
        assert _intact_gap(gap, 0, len(gap), 0), gap[:12]
    rejected = b'00dc'+struct.pack('<I', 16)+bytes(16)          # 거부된 영상 청크(길이>0)
    for gap in (rejected, bytes(32)+audio, audio+bytes(40), b'00dc'+struct.pack('<I', 9999), b'\x81'*64):
        assert not _intact_gap(gap, 0, len(gap), 0), gap[:12]


# ---- #127 녹화 중간의 SPS/PPS 전용 청크는 프레임이 아니라 다음 패킷 앞에 붙인다.
@pytest.mark.parametrize('damage', ['intact', 'middle'])
def test_parameter_set_chunks_between_frames_attach_to_next_packet(media, tmp_path, damage):
    original = media['libx264'].read_bytes()
    frames = [data for _, _, data in packets(original)]
    config = b''.join(nal for t, nal in nal_units(frames[0]) if t in (7, 8))
    assert config
    items = []
    for n, data in enumerate(frames):
        if n % 10 == 0:
            items.append((b'00dc', config))
            data = b''.join(nal for t, nal in nal_units(data) if t not in (7, 8))
        items.append((b'00dc', data))
    raw = bytearray(rebuild(original, items))
    rows = [struct.unpack_from('<4sIII', raw, raw.rfind(b'idx1')+8+i*16) for i in range(66)]
    movi = raw.find(b'movi')
    expected = list(range(60))
    if damage == 'middle':
        # 21번 프레임 청크(엔트리 24)를 지우면 다음 IDR(30) 앞의 파라미터 청크부터 다시 시작해야 한다.
        a, b = movi+rows[24][2], movi+rows[25][2]
        raw[a:b] = bytes(b-a)
        expected = [n for n in range(60) if not 21 <= n < 30]
    source = tmp_path/'parameter_chunks.avi'
    source.write_bytes(raw)
    out = tmp_path/'out'
    result = recover_avi(source, out)
    video = result['videos'][0]
    assert video['decode_check']['status'] == 'passed'
    assert video['candidate_frames'] == video['decode_check']['decoded_frames'] == len(expected)
    original_pixels = pixels(media['libx264'])
    assert pixels(out/video['file']) == [original_pixels[n] for n in expected]
    offsets = [int(r['source_payload_offset']) for r in csv.DictReader((out/'frame_offsets.csv').open(encoding='utf-8-sig'))]
    picture_offsets = [movi+off+8 for n, (tag, _, off, _) in enumerate(rows) if n % 11 != 0]
    assert offsets == [picture_offsets[n] for n in expected]
