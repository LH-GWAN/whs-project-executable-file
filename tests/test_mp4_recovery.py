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
import sys
import threading
from fractions import Fraction

import pytest

from core.mp4_recovery import recover_mp4, RecoveryCancelled


def ffmpeg(*args):
    return subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', *map(str, args)],
                          capture_output=True, check=True, timeout=60)


def pixels(path, stream=0):
    r = ffmpeg('-xerror', '-i', path, '-map', f'0:v:{stream}', '-f', 'framemd5', '-')
    assert not r.stderr
    return [line.rsplit(',', 1)[-1].strip() for line in r.stdout.decode().splitlines()
            if line and not line.startswith('#')]


def packet_map(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
                        '-show_entries', 'packet=pos,size,flags', '-of', 'json', str(path)],
                       capture_output=True, check=True, timeout=20)
    return json.loads(r.stdout)['packets']


def video_rate(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=r_frame_rate', '-of', 'json', str(path)],
                       capture_output=True, check=True, timeout=20)
    return Fraction(json.loads(r.stdout)['streams'][0]['r_frame_rate'])


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
    for kind in ('avc', 'bframes', 'fragmented', 'hevc', 'wrong',
                 'fragmented_bframes', 'fragmented_hevc_bframes', 'fragmented_continuous'):
        path = root / (kind + '.mp4')
        codec = 'libx265' if 'hevc' in kind else 'libx264'
        size = '128x96' if kind == 'wrong' else '160x90'
        gop = '60' if kind == 'fragmented_continuous' else '15'
        bframes = '2' if 'bframes' in kind else '0'
        extra = (['-x265-params', 'pools=1:frame-threads=1:keyint=15:min-keyint=15:scenecut=0:'
                  f'bframes={bframes}:log-level=error'] if codec == 'libx265' else
                 ['-g', gop, '-keyint_min', gop, '-sc_threshold', '0', '-bf', bframes])
        ffmpeg('-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=30', '-f', 'lavfi',
               '-i', 'sine=frequency=440:sample_rate=48000', '-t', '3', '-c:v', codec,
               '-threads', '1', '-pix_fmt', 'yuv420p', *extra, '-c:a', 'aac',
               *(['-movflags', 'frag_keyframe+empty_moov+default_base_moof']
                 if kind.startswith('fragmented') else []),
               *(['-frag_duration', '500000'] if kind == 'fragmented_continuous' else []), path)
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


@pytest.mark.parametrize('kind', ['avc', 'bframes', 'hevc', 'fragmented',
                                 'fragmented_bframes', 'fragmented_hevc_bframes'])
def test_codec_and_reordering_preserve_all_pixels(mp4_media, tmp_path, kind):
    source = mp4_media[kind]
    result = recover_mp4(source, tmp_path/'out')
    assert result['status'] == 'video_recovered', result
    v = result['videos'][0]
    assert v['candidate_frames'] == v['decode_check']['decoded_frames'] == 90
    assert pixels(tmp_path/'out'/v['file']) == pixels(source)
    assert video_rate(tmp_path/'out'/v['file']) == video_rate(source)


@pytest.mark.parametrize('damage', ['moof_zero', 'mdat_header_zero', 'init_zero', 'fragment_payload_zero'])
@pytest.mark.parametrize('kind', ['fragmented', 'fragmented_continuous'])
def test_fragment_resync_reaches_later_healthy_fragments(mp4_media, tmp_path, damage, kind):
    source = mp4_media[kind]; data = bytearray(source.read_bytes()); boxes = top_boxes(data)
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
    assert video_rate(tmp_path/'out'/v['file']) == video_rate(source)


@pytest.mark.parametrize('preallocated', [False, True])
def test_fragment_partial_recording_keeps_original_fps(mp4_media, tmp_path, preallocated):
    source = mp4_media['fragmented']; packets = packet_map(source)
    data = bytearray(source.read_bytes())
    end = int(packets[65]['pos']) + int(packets[65]['size']) // 2
    damaged = [(end, len(data))]
    if preallocated:
        data[end:] = bytes(len(data) - end)
    else:
        del data[end:]
    path = tmp_path/'unfinished.mp4'; path.write_bytes(data)
    result = recover_mp4(path, tmp_path/'out')
    assert result['status'] == 'video_recovered', result
    video = result['videos'][0]; recovered = tmp_path/'out'/video['file']
    indices = expected_indices(packets, damaged); original = pixels(source)
    assert pixels(recovered) == [original[i] for i in indices]
    assert video_rate(recovered) == video_rate(source)


