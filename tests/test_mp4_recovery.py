"""Real decode/pixel comparisons, severe damage and evidence-boundary regressions.

The independent oracle is FFmpeg's original packet map and decoded pixel hashes,
not the recovery parser. Small fixtures exercise real AVC/HEVC, audio and fMP4.
"""
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading

import pytest

from core.mp4_recovery import recover_mp4, RecoveryCancelled


def ffmpeg(*args):
    return subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', *map(str, args)],
                          capture_output=True, check=True, timeout=60)


def pixels(path):
    r = ffmpeg('-xerror', '-i', path, '-map', '0:v:0', '-f', 'framemd5', '-')
    assert not r.stderr
    return [line.rsplit(',', 1)[-1].strip() for line in r.stdout.decode().splitlines()
            if line and not line.startswith('#')]


def packet_map(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
                        '-show_entries', 'packet=pos,size,flags', '-of', 'json', str(path)],
                       capture_output=True, check=True, timeout=20)
    return json.loads(r.stdout)['packets']


def top_boxes(data):
    p, result = 0, []
    while p + 8 <= len(data):
        size, kind = struct.unpack_from('>I4s', data, p)
        if size == 1:
            size = struct.unpack_from('>Q', data, p + 8)[0]
        if not size:
            size = len(data) - p
        assert size >= 8
        result.append((kind, p, p + size)); p += size
    return result


@pytest.fixture(scope='module')
def mp4_media(tmp_path_factory):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/FFprobe required'
    root = tmp_path_factory.mktemp('mp4')
    result = {}
    for kind in ('avc', 'bframes', 'fragmented', 'hevc', 'wrong'):
        path = root / (kind + '.mp4')
        codec = 'libx265' if kind == 'hevc' else 'libx264'
        size = '128x96' if kind == 'wrong' else '160x90'
        extra = (['-x265-params', 'pools=1:frame-threads=1:keyint=15:min-keyint=15:scenecut=0:bframes=0:log-level=error']
                 if codec == 'libx265' else ['-g', '15', '-keyint_min', '15', '-sc_threshold', '0',
                                            '-bf', '2' if kind == 'bframes' else '0'])
        ffmpeg('-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=30', '-f', 'lavfi',
               '-i', 'sine=frequency=440:sample_rate=48000', '-t', '3', '-c:v', codec,
               '-threads', '1', '-pix_fmt', 'yuv420p', *extra, '-c:a', 'aac',
               *(['-movflags', 'frag_keyframe+empty_moov+default_base_moof'] if kind == 'fragmented' else []), path)
        result[kind] = path
    return result


def expected_indices(packets, damaged):
    indices, waiting = [], False
    for i, p in enumerate(packets):
        a, b = int(p['pos']), int(p['pos']) + int(p['size'])
        if any(a < y and x < b for x, y in damaged):
            waiting = True
            continue
        if 'K' in p['flags']:
            waiting = False
        if not waiting:
            indices.append(i)
    return indices


@pytest.mark.parametrize('damage', ['intact', 'ftyp_zero', 'front_4kb', 'middle_payload',
                                  'two_separate_gaps', 'most_video_zero', 'moov_cut',
                                  'partial_mdat_cut', 'moov_signature_zero', 'mdat_signature_zero',
                                  'mdat_size_huge', 'mdat_size_short', 'lost_sample_sizes'])
