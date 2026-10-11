"""Real decoder tests: mutate actual AVI bytes, verify survivors and provenance."""
import csv
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import pytest
from core.avi_recovery import recover_avi, RecoveryCancelled, _headers, _video_spec, _packets, _media_bounds, _jpeg_size


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope='module')
def media(tmp_path_factory):
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg required for real decode verification')
    root = tmp_path_factory.mktemp('recovery-media')
    result = {}
    for codec, extra in [('mjpeg', ['-q:v', '3']), ('libx264', ['-g', '10', '-bf', '0'])]:
        p = root/(codec+'.avi')
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=320x180:rate=10', '-t', '6', '-c:v', codec, *extra, '-an', str(p)], check=True)
        result[codec] = p
    return result


def packets(data):
    specs = {i: s for i, (h, f) in enumerate(_headers(data)) if (s := _video_spec(h, f))}
    return list(_packets(data, specs, None, _media_bounds(data)))


@pytest.fixture(scope='module')
def media_long(tmp_path_factory):
    """같은 기기의 더 긴 옛 녹화. 프레임 크기가 현재 녹화와 다르도록 화질을 다르게 둔다."""
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg required for real decode verification')
    root = tmp_path_factory.mktemp('recovery-media-long')
    result = {}
    for codec, extra in [('mjpeg', ['-q:v', '8']), ('libx264', ['-g', '10', '-bf', '0', '-crf', '35'])]:
        p = root/(codec+'.avi')
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=320x180:rate=10', '-t', '30', '-c:v', codec, *extra, '-an', str(p)], check=True)
        result[codec] = p
    return result


def rebuild(original, items):
    """원본 hdrl 뒤에 items대로 movi/idx1을 다시 쓴다.

    item: (tag, payload) 청크 | (b'rec ', [items]) LIST rec 묶음 | (b'pad', n) n바이트 0 패딩.
    """
    movi = original.find(b'movi')
    raw = bytearray(original[:movi+4])
    entries = bytearray()
    def emit(items):
        for tag, payload in items:
            if tag == b'pad':
                raw.extend(bytes(payload))
            elif tag == b'rec ':
                start, entry = len(raw), len(entries)
                raw.extend(b'LIST\0\0\0\0rec ')
                entries.extend(struct.pack('<4sIII', b'rec ', 1, start-movi, 0))
                emit(payload)
                struct.pack_into('<I', raw, start+4, len(raw)-start-8)
                struct.pack_into('<I', entries, entry+12, len(raw)-start-8)
            else:
                flags = 0x10 if tag.endswith(b'dc') and payload[:4] != b'\0\0\0\1' else 0
                entries.extend(struct.pack('<4sIII', tag, flags, len(raw)-movi, len(payload)))
                raw.extend(tag+struct.pack('<I', len(payload))+payload+(b'\0' if len(payload) & 1 else b''))
    emit(items)
    struct.pack_into('<I', raw, movi-4, len(raw)-movi)
    raw.extend(b'idx1'+struct.pack('<I', len(entries))+entries)
    struct.pack_into('<I', raw, 4, len(raw)-8)
    return bytes(raw)


def nal_units(data):
    """Annex-B 패킷을 (type, bytes) NAL 단위로 나눈다(시작 코드 포함)."""
    import re
    starts = [m.start() for m in re.finditer(b'\x00\x00\x01', data)]
    units = []
    for n, a in enumerate(starts):
        b = starts[n+1] if n+1 < len(starts) else len(data)
        units.append((data[a+3] & 31, data[a:b]))
    return units