@pytest.mark.parametrize('damaged_stream', [0, 1])
def test_partial_multitrack_index_loss_cannot_mix_cameras(tmp_path, damaged_stream):
    source = tmp_path/'dual.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30', '-f', 'lavfi',
           '-i', 'color=c=blue:size=160x90:rate=30', '-t', '3', '-map', '0:v', '-map', '1:v',
           '-c:v', 'libx264', '-threads', '1', '-g', '15', '-keyint_min', '15',
           '-sc_threshold', '0', '-bf', '0', source)
    data = bytearray(source.read_bytes())
    first = data.find(b'stsz'); second = data.find(b'stsz', first+4)
    struct.pack_into('>I', data, (first, second)[damaged_stream]+12, 0xFFFFFFFF)
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    result = recover_mp4(path, tmp_path/'out')
    assert result['status'] == 'video_recovered', result
    assert len(result['videos']) == 1, result['videos']
    video = result['videos'][0]; live_stream = 1-damaged_stream
    assert video['stream'] == live_stream+1
    assert video['decode_check']['decoded_frames'] == 90
    assert pixels(tmp_path/'out'/video['file']) == pixels(source, live_stream)
    assert any('샘플 표' in message for message in result['diagnostics'])


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


@pytest.mark.parametrize('lost', [1, 18, 46])
def test_hevc_cra_resumes_after_a_lost_picture(mp4_media, tmp_path, lost):
    source = mp4_media['hevc']; data = bytearray(source.read_bytes()); packets = packet_map(source)
    packet = packets[lost]; a = int(packet['pos']); b = a + int(packet['size'])
    data[a:b] = bytes(b-a)
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    result = recover_mp4(path, tmp_path/'out')
    original = pixels(source)
    expected = [original[i] for i in expected_indices(packets, [(a, b)])]
    assert pixels(tmp_path/'out'/result['videos'][0]['file']) == expected


def test_corrupt_entropy_keeps_the_healthy_gop_prefix(mp4_media, tmp_path):
    source = mp4_media['avc']; data = bytearray(source.read_bytes()); packet = packet_map(source)[8]
    a, size = int(packet['pos']), int(packet['size'])
    # Preserve the length and slice headers. This damage is found by the full
    # decoder, rather than a zero-fill/header heuristic in the recovery parser.
    data[a+128:a+size] = b'\xff' * (size-128)
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    result = recover_mp4(path, tmp_path/'out')
    original = pixels(source)
    assert pixels(tmp_path/'out'/result['videos'][0]['file']) == original[:8] + original[15:]


def test_decoder_error_after_log_preview_cannot_pass():
    from core.mp4_recovery import run_command
    result = run_command([sys.executable, '-c',
        "import sys; sys.stderr.write(' ' * 4096 + 'decode error'); print('frame=1')"], None)
    assert result['status'] == 'failed'


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_fast_decoder_output_is_bounded(stream):
    from core.mp4_recovery import run_command
    result = run_command([sys.executable, '-c',
        f"import sys; sys.{stream}.write(' ' * (8 * 1024**2 + 1))"], None)
    assert result['status'] == 'log_limit'


def test_recovery_copy_change_discards_results(mp4_media, tmp_path):
    def progress(message):
        if '별도 회수' in message:
            copied = next(tmp_path.glob('.mp4-recovery-*/damaged_source.mp4'))
            with copied.open('r+b') as out:
                out.seek(-1, os.SEEK_END); out.write(b'X')
    with pytest.raises(ValueError, match='사본'):
        recover_mp4(mp4_media['avc'], tmp_path/'out', progress=progress)
    assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.mp4-recovery-*'))


def test_validated_output_change_discards_results(mp4_media, tmp_path):
    def progress(message):
        if '별도 회수' in message:
            target = next(tmp_path.glob('.mp4-recovery-*/recovered_track*.mp4'))
            with target.open('ab') as out:
                out.write(b'changed after decode')
    with pytest.raises(ValueError, match='결과'):
        recover_mp4(mp4_media['avc'], tmp_path/'out', progress=progress)
    assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.mp4-recovery-*'))


@pytest.mark.parametrize('kind', ['fragmented_hevc_bframes', 'hevc', 'bframes', 'fragmented_bframes'])
def test_hevc_random_access_has_exact_display_order(mp4_media, tmp_path, kind):
    source = mp4_media[kind]; original = pixels(source); packets = packet_map(source)
    frame_info = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
        '-show_entries', 'frame=pkt_pos', '-of', 'json', str(source)], capture_output=True, check=True, timeout=20)
    positions = [int(f['pkt_pos']) for f in json.loads(frame_info.stdout)['frames']]
    assert len(positions) == len(original)
    data = bytearray(source.read_bytes()); p = packets[18]; a, n = int(p['pos']), int(p['size'])
    data[a:a+n] = bytes(n)
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out'); v = r['videos'][0]
    rows = list(csv.DictReader((tmp_path/'out'/'frame_offsets.csv').open(encoding='utf-8-sig')))
    recovered = {int(row['source_payload_offset']) for row in rows}
    expected = [picture for pos, picture in zip(positions, original) if pos in recovered]
    assert v['candidate_frames'] >= 70 and len(expected) == v['candidate_frames']
    assert pixels(tmp_path/'out'/v['file']) == expected
    if 'hevc_bframes' in kind:
        assert v['exclusions']['discarded_rasl'] >= 1