def test_classic_damage_has_exact_surviving_pixels(mp4_media, tmp_path, damage):
    source = mp4_media['avc']; data = bytearray(source.read_bytes()); packets = packet_map(source)
    original = pixels(source); damaged = []; reference = None
    moov = next(b for b in top_boxes(data) if b[0] == b'moov')
    mdat = next(b for b in top_boxes(data) if b[0] == b'mdat')
    if damage == 'ftyp_zero':
        data[:32] = bytes(32)
    elif damage == 'front_4kb':
        data[:4096] = bytes(4096); damaged = [(0, 4096)]
    elif damage in ('middle_payload', 'two_separate_gaps', 'most_video_zero'):
        intervals = [(22, 24)] if damage == 'middle_payload' else [(17, 20), (48, 52)] if damage == 'two_separate_gaps' else [(8, 70)]
        for first, last in intervals:
            a = int(packets[first]['pos']) + 15
            b = int(packets[last]['pos']) + int(packets[last]['size']) - 4
            data[a:b] = bytes(b-a); damaged.append((a, b))
    elif damage == 'moov_cut':
        data = data[:moov[1]]; reference = source
    elif damage == 'partial_mdat_cut':
        end = int(packets[65]['pos']) + int(packets[65]['size']) // 2
        damaged = [(end, len(data))]; data = data[:end]; reference = source
    elif damage == 'moov_signature_zero':
        data[moov[1]+4:moov[1]+8] = bytes(4); reference = source
    elif damage == 'mdat_signature_zero':
        data[mdat[1]+4:mdat[1]+8] = bytes(4)
    elif damage == 'mdat_size_huge':
        struct.pack_into('>I', data, mdat[1], 0xFFFFFFF0)
        # The oversized mdat would otherwise hide a surviving moov. Explicit
        # codec reference permits bounded raw recovery within the first file.
        reference = source
    elif damage == 'mdat_size_short':
        struct.pack_into('>I', data, mdat[1], 8)
    elif damage == 'lost_sample_sizes':
        p = data.find(b'stsz', moov[1])
        struct.pack_into('>I', data, p + 12, 0xFFFFFFFE)
    damaged_file = tmp_path/'damaged.mp4'; damaged_file.write_bytes(data)
    before = hashlib.sha256(data).hexdigest()
    result = recover_mp4(damaged_file, tmp_path/'out', reference)
    assert result['status'] == 'video_recovered', result
    v = result['videos'][0]; indices = expected_indices(packets, damaged)
    assert v['decode_check']['status'] == 'passed'
    assert v['candidate_frames'] == v['decode_check']['decoded_frames'] == len(indices)
    assert pixels(tmp_path/'out'/v['file']) == [original[i] for i in indices]
    assert result['copy_sha256_verified'] and not result['original_timeline_preserved']
    assert hashlib.sha256(damaged_file.read_bytes()).hexdigest() == before
    assert (tmp_path/'out'/'damaged_source.mp4').read_bytes() == data
    rows = list(csv.DictReader((tmp_path/'out'/'nal_offsets.csv').open(encoding='utf-8-sig')))
    assert rows and all(int(r['source_nal_offset']) + int(r['source_nal_size']) <= len(data) for r in rows)


@pytest.mark.parametrize('kind', ['avc', 'bframes', 'hevc', 'fragmented'])
def test_codec_and_reordering_preserve_all_pixels(mp4_media, tmp_path, kind):
    source = mp4_media[kind]
    result = recover_mp4(source, tmp_path/'out')
    assert result['status'] == 'video_recovered', result
    v = result['videos'][0]
    assert v['candidate_frames'] == v['decode_check']['decoded_frames'] == 90
    assert pixels(tmp_path/'out'/v['file']) == pixels(source)


@pytest.mark.parametrize('damage', ['moof_zero', 'mdat_header_zero', 'init_zero', 'fragment_payload_zero'])
def test_fragment_resync_reaches_later_healthy_fragments(mp4_media, tmp_path, damage):
    source = mp4_media['fragmented']; data = bytearray(source.read_bytes()); boxes = top_boxes(data)
    moofs = [b for b in boxes if b[0] == b'moof']; mdats = [b for b in boxes if b[0] == b'mdat']
    reference = None; damaged = []
    if damage == 'init_zero':
        end = moofs[0][1]; data[:end] = bytes(end); reference = source
    elif damage == 'moof_zero':
        a, b = moofs[2][1:]; data[a:b] = bytes(b-a); damaged = [mdats[2][1:]]
    elif damage == 'mdat_header_zero':
        a = mdats[2][1]; data[a:a+8] = bytes(8); damaged = [mdats[2][1:]]
    else:
        a, b = mdats[2][1] + 8, mdats[2][2]; data[a:b] = bytes(b-a); damaged = [(a, b)]
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out', reference)
    assert r['status'] == 'video_recovered', r
    v = r['videos'][0]; original = pixels(source)
    indices = expected_indices(packet_map(source), damaged)
    assert v['decode_check']['decoded_frames'] == len(indices)
    assert pixels(tmp_path/'out'/v['file']) == [original[i] for i in indices]


