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
from core.avi_recovery import recover_avi, RecoveryCancelled, _headers, _video_spec, _packets, _media_bounds


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
    assert json.loads((out/'recovery.json').read_text())['status'] == 'video_recovered'


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
    ref = bytearray(media['mjpeg'].read_bytes())
    off = ref.index(b'strf')+8+4
    struct.pack_into('<i', ref, off, 640)
    reference = tmp_path/'reference.avi'
    reference.write_bytes(ref)
    result = recover_avi(source, tmp_path/'out', reference)
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
    (out/'keep').write_text('unchanged')
    with pytest.raises(FileExistsError): recover_avi(media['mjpeg'], out)
    assert (out/'keep').read_text() == 'unchanged'
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
    assert result['videos'][0]['candidate_frames'] == 12
    assert result['videos'][0]['decode_check']['decoded_frames'] == 12
    assert result['videos'][0]['decode_check']['status'] == 'passed'


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
    assert 'untrusted' in result['video_scan_range']['basis']


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