@pytest.mark.parametrize('kind', ['bframes', 'fragmented_hevc_bframes'])
def test_source_time_csv_matches_independent_container_ticks(mp4_media, tmp_path, kind):
    source = mp4_media[kind]
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
        '-show_entries', 'packet=pos,dts,pts,duration', '-of', 'json', str(source)],
        capture_output=True, check=True, timeout=20)
    packets = json.loads(probe.stdout)['packets']; bypos = {int(p['pos']): p for p in packets}
    origin = int(packets[0]['dts'])
    r = recover_mp4(source, tmp_path/'out')
    rows = list(csv.DictReader((tmp_path/'out'/'frame_offsets.csv').open(encoding='utf-8-sig')))
    assert not r['original_timeline_preserved']
    for row in rows:
        packet = bypos[int(row['source_payload_offset'])]
        assert int(row['source_dts_ticks']) == int(packet['dts']) - origin
        assert int(row['source_pts_ticks']) == int(packet['pts']) - origin


def test_bit_exact_audit_rejects_an_output_that_a_decoder_accepts(mp4_media, tmp_path, monkeypatch):
    import core.mp4_writer as writer
    original_write = writer.write_mp4
    def wrong_write(target, *args, **kwargs):
        original_write(target, *args, **kwargs)
        packet = packet_map(target)[0]
        with target.open('r+b') as out:
            # Keep the sample size, but alter one copied byte.
            out.seek(int(packet['pos'])+5); old = out.read(1)
            out.seek(-1, os.SEEK_CUR); out.write(bytes([old[0] ^ 1]))
    monkeypatch.setattr(writer, 'write_mp4', wrong_write)
    monkeypatch.setattr('core.mp4_recovery.decode_check',
                        lambda path, count, cancel: dict(status='passed', decoded_frames=count))
    r = recover_mp4(mp4_media['avc'], tmp_path/'out')
    assert r['status'] == 'nothing_recovered'
    assert r['videos'][0]['payload_check']['status'] == 'failed'
    assert not list((tmp_path/'out').glob('recovered_track*.mp4'))


def test_independent_probe_failure_cannot_be_verified(mp4_media, tmp_path, monkeypatch):
    monkeypatch.setattr('core.mp4_recovery.probe_check',
                        lambda *args: dict(status='failed', reason='packet count mismatch'))
    r = recover_mp4(mp4_media['avc'], tmp_path/'out')
    assert r['status'] == 'nothing_recovered'
    assert r['videos'][0]['decode_check']['status'] == 'failed'
    assert not list((tmp_path/'out').glob('recovered_track*.mp4'))


def test_validation_records_tools_and_all_payload_hashes(mp4_media, tmp_path):
    r = recover_mp4(mp4_media['avc'], tmp_path/'out'); v = r['videos'][0]
    assert r['schema'] == 2 and r['copy_sha256_verified_at_completion']
    assert v['payload_check']['matched_frames'] == v['candidate_frames'] == 90
    assert v['payload_check']['source_nal_sha256'] == v['payload_check']['output_nal_sha256']
    assert v['decode_check']['probe_check']['video_packets'] == 90
    for tool in r['verification_tools'].values():
        assert tool['available'] and tool['version'] and len(tool['sha256']) == 64
    for name, digest in r['artifact_sha256'].items():
        assert hashlib.sha256((tmp_path/'out'/name).read_bytes()).hexdigest() == digest


def test_metadata_regions_do_not_promote_slack_to_trusted_gps(tmp_path):
    from core.mp4_writer import box
    gps = nmea('GPRMC,120000.00,A,3730.000,N,12700.000,E,1000.0,0.0,081026,,,A')
    undated = nmea('GPGGA,120001.00,3730.000,N,12700.000,E,1,08,1.0,10.0,M,0.0,M,,')
    ftyp = box(b'ftyp', b'isom'+bytes(4)+b'isom')
    path = tmp_path/'source.mp4'
    path.write_bytes(ftyp+box(b'mdat', b'\0'+gps+b'\r\n')+box(b'free', b'\0'+undated+b'\r\n')+
                     ftyp+box(b'mdat', b'\0'+gps+b'\r\n'))
    r = recover_mp4(path, tmp_path/'out')
    rows = list(csv.DictReader((tmp_path/'out'/'recovered_gps.csv').open(encoding='utf-8-sig')))
    assert r['gps_records'] == 3
    assert [row['source_region'] for row in rows] == ['mdat_unindexed', 'free_or_skip', 'after_first_recording']
    assert all(row['scope'] == 'whole_file_untrusted' for row in rows)
    assert rows[1]['gps_date_utc'] == '' and rows[0]['idas_outlier_reason'] == 'invalid_speed'