@pytest.mark.parametrize('slack', ['appended_file', 'video_in_free', 'video_before_moov', 'header_lost_appended'])
def test_slack_never_enters_recovered_video(mp4_media, tmp_path, slack):
    source = mp4_media['avc']; data = source.read_bytes(); stale = mp4_media['wrong'].read_bytes()
    if slack in ('appended_file', 'header_lost_appended'):
        data += stale
        if slack == 'header_lost_appended':
            data = bytes(32) + data[32:]
    elif slack == 'video_in_free':
        data += struct.pack('>I4s', len(stale)+8, b'free') + stale
    else:
        # Preallocated slack immediately before moov, with live sample offsets
        # still valid: the table, not the apparent physical mdat end, owns video.
        moov = next(b for b in top_boxes(data) if b[0] == b'moov')
        data = data[:moov[1]] + struct.pack('>I4s', len(stale)+8, b'free') + stale + data[moov[1]:]
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out')
    assert r['status'] == 'video_recovered', r
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(source)


def nmea(body):
    value = 0
    for c in body:
        value ^= ord(c)
    return (body+f'*{value:02X}').encode()


def test_metadata_survives_total_container_loss_and_rejects_bad_fixes(tmp_path):
    good = nmea('GPRMC,120000.00,A,3730.000,N,12700.000,E,10.0,0.0,081026,,,A')
    zero = nmea('GPRMC,120001.00,A,0000.000,N,00000.000,E,10.0,0.0,081026,,,A')
    bad_date = nmea('GPRMC,120002.00,A,3730.000,N,12700.000,E,10.0,0.0,321326,,,A')
    invalid = nmea('GPRMC,120003.00,V,3730.000,N,12700.000,E,10.0,0.0,081026,,,N')
    path = tmp_path/'destroyed.mp4'
    path.write_bytes(bytes(4096)+b'\0'+good+b'\r\n'+b'$'+good+b'\r\n'+zero+b'\r\n'+bad_date+b'\r\n'+invalid+
                     b'\r\ngsensori,4,512,512,-256,1024;'+bytes(100)+good[:-2]+b'00\r\n')
    r = recover_mp4(path, tmp_path/'out')
    assert r['status'] == 'metadata_only' and r['gps_records'] == 2 and r['gsensor_records'] == 1
    assert r['videos'] == [] and not r['video_boundary_verified']
    rows = list(csv.DictReader((tmp_path/'out'/'recovered_gps.csv').open(encoding='utf-8-sig')))
    assert all(row['scope'] == 'whole_file_untrusted' and row['gps_date_utc'] == '2026-10-08' for row in rows)
    assert float(rows[0]['speed_kmh']) == pytest.approx(18.52)


def test_cancel_during_video_export_cleans_temp_results(mp4_media, tmp_path):
    cancel = threading.Event()
    def progress(message):
        if '후보 MP4' in message:
            cancel.set()
    with pytest.raises(RecoveryCancelled):
        recover_mp4(mp4_media['avc'], tmp_path/'out', cancel=cancel, progress=progress)
    assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.mp4-recovery-*'))


def test_wrong_reference_cannot_supply_pictures(mp4_media, tmp_path):
    with pytest.raises(ValueError, match='설정'):
        recover_mp4(mp4_media['avc'], tmp_path/'out', mp4_media['wrong'])
    assert not (tmp_path/'out').exists()


def test_no_decoder_does_not_claim_verified_mp4(mp4_media, tmp_path, monkeypatch):
    monkeypatch.setattr('core.mp4_recovery.shutil.which', lambda _: None)
    r = recover_mp4(mp4_media['avc'], tmp_path/'out')
    assert r['status'] == 'video_candidates_unverified'
    assert r['videos'][0]['decode_check']['status'] == 'not_run'
    assert r['videos'][0]['file'] and (tmp_path/'out'/r['videos'][0]['file']).exists()


def test_existing_output_is_preserved(mp4_media, tmp_path):
    out = tmp_path/'out'; out.mkdir(); (out/'marker').write_text('keep')
    with pytest.raises(FileExistsError):
        recover_mp4(mp4_media['avc'], out)
    assert (out/'marker').read_text() == 'keep'


def test_source_change_discards_results(mp4_media, tmp_path):
    source = tmp_path/'source.mp4'; shutil.copyfile(mp4_media['avc'], source)
    changed = False
    def progress(message):
        nonlocal changed
        if '별도 회수' in message and not changed:
            with source.open('ab') as f:
                f.write(b'changed')
            changed = True
    with pytest.raises(ValueError, match='원본이 변경'):
        recover_mp4(source, tmp_path/'out', progress=progress)
    assert not (tmp_path/'out').exists()