def mutate(path, kind, target):
    raw = bytearray(path.read_bytes())
    found = packets(raw)
    assert len(found) == 60
    movi = raw.find(b'movi')
    idx = raw.rfind(b'idx1')
    if kind in ('signature', 'combined'):
        raw[:12] = b'\0'*12
    if kind in ('sizes', 'combined'):
        raw[4:8] = b'\xff'*4
        raw[movi-4:movi] = b'\xff'*4
    if kind in ('index', 'combined'):
        raw[idx:] = b'\0'*(len(raw)-idx)
    if kind in ('middle', 'combined'):
        start, stop = found[20][1]-8, found[35][1]-8
        raw[start:stop] = b'\0'*(stop-start)
        # Another impossible length. Must still find the following chunk.
        off = found[40][1]-4
        raw[off:off+4] = b'\xff'*4
    if kind == 'header_and_middle':
        raw[:movi+4] = b'\0'*(movi+4)
        start, stop = found[20][1]-8, found[35][1]-8
        raw[start:stop] = b'\0'*(stop-start)
        raw[idx:] = b'\0'*(len(raw)-idx)
    if kind == 'truncated':
        raw = raw[:found[45][1]+len(found[45][2])//2]
    target.write_bytes(raw)
    return found


@pytest.mark.parametrize('kind,expected', [('signature',60), ('sizes',60), ('index',60),
    ('middle',45), ('combined',45), ('header_and_middle',45), ('truncated',45)])
def test_mjpeg_severe_damage_decodes_survivors(media, tmp_path, kind, expected):
    src = tmp_path/'damaged.avi'
    original = mutate(media['mjpeg'], kind, src)
    before = sha(src)
    out = tmp_path/'result'
    result = recover_avi(src, out, media['mjpeg'] if kind == 'header_and_middle' else None)
    if kind in ('combined', 'header_and_middle'):
        # No trustworthy movi size or idx1 survives: a reference cannot prove
        # where this recording ends. Keep isolated stills, never stitch slack.
        assert result['videos'] == []
        assert result['jpeg_stills'] == expected
        assert result['jpeg_scope'] == 'whole_file_untrusted'
        assert result['video_boundary_verified'] is False
        assert result['video_withheld_reason']
        assert sha(src) == before == result['source_sha256'] == sha(out/'damaged_source.avi')
        return
    assert result['status'] == 'video_recovered'
    video = result['videos'][0]
    assert video['candidate_frames'] == expected
    assert video['decode_check']['status'] == 'passed'
    assert video['decode_check']['decoded_frames'] == expected
    assert sha(src) == before == result['source_sha256'] == sha(out/'damaged_source.avi')
    assert not result['original_timeline_preserved']
    assert sha(out/video['file']) == video['sha256']
    original_hashes = {hashlib.sha256(data).hexdigest() for _, _, data in original}
    recovered = packets((out/video['file']).read_bytes())
    assert len(recovered) == expected
    assert all(hashlib.sha256(data).hexdigest() in original_hashes for _, _, data in recovered)
    offsets = list(csv.DictReader((out/'frame_offsets.csv').open(encoding='utf-8-sig')))
    assert len(offsets) == expected
    assert max(int(row['source_payload_offset']) for row in offsets) >= original[44][1]
    assert json.loads((out/'recovery.json').read_text(encoding='utf-8'))['status'] == 'video_recovered'


@pytest.mark.parametrize('kind', ['signature', 'index', 'sizes'])
def test_h264_headers_and_index(media, tmp_path, kind):
    source = tmp_path/'h264.avi'
    mutate(media['libx264'], kind, source)
    result = recover_avi(source, tmp_path/'out')
    assert result['videos'][0]['decode_check']['status'] == 'passed'
    assert result['videos'][0]['decode_check']['decoded_frames'] == 60


def test_headerless_without_reference_only_stills(media, tmp_path):
    source = tmp_path/'broken.avi'
    mutate(media['mjpeg'], 'header_and_middle', source)
    result = recover_avi(source, tmp_path/'out')
    assert result['videos'] == []
    assert result['jpeg_stills'] == 45
    assert result['status'] == 'metadata_or_stills_only'


def nmea(body):
    checksum = 0
    for ch in body.encode(): checksum ^= ch
    return f'${body}*{checksum:02X}\r\n'.encode()


def test_gps_survives_complete_container_loss(tmp_path):
    good = nmea('GPRMC,010203.00,A,3700.000,N,12700.000,E,10,0,061026,,,A')
    bad_zero = nmea('GPRMC,010203.00,A,0000.000,N,00000.000,E,10,0,061026,,,A')
    bad_date = nmea('GPRMC,990203.00,A,3700.000,N,12700.000,E,10,0,991326,,,A')
    source = tmp_path/'metadata.avi'
    source.write_bytes(b'\0'*2000+bad_zero+bad_date+good+b'\0'*300000+good.replace(b'*', b'*00'))
    result = recover_avi(source, tmp_path/'out')
    assert result['gps_records'] == 1
    rows = list(csv.DictReader((tmp_path/'out/recovered_gps.csv').open(encoding='utf-8-sig')))
    assert rows[0]['latitude'] == '37.0'
    assert rows[0]['scope'] == 'whole_file_untrusted'
    assert result['videos'] == []


def test_trailing_slack_excluded_from_video(media, tmp_path):
    source = tmp_path/'slack.avi'
    source.write_bytes(media['mjpeg'].read_bytes()*2)
    result = recover_avi(source, tmp_path/'out')
    assert result['videos'][0]['candidate_frames'] == 60
    assert result['video_scan_range']['basis'] == 'movi'


def test_wrong_reference_dimensions_not_accepted(media, tmp_path):
    source = tmp_path/'broken.avi'
    mutate(media['mjpeg'], 'header_and_middle', source)
    raw = bytearray(source.read_bytes())
    original = media['mjpeg'].read_bytes()
    idx = original.rfind(b'idx1')
    raw[idx:] = original[idx:]
    source.write_bytes(raw)
    ref = bytearray(media['mjpeg'].read_bytes())
    off = ref.index(b'strf')+8+4
    struct.pack_into('<i', ref, off, 640)
    reference = tmp_path/'reference.avi'
    reference.write_bytes(ref)
    result = recover_avi(source, tmp_path/'out', reference)
    assert result['video_boundary_verified']
    assert not result['videos']
    assert result['jpeg_stills'] == 45


def test_cancel_leaves_no_partial_output(media, tmp_path):
    event = threading.Event()
    def progress(_): event.set()
    with pytest.raises(RecoveryCancelled):
        recover_avi(media['mjpeg'], tmp_path/'out', cancel=event, progress=progress)
    assert list(tmp_path.iterdir()) == []


def test_existing_output_and_mp4_rejected(media, tmp_path):
    out = tmp_path/'existing'
    out.mkdir()
    (out/'keep').write_text('unchanged', encoding='utf-8')
    with pytest.raises(FileExistsError): recover_avi(media['mjpeg'], out)
    assert (out/'keep').read_text(encoding='utf-8') == 'unchanged'
    mp4 = tmp_path/'not_supported.mp4'
    mp4.write_bytes(b'a'*32)
    with pytest.raises(ValueError, match='AVI 전용'): recover_avi(mp4, tmp_path/'new')


def test_garbage_not_success(tmp_path):
    source = tmp_path/'garbage.avi'
    source.write_bytes(b'\0'*1000000+b'00dc'+b'\xff'*4)
    result = recover_avi(source, tmp_path/'out')
    assert result['status'] == 'nothing_recovered'
    assert not result['videos'] and not result['gps_records']


def test_missing_decoder_never_claims_verified(media, tmp_path, monkeypatch):
    monkeypatch.setattr('core.avi_recovery.shutil.which', lambda _: None)
    result = recover_avi(media['mjpeg'], tmp_path/'out')
    assert result['status'] == 'video_candidates_unverified'
    assert result['videos'][0]['decode_check']['status'] == 'not_run'


def test_eighty_percent_frames_and_all_headers_destroyed(media, tmp_path):
    raw = bytearray(media['mjpeg'].read_bytes())
    frames = packets(raw)
    idx = raw.rfind(b'idx1')
    # No container/stream/movi/chunk/index headers survive for the first 48 frames.
    raw[:frames[48][1]] = b'\0'*frames[48][1]
    raw[idx:] = b'\0'*(len(raw)-idx)
    source = tmp_path/'severely_damaged.avi'
    source.write_bytes(raw)
    result = recover_avi(source, tmp_path/'out', media['mjpeg'])
    assert result['videos'] == []
    assert result['jpeg_stills'] == 12
    assert result['video_boundary_verified'] is False
    assert result['jpeg_scope'] == 'whole_file_untrusted'


def test_all_chunk_headers_destroyed_body_survives(media, tmp_path):
    raw = bytearray(media['mjpeg'].read_bytes())
    for _, offset, _ in packets(raw):
        raw[offset-8:offset] = b'\0'*8
    source = tmp_path/'headers_destroyed.avi'
    source.write_bytes(raw)
    result = recover_avi(source, tmp_path/'out')
    assert result['videos'][0]['decode_check']['decoded_frames'] == 60
    assert result['videos'][0]['decode_check']['status'] == 'passed'


def test_source_change_discards_result(media, tmp_path):
    source = tmp_path/'changing.avi'
    shutil.copyfile(media['mjpeg'], source)
    def progress(message):
        if '청크' in message:
            with source.open('ab') as f: f.write(b'changed')
    with pytest.raises(ValueError, match='원본이 변경'):
        recover_avi(source, tmp_path/'out', progress=progress)
    assert not (tmp_path/'out').exists()
    assert not list(tmp_path.glob('.avi-recovery-*'))


def test_undersized_movi_does_not_hide_later_frames(media, tmp_path):
    raw = bytearray(media['mjpeg'].read_bytes())
    struct.pack_into('<I', raw, raw.find(b'movi')-4, 4)
    source = tmp_path/'wrong_size.avi'
    source.write_bytes(raw)
    result = recover_avi(source, tmp_path/'out')
    assert result['videos'][0]['decode_check']['decoded_frames'] == 60
    assert result['video_scan_range']['basis'] == 'idx1_validated'


def test_zero_exit_with_decoder_errors_is_not_success(tmp_path, monkeypatch):
    from core.avi_recovery import _decode_check
    monkeypatch.setattr('core.avi_recovery.shutil.which', lambda _: 'ffmpeg')
    class DecoderWithError:
        returncode = 0
        def __init__(self, args, stdout, stderr):
            stdout.write(b'frame=562\nprogress=end\n')
            stderr.write(b'Error processing packet in decoder: Invalid data found\n')
        def poll(self):
            return self.returncode
    monkeypatch.setattr('core.avi_recovery.subprocess.Popen', DecoderWithError)
    result = _decode_check(tmp_path/'candidate.avi', None)
    assert result['status'] == 'failed'
    assert result['decoded_frames'] == 562


def test_missing_decoded_frames_is_not_full_recovery(media, tmp_path, monkeypatch):
    monkeypatch.setattr('core.avi_recovery._decode_check',
        lambda *args: {'status': 'passed', 'decoded_frames': 53, 'error': ''})
    result = recover_avi(media['mjpeg'], tmp_path/'out')
    assert result['videos'][0]['candidate_frames'] == 60
    assert result['videos'][0]['decode_check']['status'] == 'incomplete'
    assert result['status'] == 'video_candidates_unverified'


def test_cli_help_with_legacy_windows_output_encoding():
    import os
    import sys
    env = dict(os.environ, PYTHONIOENCODING='cp1252', PYTHONUTF8='0')
    result = subprocess.run(
        [sys.executable, '-m', 'core.avi_recovery', '--help'],
        cwd=Path(__file__).resolve().parents[1], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    assert '손상 AVI' in result.stdout.decode('utf-8')


# ---- #31 청크 경계와 어긋난 덮어쓰기: 앞 프레임 머리+뒤 프레임 꼬리 합성 프레임을 받지 않는다.
@pytest.mark.parametrize('kind', ['straddle', 'tail_zeroed', 'header_lost_stills'])
def test_overwrite_across_chunk_boundaries_never_yields_composite_frame(media, tmp_path, kind):
    raw = bytearray(media['mjpeg'].read_bytes())
    found = packets(raw)
    original = {hashlib.sha256(data).hexdigest(): n for n, (_, _, data) in enumerate(found)}
    sizes = {found[n][1]: len(found[n][2]) for n in range(60)}
    if kind == 'tail_zeroed':
        start, stop = found[20][1]+len(found[20][2])//2, found[20][1]+len(found[20][2])-1
        expected = [n for n in range(60) if n != 20]
    else:
        start, stop = found[20][1]+len(found[20][2])//2, found[35][1]+len(found[35][2])//2
        expected = [n for n in range(60) if not 20 <= n <= 35]
    raw[start:stop] = bytes(stop-start)
    if kind == 'header_lost_stills':
        movi = raw.find(b'movi')
        raw[:movi+4] = bytes(movi+4)
    source = tmp_path/'straddle.avi'
    source.write_bytes(raw)
    out = tmp_path/'out'
    result = recover_avi(source, out)
    if kind == 'header_lost_stills':
        assert result['videos'] == []
        rows = list(csv.DictReader((out/'jpeg_offsets.csv').open(encoding='utf-8-sig')))
        recovered = [original.get(hashlib.sha256((out/'jpeg_stills'/r['file']).read_bytes()).hexdigest())
                     for r in rows]
        assert recovered == expected
        assert result['jpeg_stills'] == len(expected)
        return
    video = result['videos'][0]
    assert video['decode_check']['status'] == 'passed'
    assert video['candidate_frames'] == video['decode_check']['decoded_frames'] == len(expected)
    recovered = [original.get(hashlib.sha256(data).hexdigest()) for _, _, data in packets((out/video['file']).read_bytes())]
    assert recovered == expected
    rows = list(csv.DictReader((out/'frame_offsets.csv').open(encoding='utf-8-sig')))
    # 기록된 원본 위치·크기도 실제 프레임과 정확히 일치해야 한다(합성 프레임은 크기가 다르다).
    assert [(int(r['source_payload_offset']), int(r['source_payload_size'])) for r in rows] == \
        [(found[n][1], sizes[found[n][1]]) for n in expected]


def test_jpeg_structure_scan_rejects_composites_and_keeps_real_frames(media):
    found = packets(media['mjpeg'].read_bytes())
    a, b = found[20][2], found[35][2]
    assert all(_jpeg_size(data) == (320, 180) for _, _, data in found)
    half = len(a)//2
    assert _jpeg_size(a[:half]+bytes(len(a)-half)+b[len(b)//2:]) is None  # 긴 0 구간
    assert _jpeg_size(a[:half]+b) is None                                   # 두 번째 SOI
    assert _jpeg_size(a[:half]+b'00dc'+struct.pack('<I', len(b))+b[4:]) is None  # 청크 헤더
    assert _jpeg_size(a[:half]+bytes(range(256))*4+a[half:]) is None       # 허용되지 않는 마커
    assert _jpeg_size(a[:-2]+b'\0\xd9') is None
    assert _jpeg_size(a[:a.index(b'\xff\xda')]+b'\xff\xd9') is None          # SOS 없이 끝남


# ---- #86 헤더(스펙)가 없는 후방 MJPEG 청크가 전방 스트림에 섞이면 안 된다.
@pytest.mark.parametrize('rear', ['intact_headers', 'size_damaged'])
def test_undeclared_rear_stream_chunks_not_mixed_into_front(media, tmp_path, rear):
    original = media['mjpeg'].read_bytes()
    front = [data for _, _, data in packets(original)]
    # 후방: 같은 해상도·코덱의 다른 그림(역순). hdrl에는 전방 스트림 하나만 선언돼 있다.
    items = []
    for n, data in enumerate(front):
        items.append((b'00dc', data))
        items.append((b'01dc', front[59-n]))
    raw = bytearray(rebuild(original, items))
    rows = [struct.unpack_from('<4sIII', raw, raw.rfind(b'idx1')+8+i*16) for i in range(120)]
    movi = raw.find(b'movi')
    front_offsets = [movi+off+8 for tag, _, off, _ in rows if tag == b'00dc']
    if rear == 'size_damaged':
        # 후방 청크 일부의 크기 필드가 깨져 통째로 건너뛸 수 없어도, 바로 앞의 '01dc' 헤더가
        # 그 JPEG가 후방 것임을 말해 준다.
        for n, (tag, _, off, _) in enumerate(rows):
            if tag == b'01dc' and n % 8 == 1:
                struct.pack_into('<I', raw, movi+off+4, 0xFFFFFFFF)
    source = tmp_path/'two_channel.avi'
    source.write_bytes(raw)
    out = tmp_path/'out'
    result = recover_avi(source, out)
    video = result['videos'][0]
    assert result['video_boundary_verified']
    assert video['decode_check']['status'] == 'passed'
    assert video['candidate_frames'] == video['decode_check']['decoded_frames'] == 60
    offsets = [int(r['source_payload_offset']) for r in csv.DictReader((out/'frame_offsets.csv').open(encoding='utf-8-sig'))]
    assert offsets == front_offsets
    recovered = [data for _, _, data in packets((out/video['file']).read_bytes())]
    assert recovered == front


# ---- #128 손상본에 남은 스트림 헤더가 참조 헤더보다 우선한다.
@pytest.mark.parametrize('reference_kind', ['same', 'triple_fps'])
def test_reference_does_not_override_surviving_source_headers(media, tmp_path, reference_kind):
    source = tmp_path/'index_lost.avi'
    mutate(media['mjpeg'], 'index', source)
    ref = bytearray(media['mjpeg'].read_bytes())
    strh = ref.index(b'strh')+8
    scale, rate = struct.unpack_from('<II', ref, strh+20)
    if reference_kind == 'triple_fps':
        struct.pack_into('<II', ref, strh+20, scale, rate*3)
    reference = tmp_path/'reference.avi'
    reference.write_bytes(ref)
    out = tmp_path/'out'
    result = recover_avi(source, out, reference)
    video = result['videos'][0]
    assert video['decode_check']['status'] == 'passed' and video['candidate_frames'] == 60
    header, _ = _headers((out/video['file']).read_bytes())[0]
    assert struct.unpack_from('<II', header, 20) == (scale, rate)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'stream=r_frame_rate', '-of', 'csv=p=0', str(out/video['file'])], capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == '10/1'
    assert result['reference']['usage'] == 'not_used_source_headers_intact'
    assert result['reference']['sha256'] == sha(reference)
    if reference_kind == 'triple_fps':
        assert '30fps' in result['reference']['warning'] and '10fps' in result['reference']['warning']
    else:
        assert 'warning' not in result['reference']