@pytest.mark.parametrize('kind', ['hevc', 'bframes'])
def test_compact_sample_sizes_keep_all_original_pixels(mp4_media, tmp_path, kind):
    from core.mp4_writer import box, full
    data = mp4_media[kind].read_bytes()
    def convert(data):
        result = b''
        for typ, a, b in top_boxes(data):
            payload = data[a+8:b]
            if typ in (b'moov', b'trak', b'mdia', b'minf', b'stbl'):
                payload = convert(payload)
            elif typ == b'stsz':
                fixed, count = struct.unpack_from('>II', payload, 4)
                sizes = [fixed]*count if fixed else list(struct.unpack_from('>'+str(count)+'I', payload, 12))
                result += full(b'stz2', bytes(3)+b'\x10'+struct.pack('>I', count)+
                               b''.join(struct.pack('>H', n) for n in sizes))
                continue
            result += box(typ, payload)
        return result
    path = tmp_path/'compact.mp4'; path.write_bytes(convert(data))
    assert pixels(path) == pixels(mp4_media[kind])  # Independent reader accepts the fixture.
    r = recover_mp4(path, tmp_path/'out')
    assert r['videos'][0]['recovery_method'] == 'source_sample_tables'
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(mp4_media[kind])


@pytest.mark.parametrize('kind', ['hevc', 'avc'])
def test_broken_movie_header_preserves_valid_track_tables(mp4_media, tmp_path, kind):
    data = bytearray(mp4_media[kind].read_bytes()); p = data.find(b'mvhd'); data[p:p+4] = b'xxxx'
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out')
    assert r['diagnostics'] and r['videos'][0]['recovery_method'] == 'source_sample_tables'
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(mp4_media[kind])


@pytest.mark.parametrize('width,packed', [(4, b'\x12\xf0'), (8, b'\x01\x02\xff'), (16, b'\x00\x01\x00\x02\xff\xff')])
def test_compact_size_widths_and_bad_counts_are_bounded(width, packed):
    from core.mp4_structure import InvalidMP4, box_at, sample_sizes
    from core.mp4_writer import box, full
    data = box(b'stbl', full(b'stz2', bytes(3)+bytes([width])+struct.pack('>I', 3)+packed))
    assert sample_sizes(data, box_at(data, 0, len(data))) == [1, 2, (1 << width)-1]
    bad = bytearray(data); struct.pack_into('>I', bad, 24, 0xFFFFFFFF)
    with pytest.raises(InvalidMP4):
        sample_sizes(bad, box_at(bad, 0, len(bad)))


def test_publish_failure_cleans_only_owned_results(tmp_path, monkeypatch):
    from core.mp4_recovery import publish_result
    work = tmp_path/'work'; work.mkdir(); (work/'a.csv').write_text('first')
    (work/'recovery.json').write_text('{}'); output = tmp_path/'out'
    original_rename = os.rename
    def failing_rename(source, target):
        if Path(source).name == 'recovery.json':
            (output/'other_process').write_text('preserve')
            raise OSError('publication interrupted')
        return original_rename(source, target)
    monkeypatch.setattr('core.mp4_recovery.os.rename', failing_rename)
    with pytest.raises(OSError, match='interrupted'):
        publish_result(work, output, None)
    assert list(output.iterdir()) == [output/'other_process']
    assert (output/'other_process').read_text() == 'preserve'


def test_decoder_timeout_and_cancel_terminate_the_child():
    from core.mp4_recovery import run_command
    result = run_command([sys.executable, '-c', 'import time; time.sleep(20)'], None, timeout=.05)
    assert result['status'] == 'timeout'
    cancel = threading.Event(); timer = threading.Timer(.05, cancel.set); timer.start()
    try:
        with pytest.raises(RecoveryCancelled):
            run_command([sys.executable, '-c', 'import time; time.sleep(20)'], cancel)
    finally:
        timer.cancel()


# Reuse the established Qt fixture instead of creating a second QApplication.
from test_review_regressions import qapp, tracker  # noqa: E402,F401