def test_cli_help_in_legacy_windows_console():
    env = dict(os.environ, PYTHONIOENCODING='cp1252', PYTHONUTF8='0')
    r = subprocess.run([__import__('sys').executable, '-m', 'core.mp4_recovery', '--help'],
                       capture_output=True, env=env, timeout=20)
    assert r.returncode == 0 and b'--reference' in r.stdout and b'--fps' in r.stdout


def test_ui_format_selection_and_success_summary(qapp):
    from ui.recovery_dialog import RecoveryDialog
    dialog = RecoveryDialog(); dialog._format.setCurrentIndex(1)
    assert 'MP4' in dialog._notice.text() and 'MP4' in dialog.windowTitle()
    dialog._done(dict(videos=[dict(decode_check=dict(status='passed'))], jpeg_stills=0,
                      gps_records=20, gsensor_records=200, output_dir='result'))
    assert '200' in dialog._status.text() and dialog._open.isEnabled()
    dialog._format.setCurrentIndex(0)
    assert 'MJPEG' in dialog._notice.text()
    dialog.deleteLater(); qapp.processEvents()


def test_missing_moov_bframes_reconstruct_display_order(mp4_media, tmp_path):
    source = mp4_media['bframes']; data = source.read_bytes()
    moov = next(b for b in top_boxes(data) if b[0] == b'moov')
    path = tmp_path/'cut.mp4'; path.write_bytes(data[:moov[1]])
    r = recover_mp4(path, tmp_path/'out', source)
    assert r['status'] == 'video_recovered', r
    assert r['videos'][0]['candidate_frames'] == 90
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(source)


def test_two_video_tracks_are_recovered_separately(mp4_media, tmp_path):
    source = tmp_path/'dual.mp4'
    ffmpeg('-i', mp4_media['avc'], '-i', mp4_media['wrong'], '-map', '0:v:0', '-map', '1:v:0', '-c', 'copy', source)
    r = recover_mp4(source, tmp_path/'out')
    assert r['status'] == 'video_recovered' and len(r['videos']) == 2
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(mp4_media['avc'])
    assert pixels(tmp_path/'out'/r['videos'][1]['file']) == pixels(mp4_media['wrong'])
    moov = next(b for b in top_boxes(source.read_bytes()) if b[0] == b'moov')
    path = tmp_path/'cut.mp4'; path.write_bytes(source.read_bytes()[:moov[1]])
    r = recover_mp4(path, tmp_path/'withheld', source)
    assert not r['videos'] and r['video_withheld_reason']


def test_reference_has_no_media_bytes_in_output(mp4_media, tmp_path):
    # Different picture content, identical encoder settings: reference supplies
    # no sample sizes, offsets, composition map, or pictures to the output.
    reference = tmp_path/'reference.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'color=blue:size=160x90:rate=30', '-t', '2', '-c:v',
           'libx264', '-threads', '1', '-g', '15', '-keyint_min', '15', '-sc_threshold', '0', '-bf', '0', reference)
    data = mp4_media['avc'].read_bytes(); moov = next(b for b in top_boxes(data) if b[0] == b'moov')
    path = tmp_path/'cut.mp4'; path.write_bytes(data[:moov[1]])
    r = recover_mp4(path, tmp_path/'out', reference)
    assert r['reference']['usage'] == 'codec_settings_only'
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(mp4_media['avc'])


def test_raw_carving_stops_before_embedded_recording(mp4_media, tmp_path):
    source = mp4_media['avc']; data = source.read_bytes()
    moov = next(b for b in top_boxes(data) if b[0] == b'moov')
    mdat = next(b for b in top_boxes(data) if b[0] == b'mdat')
    data = bytearray(data[:moov[1]] + mp4_media['wrong'].read_bytes())
    struct.pack_into('>I', data, mdat[1], len(data)-mdat[1])
    path = tmp_path/'cut.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out', source)
    assert r['status'] == 'video_recovered'
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(source)


def test_index_cannot_point_into_known_free_slack(mp4_media, tmp_path):
    source = mp4_media['avc']; data = bytearray(source.read_bytes())
    size = len(data); packet = packet_map(source)[0]
    old = data[int(packet['pos']):int(packet['pos'])+int(packet['size'])]
    data += struct.pack('>I4s', len(old)+8, b'free') + old
    stco = data.find(b'stco'); struct.pack_into('>I', data, stco+12, size+8)
    path = tmp_path/'poisoned.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out')
    # A corrupt source table may fall back to bounded current mdat carving.
    for row in csv.DictReader((tmp_path/'out'/'nal_offsets.csv').open(encoding='utf-8-sig')):
        assert int(row['source_nal_offset']) < size


def test_empty_and_unallocated_garbage_never_claim_success(tmp_path):
    path = tmp_path/'garbage.mp4'; path.write_bytes(bytes(8192))
    r = recover_mp4(path, tmp_path/'out')
    assert r['status'] == 'nothing_recovered' and not r['videos']
    assert r['video_withheld_reason']


def test_native_recovered_mp4_plays_in_real_qt(mp4_media, tmp_path, tracker, qapp):
    from test_media_smoke import wait_until
    r = recover_mp4(mp4_media['avc'], tmp_path/'out')
    frames = []
    tracker._video_widget.videoSink().videoFrameChanged.connect(
        lambda f: frames.append(f.startTime()) if f.isValid() else None)
    tracker.load_video(str(tmp_path/'out'/r['videos'][0]['file']))
    assert wait_until(qapp, lambda: tracker._player.isSeekable() and not tracker._priming)
    tracker._toggle_play()
    assert wait_until(qapp, lambda: len(frames) >= 5 and tracker._player.position() >= 150)


@pytest.mark.parametrize('field', ['stsz', 'stsc', 'stco', 'stts'])
def test_hostile_table_counts_remain_bounded_and_salvage_media(mp4_media, tmp_path, field):
    data = bytearray(mp4_media['avc'].read_bytes()); pos = data.find(field.encode())
    struct.pack_into('>I', data, pos + (12 if field == 'stsz' else 8), 0xFFFFFFFF)
    path = tmp_path/'hostile.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out')
    assert r['status'] == 'video_recovered', r
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(mp4_media['avc'])


@pytest.mark.parametrize('kind', ['avc', 'bframes'])
def test_co64_extended_mdat_and_version1_headers_roundtrip(mp4_media, tmp_path, kind):
    first = recover_mp4(mp4_media[kind], tmp_path/'first')
    source = tmp_path/'first'/first['videos'][0]['file']
    second = recover_mp4(source, tmp_path/'second')
    assert second['status'] == 'video_recovered'
    assert pixels(tmp_path/'second'/second['videos'][0]['file']) == pixels(mp4_media[kind])


def test_missing_all_codec_settings_still_recovers_metadata(tmp_path, mp4_media):
    data = mp4_media['avc'].read_bytes()
    moov = next(b for b in top_boxes(data) if b[0] == b'moov')
    data = data[:moov[1]]
    gps = nmea('GPRMC,120000.00,A,3730.000,N,12700.000,E,10.0,0.0,081026,,,A')
    path = tmp_path/'lost_moov.mp4'; path.write_bytes(data+b'\r\n'+gps+b'\r\n')
    r = recover_mp4(path, tmp_path/'out')
    assert r['status'] == 'metadata_only' and r['gps_records'] == 1
    assert not r['videos']


def test_broken_codec_and_sample_tables_do_not_block_gps(mp4_media, tmp_path):
    data = bytearray(mp4_media['avc'].read_bytes())
    cc = data.find(b'avcC'); count = int.from_bytes(data[cc+10:cc+12], 'big')
    data[cc+13:cc+12+count] = bytes(count-1)
    sz = data.find(b'stsz'); struct.pack_into('>I', data, sz+12, 0xFFFFFFFF)
    gps = nmea('GPRMC,120000.00,A,3730.000,N,12700.000,E,10.0,0.0,081026,,,A')
    data += struct.pack('>I4s', len(gps)+10, b'free')+b'\r\n'+gps
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out')
    assert r['status'] == 'metadata_only' and r['gps_records'] == 1
    assert r['diagnostics'] and not r['videos']


# Reuse the established Qt fixture instead of creating a second QApplication.
from test_review_regressions import qapp, tracker  # noqa: E402,F401
