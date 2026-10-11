"""Decode/pixel oracles for missing HEVC tables and whole-job time limits."""
import csv
import json
from pathlib import Path
import struct
import subprocess
import sys
import time

import pytest

from core.mp4_recovery import (NMEA, RecoveryDeadline, RecoveryTimedOut, bounded_matches,
                               recover_mp4, run_command)
from core.mp4_structure import children, discover
from test_mp4_recovery import mp4_media, pixels, top_boxes


@pytest.mark.parametrize('damage', ['moov_cut', 'moov_cut_and_middle', 'partial_mdat_cut'])
def test_unindexed_hevc_keeps_exact_original_pixels(mp4_media, tmp_path, damage):
    source = mp4_media['hevc']; data = bytearray(source.read_bytes())
    moov = next(b for b in top_boxes(data) if b[0] == b'moov')
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
        '-show_entries', 'packet=pos,size,flags', '-of', 'json', str(source)], capture_output=True, check=True, timeout=20)
    packets = json.loads(probe.stdout)['packets']; expected = list(range(90))
    if damage == 'moov_cut_and_middle':
        a = int(packets[22]['pos']); b = a+int(packets[22]['size'])
        data[a:b] = bytes(b-a); expected = list(range(22))+list(range(30,90))
    if damage == 'partial_mdat_cut':
        stop = int(packets[65]['pos'])+int(packets[65]['size'])//2
        data = data[:stop]; expected = list(range(65))
    else:
        data = data[:moov[1]]
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out', source)
    assert r['status'] == 'video_recovered', r
    video = r['videos'][0]
    assert video['recovery_method'] == 'bounded_hevc_nal_carving'
    assert video['candidate_frames'] == len(expected)
    original = pixels(source)
    assert pixels(tmp_path/'out'/video['file']) == [original[i] for i in expected]
    rows = list(csv.DictReader((tmp_path/'out'/'frame_offsets.csv').open(encoding='utf-8-sig')))
    assert all(row['source_sample_number_basis'] == 'unknown' and not row['source_pts_ticks'] for row in rows)


@pytest.mark.parametrize('kind', ['fragmented', 'fragmented_hevc_bframes'])
def test_missing_fragment_video_table_does_not_invent_reordered_pictures(mp4_media, tmp_path, kind):
    source=mp4_media[kind]; data=bytearray(source.read_bytes()); bs,_,_=discover(data)
    moof=[b for b in bs if b.kind==b'moof'][2]
    for traf in children(data,moof.payload,moof.end):
        if traf.kind != b'traf': continue
        sub=children(data,traf.payload,traf.end)
        tfhd=next(b for b in sub if b.kind==b'tfhd')
        if int.from_bytes(data[tfhd.payload+4:tfhd.payload+8],'big')==1:
            run=next(b for b in sub if b.kind==b'trun'); struct.pack_into('>I',data,run.payload+4,0xfffffffe)
    path=tmp_path/'lost.mp4';path.write_bytes(data)
    r=recover_mp4(path,tmp_path/'out');v=r['videos'][0]
    if kind=='fragmented':
        assert v['candidate_frames']==90 and v['carved_frames']==15
        assert pixels(tmp_path/'out'/v['file'])==pixels(source)
    else:
        assert v['carved_frames']==0 and 'B-picture' in v['exclusions']['withheld']
        assert v['candidate_frames']<90 and v['decode_check']['status']=='passed'


@pytest.mark.parametrize('damage', ['signature', 'mfhd'])
def test_fragment_damage_preserves_anchored_media(mp4_media,tmp_path,damage):
    source=mp4_media['fragmented'];data=bytearray(source.read_bytes());bs,_,_=discover(data)
    moof=[b for b in bs if b.kind==b'moof'][2]
    if damage=='signature':data[moof.start+4:moof.start+8]=bytes(4)
    else:
        mfhd=next(b for b in children(data,moof.payload,moof.end) if b.kind==b'mfhd')
        data[mfhd.start+4:mfhd.start+8]=bytes(4)
    path=tmp_path/'lost.mp4';path.write_bytes(data)
    r=recover_mp4(path,tmp_path/'out');v=r['videos'][0]
    assert v['candidate_frames']==90 and v['decode_check']['status']=='passed'
    assert pixels(tmp_path/'out'/v['file'])==pixels(source)


def test_unindexed_hevc_b_picture_order_is_withheld(mp4_media,tmp_path):
    # Remux actual HEVC B pictures into a classic MP4 and remove its moov.
    source=tmp_path/'b.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(mp4_media['fragmented_hevc_bframes']),
        '-map','0:v:0','-c','copy',str(source)],capture_output=True,check=True,timeout=20)
    data=source.read_bytes();moov=next(b for b in top_boxes(data) if b[0]==b'moov')
    path=tmp_path/'lost.mp4';path.write_bytes(data[:moov[1]])
    r=recover_mp4(path,tmp_path/'out',source)
    assert not r['videos'] and any('B-picture' in x for x in r['diagnostics'])


def test_orphaned_media_does_not_carve_an_embedded_recording(mp4_media,tmp_path):
    source=mp4_media['fragmented'];data=bytearray(source.read_bytes());bs,_,_=discover(data)
    moof=[b for b in bs if b.kind==b'moof'][2]
    mdat=next(b for b in bs if b.kind==b'mdat' and b.start==moof.end)
    foreign=tmp_path/'foreign.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=blue:size=160x90:rate=30',
        '-t','0.1','-c:v','libx264','-threads','1',str(foreign)],capture_output=True,check=True,timeout=20)
    other=foreign.read_bytes();assert len(other)<mdat.end-mdat.payload
    data[mdat.payload:mdat.payload+len(other)]=other
    for traf in children(data,moof.payload,moof.end):
        if traf.kind != b'traf':continue
        subs=children(data,traf.payload,traf.end);tfhd=next(b for b in subs if b.kind==b'tfhd')
        if int.from_bytes(data[tfhd.payload+4:tfhd.payload+8],'big')==1:
            run=next(b for b in subs if b.kind==b'trun');struct.pack_into('>I',data,run.payload+4,0xfffffffe)
    path=tmp_path/'mixed.mp4';path.write_bytes(data)
    r=recover_mp4(path,tmp_path/'out');v=r['videos'][0]
    assert v['candidate_frames']==75 and v['carved_frames']==0
    original=pixels(source)
    assert pixels(tmp_path/'out'/v['file'])==original[:30]+original[45:]


def test_fragmented_reference_supplies_cadence_without_reference_media(mp4_media,tmp_path):
    source=mp4_media['hevc'];fragmented=tmp_path/'fragmented.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(source),'-map','0:v:0','-c','copy',
        '-movflags','frag_keyframe+empty_moov+default_base_moof',str(fragmented)],capture_output=True,check=True,timeout=20)
    data=bytearray(fragmented.read_bytes());bs,_,_=discover(data)
    for moof in (b for b in bs if b.kind==b'moof'):
        for traf in children(data,moof.payload,moof.end):
            if traf.kind!=b'traf':continue
            run=next(b for b in children(data,traf.payload,traf.end) if b.kind==b'trun')
            struct.pack_into('>I',data,run.payload+4,0xfffffffe)
    path=tmp_path/'lost.mp4';path.write_bytes(data)
    r=recover_mp4(path,tmp_path/'out',fragmented);v=r['videos'][0]
    assert v['candidate_frames']==90 and v['fps']=='30'
    assert pixels(tmp_path/'out'/v['file'])==pixels(source)


@pytest.mark.parametrize('relative', [-170, -1, 0, 30])
def test_chunked_metadata_has_no_boundary_loss_or_duplicates(relative):
    raw=b'$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A'
    data=b'\0'*(1024*1024+relative)+raw+b'\0'*(1024*1024)
    found=list(bounded_matches(NMEA,data,lambda:None))
    assert len(found)==1 and found[0].group()==raw


def test_shared_deadline_terminates_an_actual_blocked_child():
    started=time.monotonic()
    with pytest.raises(RecoveryTimedOut):
        run_command([sys.executable,'-c','import time; time.sleep(30)'],RecoveryDeadline(None,.05))
    assert time.monotonic()-started<3


def test_whole_recovery_timeout_removes_partial_results(mp4_media,tmp_path):
    with pytest.raises(RecoveryTimedOut):
        recover_mp4(mp4_media['hevc'],tmp_path/'out',timeout=.001)
    assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.mp4-recovery-*'))


@pytest.mark.parametrize('value',[0,-1,float('nan'),float('inf'),3601])
def test_invalid_deadline_cannot_disable_limits(mp4_media,tmp_path,value):
    with pytest.raises(ValueError):recover_mp4(mp4_media['avc'],tmp_path/'out',timeout=value)


def test_cli_timeout_exits_without_traceback(mp4_media,tmp_path):
    r=subprocess.run([sys.executable,'-m','core.mp4_recovery',str(mp4_media['avc']),
        str(tmp_path/'out'),'--timeout','.001'],capture_output=True,timeout=5)
    assert r.returncode==2 and b'Traceback' not in r.stderr
    assert not (tmp_path/'out').exists()


# ---------------------------------------------------------------------------
# 2026-10-11 코드 리뷰 반영: #12 #32 #33 #34 #35 #36 #37 #38 #89 #90 #91 #92 #93
# #94 #95 #96 #126 #129 #134. 원칙: 옛 녹화·다른 카메라·잘못 디코딩된 그림이
# passed로 나오면 안 된다. 구별할 수 없으면 보류한다.
# ---------------------------------------------------------------------------
import re
import shutil
from fractions import Fraction

from test_mp4_recovery import ffmpeg, packet_map, video_rate, expected_indices
import core.mp4_recovery as mp4_recovery
from core.mp4_recovery import IDENTICAL_RUN, command_timeout, find_tool
from core.mp4_structure import Box, box_at, child, parse_moov


CONTAINERS = (b'moov', b'trak', b'mdia', b'minf', b'stbl')


def rewrite_box(data, kind, transform):
    """kind Box의 payload를 transform(payload)로 바꾼 파일 바이트(상위 크기 재계산)."""
    out = bytearray()
    for typ, a, b in top_boxes(data):
        payload = data[a+8:b]
        if typ in CONTAINERS:
            payload = rewrite_box(payload, kind, transform)
        elif typ == kind:
            payload = transform(payload)
        out += struct.pack('>I4s', len(payload)+8, typ)+payload
    return bytes(out)


def encode(path, source, seconds, *, codec='libx264', size='160x90', gop=15, audio=True, extra=()):
    args = ['-f', 'lavfi', '-i', f'{source}=size={size}:rate=30']
    if audio:
        args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-c:a', 'aac']
    if codec == 'libx265':
        args += ['-c:v', 'libx265', '-x265-params',
                 f'pools=1:frame-threads=1:keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0:log-level=error']
    else:
        args += ['-c:v', 'libx264', '-g', str(gop), '-keyint_min', str(gop), '-sc_threshold', '0', '-bf', '0']
    ffmpeg(*args, '-t', str(seconds), '-threads', '1', '-pix_fmt', 'yuv420p', *extra, path)
    return path


def fragmented(path, source):
    ffmpeg('-i', source, '-map', '0', '-c', 'copy',
           '-movflags', 'frag_keyframe+empty_moov+default_base_moof', path)
    return path


def moof_sequence(data, moof):
    return int.from_bytes(data[child(data, moof, b'mfhd').payload+4:][:4], 'big')


# ---- #12 ------------------------------------------------------------------
@pytest.mark.parametrize('layout', ['appended', 'from_first_fragment', 'after_garbage', 'tfdt_only'])
def test_stale_fragments_after_recording_end_are_excluded(mp4_media, tmp_path, layout):
    """같은 카메라의 옛 fragment가 녹화 뒤에 남아 있어도 현재 녹화만 복원한다."""
    source = mp4_media['fragmented']; a = bytearray(source.read_bytes())
    old = fragmented(tmp_path/'old_fragmented.mp4', encode(tmp_path/'old.mp4', 'mandelbrot', 3))
    b = bytearray(old.read_bytes()); ba, _, _ = discover(a); bb, _, _ = discover(b)
    moofs_b = [x for x in bb if x.kind == b'moof']
    tail = b[moofs_b[0].start:] if layout == 'from_first_fragment' else b[moofs_b[2].start:]
    if layout == 'tfdt_only':
        # 순번은 이어지지만 tfdt가 되돌아간다: 순번 우연 일치도 녹화 끝으로 본다.
        last = max(moof_sequence(a, x) for x in ba if x.kind == b'moof')
        tail = bytearray(tail)
        for i, m in enumerate(x for x in discover(tail)[0] if x.kind == b'moof'):
            mfhd = child(tail, m, b'mfhd'); struct.pack_into('>I', tail, mfhd.payload+4, last+1+i)
    data = bytes(a) + (b'\xa5' * 777 if layout == 'after_garbage' else b'') + bytes(tail)
    path = tmp_path/'stale.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out')
    assert r['status'] == 'video_recovered', r
    v = r['videos'][0]
    assert v['candidate_frames'] == 90 and v['decode_check']['status'] == 'passed'
    assert pixels(tmp_path/'out'/v['file']) == pixels(source)
    assert r['recording_end_offset'] >= len(a) - 300 and r['video_scan_range']['end'] <= len(a) + 777
    assert any('녹화 끝' in d for d in r['diagnostics'])
    rows = list(csv.DictReader((tmp_path/'out'/'frame_offsets.csv').open(encoding='utf-8-sig')))
    assert all(int(row['source_payload_offset']) < len(a) for row in rows)


def test_fragment_sequence_skip_is_allowed_only_across_damage(mp4_media, tmp_path):
    """손상(빈틈)으로 설명되는 순번 건너뜀은 허용, 손상 없는 건너뜀은 녹화 끝."""
    source = mp4_media['fragmented']; data = bytearray(source.read_bytes()); bs, _, _ = discover(data)
    moofs = [x for x in bs if x.kind == b'moof']
    for m in moofs[3:]:
        mfhd = child(data, m, b'mfhd'); struct.pack_into('>I', data, mfhd.payload+4, moof_sequence(data, m)+1)
    path = tmp_path/'skip.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out')
    assert r['videos'][0]['candidate_frames'] == 45 and r['recording_end_offset'] == moofs[3].start
    # 같은 건너뜀이라도 그 앞 fragment가 손상돼 빈틈이 생겼으면 현재 녹화의 이어짐이다.
    a, b = moofs[2].start, moofs[2].end; data[a:b] = bytes(b-a)
    path = tmp_path/'skip_damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out2')
    assert 'recording_end_offset' not in r and r['videos'][0]['candidate_frames'] == 75
    assert pixels(tmp_path/'out2'/r['videos'][0]['file']) == pixels(source)[:30]+pixels(source)[45:]


# ---- #32 ------------------------------------------------------------------
@pytest.fixture(scope='module')
def large_media(tmp_path_factory):
    root = tmp_path_factory.mktemp('large')
    return {codec: encode(root/f'{codec}.mp4', 'testsrc2', 4, codec=codec, size='960x540')
            for codec in ('libx264', 'libx265')}


@pytest.mark.parametrize('codec', ['libx264', 'libx265'])
def test_exact_gop_sized_zero_loss_waits_for_the_next_idr(large_media, tmp_path, codec):
    """정확히 GOP 하나 길이의 0 채움 손실은 frame_num/POC가 우연히 이어진다. 손실 증거
    (긴 동일 바이트 구간)가 있으면 다음 IDR까지 기다려 잘못 디코딩된 그림을 내지 않는다."""
    source = large_media[codec]; data = bytearray(source.read_bytes()); packets = packet_map(source)
    assert [i for i, p in enumerate(packets[:46]) if 'K' in p['flags']] == [0, 15, 30, 45]
    a = int(packets[20]['pos']); b = int(packets[34]['pos'])+int(packets[34]['size'])
    assert b-a >= IDENTICAL_RUN
    data[a:b] = bytes(b-a)
    moov = next(x for x in top_boxes(data) if x[0] == b'moov')
    path = tmp_path/'gop_lost.mp4'; path.write_bytes(data[:moov[1]])
    r = recover_mp4(path, tmp_path/'out', source)
    v = r['videos'][0]; original = pixels(source)
    expected = [original[i] for i in expected_indices(packets, [(a, b)])]
    assert len(expected) == 120-15-10
    assert pixels(tmp_path/'out'/v['file']) == expected
    assert v['exclusions']['prediction_breaks'] >= 1


def test_lost_reference_excludes_following_non_reference_pictures(tmp_path):
    """참조 P가 사라지면 그것을 참조하는 비참조 B도 frame_num = 직전 참조 + 1 규칙으로 빠진다."""
    source = tmp_path/'bfr.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30', '-t', '2', '-c:v', 'libx264', '-g', '15',
           '-keyint_min', '15', '-sc_threshold', '0', '-bf', '2', '-x264-params', 'b-pyramid=none',
           '-threads', '1', '-pix_fmt', 'yuv420p', source)
    data = bytearray(source.read_bytes()); packets = packet_map(source)
    assert all('K' not in p['flags'] for p in packets[1:15])
    lost = packets[4]; a = int(lost['pos']); b = a+int(lost['size']); data[a:b] = bytes(b-a)
    moov = next(x for x in top_boxes(data) if x[0] == b'moov')
    path = tmp_path/'lost_ref.mp4'; path.write_bytes(data[:moov[1]])
    r = recover_mp4(path, tmp_path/'out', source)
    recovered = pixels(tmp_path/'out'/r['videos'][0]['file']); original = set(pixels(source))
    assert all(h in original for h in recovered), '원본에 없는 그림이 passed로 나왔다'
    assert len(recovered) == len(expected_indices(packets, [(a, b)])) == 60-11


# ---- #33 / #93 ---------------------------------------------------------------
def preallocated_slot(tmp_path, *, with_old_moov):
    """옛 녹화 B가 차지한 슬롯을 새 녹화 A가 앞에서 덮어쓰다 멈춘 파일(mdat 크기 0, A moov 없음)."""
    a = encode(tmp_path/'A.mp4', 'testsrc2', 2).read_bytes()
    b = encode(tmp_path/'B.mp4', 'mandelbrot', 6).read_bytes()
    moov_a = next(x for x in top_boxes(a) if x[0] == b'moov'); mdat_a = next(x for x in top_boxes(a) if x[0] == b'mdat')
    slot = bytearray(b if with_old_moov else b[:next(x for x in top_boxes(b) if x[0] == b'moov')[1]])
    slot[:moov_a[1]] = a[:moov_a[1]]
    struct.pack_into('>I', slot, mdat_a[1], 0)
    path = tmp_path/'slot.mp4'; path.write_bytes(slot)
    return path, tmp_path/'A.mp4', tmp_path/'B.mp4'


@pytest.mark.parametrize('with_old_moov', [False, True])
def test_unbounded_mdat_is_not_a_verified_boundary(tmp_path, with_old_moov):
    path, a, b = preallocated_slot(tmp_path, with_old_moov=with_old_moov)
    reference = encode(tmp_path/'REF.mp4', 'smptebars', 2)
    r = recover_mp4(path, tmp_path/'out', reference)
    assert r['status'] == 'video_recovered', r
    assert r['video_boundary_verified'] is False
    assert r['video_scan_range']['basis'] == 'unverified_mdat_extent'
    v = r['videos'][0]; recovered = pixels(tmp_path/'out'/v['file'])
    assert recovered == pixels(a), '옛 녹화 B의 그림이 섞였다'
    assert v['slack_after_offset'] > 0 and any('슬랙' in d for d in r['diagnostics'])
    assert r['video_scan_range']['unbounded_mdat_offsets']


def test_old_moov_inside_unbounded_mdat_is_not_adopted(tmp_path):
    """크기 0 mdat 안에 남은 옛 녹화의 moov는 범위 검사를 통과해도 바이트가 맞지 않으면 쓰지 않는다."""
    path, a, b = preallocated_slot(tmp_path, with_old_moov=True)
    boxes, _, _ = discover(path.read_bytes())
    assert [x.kind for x in boxes] == [b'ftyp', b'mdat'] or b'moov' not in [x.kind for x in boxes]
    r = recover_mp4(path, tmp_path/'out')
    assert not r['videos'] and '참조' in r['video_withheld_reason']


def test_zero_size_mdat_still_finds_its_own_moov(mp4_media, tmp_path):
    source = mp4_media['avc']; data = bytearray(source.read_bytes())
    mdat = next(x for x in top_boxes(data) if x[0] == b'mdat'); moov = next(x for x in top_boxes(data) if x[0] == b'moov')
    assert moov[1] > mdat[1]
    struct.pack_into('>I', data, mdat[1], 0)
    path = tmp_path/'zero_mdat.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out')
    assert r['status'] == 'video_recovered' and r['videos'][0]['recovery_method'] == 'source_sample_tables'
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(source)


# ---- #34 ------------------------------------------------------------------
def test_damaged_second_camera_track_counts_as_a_channel(tmp_path):
    source = tmp_path/'dual_frag.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30', '-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=30',
           '-t', '3', '-map', '0:v', '-map', '1:v', '-c:v', 'libx264', '-threads', '1', '-g', '15', '-keyint_min', '15',
           '-sc_threshold', '0', '-bf', '0', '-pix_fmt', 'yuv420p',
           '-movflags', 'frag_keyframe+empty_moov+default_base_moof', source)
    data = bytearray(source.read_bytes()); bs, _, _ = discover(data)
    moov = next(x for x in bs if x.kind == b'moov')
    second = [x for x in children(data, moov.payload, moov.end) if x.kind == b'trak'][1]
    data[data.find(b'avcC', second.start, second.end)+4] = 0  # configurationVersion
    moofs = [x for x in bs if x.kind == b'moof']
    for m in moofs[2:4]:
        for traf in children(data, m.payload, m.end):
            if traf.kind != b'traf':
                continue
            sub = children(data, traf.payload, traf.end); tfhd = next(x for x in sub if x.kind == b'tfhd')
            if int.from_bytes(data[tfhd.payload+4:tfhd.payload+8], 'big') == 1:
                run = next(x for x in sub if x.kind == b'trun'); struct.pack_into('>I', data, run.payload+4, 0xfffffffe)
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out')
    assert r['unknown_video_channels'] == ['trak(handler=vide)', 'traf(track_id=2)']
    assert len(r['videos']) == 1 and r['videos'][0]['carved_frames'] == 0
    cam0 = set(pixels(source, 0)); cam1 = set(pixels(source, 1))
    recovered = pixels(tmp_path/'out'/r['videos'][0]['file'])
    assert len(recovered) == 60 and all(h in cam0 and h not in cam1 for h in recovered)
    assert any('구별할 수 없는 영상 채널' in d for d in r['diagnostics'])


# ---- #35 ------------------------------------------------------------------
@pytest.mark.parametrize('settings', ['same', 'different'])
def test_no_moov_and_no_reference_withholds_video(tmp_path, settings):
    source = tmp_path/'dual.mp4'
    extra = ['-profile:v:1', 'baseline', '-crf:v:1', '35'] if settings == 'different' else []
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30', '-f', 'lavfi', '-i', 'smptebars=size=160x90:rate=30',
           '-t', '2', '-map', '0:v', '-map', '1:v', '-c:v', 'libx264', '-threads', '1', '-g', '15', '-keyint_min', '15',
           '-sc_threshold', '0', '-bf', '0', *extra, '-bsf:v', 'dump_extra=freq=keyframe', source)
    data = source.read_bytes(); moov = next(x for x in top_boxes(data) if x[0] == b'moov')
    path = tmp_path/'cut.mp4'; path.write_bytes(data[:moov[1]])
    r = recover_mp4(path, tmp_path/'out')
    assert not r['videos'] and '참조' in r['video_withheld_reason'] and r['status'] == 'nothing_recovered'


def test_in_band_parameter_change_with_reference_is_withheld(tmp_path):
    """참조가 있으면 그 파라미터셋을 고정한다. 본문의 다른 SPS/PPS(설정이 다른 채널)는 보류."""
    source = tmp_path/'dual.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30', '-f', 'lavfi', '-i', 'smptebars=size=160x90:rate=30',
           '-t', '2', '-map', '0:v', '-map', '1:v', '-c:v', 'libx264', '-threads', '1', '-g', '15', '-keyint_min', '15',
           '-sc_threshold', '0', '-bf', '0', '-profile:v:1', 'baseline', '-crf:v:1', '35',
           '-bsf:v', 'dump_extra=freq=keyframe', source)
    reference = tmp_path/'single.mp4'
    ffmpeg('-i', source, '-map', '0:v:0', '-c', 'copy', reference)
    data = source.read_bytes(); moov = next(x for x in top_boxes(data) if x[0] == b'moov')
    path = tmp_path/'cut.mp4'; path.write_bytes(data[:moov[1]])
    r = recover_mp4(path, tmp_path/'out', reference)
    assert not r['videos'] and any('parameters changed' in d for d in r['diagnostics'])


# ---- #36 ------------------------------------------------------------------
@pytest.fixture(scope='module')
def long_avc(tmp_path_factory):
    return encode(tmp_path_factory.mktemp('long')/'long.mp4', 'testsrc2', 6)


def corrupt_packet(data, packet):
    a, size = int(packet['pos']), int(packet['size'])
    data[a+128:a+size] = b'\xff' * (size-128)


def test_gop_bisection_keeps_healthy_gops_within_attempt_limit(long_avc, tmp_path, monkeypatch):
    data = bytearray(long_avc.read_bytes()); packets = packet_map(long_avc)
    assert len(packets) == 180
    corrupt_packet(data, packets[68])
    monkeypatch.setattr(mp4_recovery, 'MAX_DECODE_ATTEMPTS', 12)
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out'); v = r['videos'][0]
    assert v['file'] and v['decode_check']['status'] == 'passed' and v['verification_limit'] is None
    assert v['decode_attempts'] <= 12
    original = pixels(long_avc)
    assert pixels(tmp_path/'out'/v['file']) == original[:68]+original[75:]


def test_verification_limit_keeps_validated_survivors(long_avc, tmp_path, monkeypatch):
    data = bytearray(long_avc.read_bytes()); packets = packet_map(long_avc)
    corrupt_packet(data, packets[68]); corrupt_packet(data, packets[140])
    monkeypatch.setattr(mp4_recovery, 'MAX_DECODE_ATTEMPTS', 5)
    path = tmp_path/'damaged.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out'); v = r['videos'][0]
    assert v['verification_limit']['status'] == 'verification_limit'
    assert v['file'] and v['decode_check']['status'] == 'passed' and v['candidate_frames'] >= 15
    original = set(pixels(long_avc))
    assert all(h in original for h in pixels(tmp_path/'out'/v['file']))
    assert any('검증 한도' in g.get('reason', '') for g in v['discarded_gops'])


# ---- #37 ------------------------------------------------------------------
@pytest.fixture(scope='module')
def sliced_media(tmp_path_factory):
    root = tmp_path_factory.mktemp('sliced'); result = {}
    for codec in ('libx264', 'libx265'):
        extra = ['-slices', '4'] if codec == 'libx264' else []
        path = root/f'{codec}.mp4'
        args = ['-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30', '-f', 'lavfi', '-i',
                'sine=frequency=440:sample_rate=48000', '-t', '1', '-c:a', 'aac']
        if codec == 'libx265':
            args += ['-c:v', 'libx265', '-x265-params',
                     'pools=1:frame-threads=1:keyint=15:min-keyint=15:scenecut=0:bframes=0:slices=4:log-level=error']
        else:
            args += ['-c:v', 'libx264', '-g', '15', '-keyint_min', '15', '-sc_threshold', '0', '-bf', '0', *extra]
        ffmpeg(*args, '-threads', '1', '-pix_fmt', 'yuv420p', path)
        result[codec] = path
    return result


def vcl_nals(data, packet, codec):
    off, size = int(packet['pos']), int(packet['size']); p = off; out = []
    while p < off+size:
        n = int.from_bytes(data[p:p+4], 'big'); head = data[p+4]
        kind = (head >> 1) & 63 if codec == 'libx265' else head & 31
        if (kind < 32) if codec == 'libx265' else kind in (1, 5):
            out.append((p+4, n))
        p += 4+n
    return out


@pytest.mark.parametrize('codec', ['libx264', 'libx265'])
@pytest.mark.parametrize('damage', ['none', 'last_slice', 'middle_slice'])
def test_multi_slice_pictures_need_every_contiguous_slice(sliced_media, tmp_path, codec, damage):
    source = sliced_media[codec]; data = bytearray(source.read_bytes()); packets = packet_map(source)
    slices = vcl_nals(data, packets[7], codec)
    assert len(slices) == 4 and any(n < 16 for _, n in vcl_nals(data, packets[8], codec)) or len(slices) == 4
    damaged = []
    if damage != 'none':
        start, n = slices[-1] if damage == 'last_slice' else slices[1]
        a = start+n//2; data[a:a+8] = bytes(8); damaged = [(a, a+8)]
    moov = next(x for x in top_boxes(data) if x[0] == b'moov')
    path = tmp_path/'cut.mp4'; path.write_bytes(data[:moov[1]])
    r = recover_mp4(path, tmp_path/'out', source); v = r['videos'][0]
    original = pixels(source); expected = [original[i] for i in expected_indices(packets, damaged)]
    assert len(expected) == (30 if damage == 'none' else 22)
    assert pixels(tmp_path/'out'/v['file']) == expected, '슬라이스가 빠진 그림이 passed로 나왔다'


# ---- #38 ------------------------------------------------------------------
@pytest.mark.parametrize('fake', ['vps', 'sei_long', 'filler_long'])
def test_fake_nal_candidates_inside_audio_do_not_skip_or_withhold(mp4_media, tmp_path, fake):
    source = mp4_media['hevc']; data = bytearray(source.read_bytes())
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_packets',
        '-show_entries', 'packet=pos,size', '-of', 'json', str(source)], capture_output=True, check=True, timeout=20)
    audio = json.loads(probe.stdout)['packets']
    for packet in audio[3:9]:
        at = int(packet['pos'])+8
        assert int(packet['size']) > 16
        planted = {'vps': b'\x00\x00\x00\x40\x40\x01', 'sei_long': b'\x00\x00\x4e\x20\x4e\x01',
                   'filler_long': b'\x00\x00\x4e\x20\x4c\x01'}[fake]
        data[at:at+len(planted)] = planted
    moov = next(x for x in top_boxes(data) if x[0] == b'moov')
    path = tmp_path/'cut.mp4'; path.write_bytes(data[:moov[1]])
    r = recover_mp4(path, tmp_path/'out', source)
    assert r['status'] == 'video_recovered', r
    v = r['videos'][0]
    assert v['candidate_frames'] == 90 and 'withheld' not in v['exclusions']
    assert pixels(tmp_path/'out'/v['file']) == pixels(source)


# ---- #89 ------------------------------------------------------------------
def test_command_timeout_comes_from_the_remaining_total_budget():
    assert command_timeout(RecoveryDeadline(None, 600)) > 120
    assert command_timeout(RecoveryDeadline(None, 600), 5) <= 5
    assert command_timeout(None) == 120 and command_timeout(None, 7) == 7
    assert command_timeout(RecoveryDeadline(None, .05)) <= .05


# ---- #90 / #91 ---------------------------------------------------------------
def test_trailing_zero_duration_keeps_sample_table(mp4_media, tmp_path):
    source = mp4_media['avc']; data = source.read_bytes()
    def split(payload):  # 영상 stts(90 샘플 한 run)만 바꾼다. 음성 stts는 그대로.
        count, delta = struct.unpack_from('>II', payload, 8)
        if struct.unpack_from('>I', payload, 4)[0] != 1 or count != 90:
            return payload
        return payload[:4]+struct.pack('>IIIII', 2, count-1, delta, 1, 0)
    path = tmp_path/'vfr_tail.mp4'; path.write_bytes(rewrite_box(data, b'stts', split))
    r = recover_mp4(path, tmp_path/'out'); v = r['videos'][0]
    assert v['recovery_method'] == 'source_sample_tables' and v['candidate_frames'] == 90
    assert pixels(tmp_path/'out'/v['file']) == pixels(source) and video_rate(tmp_path/'out'/v['file']) == 30


def test_single_odd_duration_uses_nominal_fps(mp4_media, tmp_path):
    source = mp4_media['avc']; data = source.read_bytes()
    def odd(payload):
        count, delta = struct.unpack_from('>II', payload, 8)
        if struct.unpack_from('>I', payload, 4)[0] != 1 or count != 90:
            return payload
        return payload[:4]+struct.pack('>IIIIIII', 3, 45, delta, 1, delta+1, count-46, delta)
    path = tmp_path/'odd.mp4'; path.write_bytes(rewrite_box(data, b'stts', odd))
    boxes, _, _ = discover(path.read_bytes())
    track = parse_moov(path.read_bytes(), next(b for b in boxes if b.kind == b'moov'))[0][0]
    assert track.fps == 30
    r = recover_mp4(path, tmp_path/'out'); v = r['videos'][0]
    assert v['file'] and v['fps'] == '30' and pixels(tmp_path/'out'/v['file']) == pixels(source)


# ---- #92 ------------------------------------------------------------------
def test_missing_tfdt_without_reordering_still_muxes(mp4_media, tmp_path):
    source = mp4_media['fragmented']; data = bytearray(source.read_bytes()); bs, _, _ = discover(data)
    moof = [x for x in bs if x.kind == b'moof'][2]
    for traf in children(data, moof.payload, moof.end):
        if traf.kind == b'traf':
            tfdt = child(data, traf, b'tfdt'); data[tfdt.start+4:tfdt.start+8] = b'free'
    path = tmp_path/'no_tfdt.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out'); v = r['videos'][0]
    assert v['file'] and v['candidate_frames'] == 90 and v['decode_check']['status'] == 'passed'
    assert pixels(tmp_path/'out'/v['file']) == pixels(source)


# ---- #94 ------------------------------------------------------------------
def test_ctts_version0_negative_offsets_are_signed(mp4_media, tmp_path):
    source = mp4_media['bframes']; data = source.read_bytes()
    seen = {}
    def negative(payload):
        seen['version'] = payload[0]; n = struct.unpack_from('>I', payload, 4)[0]
        entries = [struct.unpack_from('>Ii', payload, 8+i*8) for i in range(n)]
        shift = max(v for _, v in entries)//2 or 1
        return bytes(4)+struct.pack('>I', n)+b''.join(struct.pack('>Ii', c, v-shift) for c, v in entries)
    path = tmp_path/'negative.mp4'; path.write_bytes(rewrite_box(data, b'ctts', negative))
    assert seen['version'] == 0
    # FFmpeg도 version 0 값을 부호 있는 수로 읽는다(음수 PTS 앞 그림만 CLI가 떨어뜨린다).
    assert pixels(path) == pixels(source)[2:]
    r = recover_mp4(path, tmp_path/'out'); v = r['videos'][0]
    assert v['candidate_frames'] == 90 and pixels(tmp_path/'out'/v['file']) == pixels(source)


# ---- #95 ------------------------------------------------------------------
def test_hevc_b_pictures_without_ctts_are_withheld_with_reason(mp4_media, tmp_path):
    source = tmp_path/'b.mp4'
    ffmpeg('-i', mp4_media['fragmented_hevc_bframes'], '-map', '0:v:0', '-c', 'copy', source)
    data = bytearray(source.read_bytes()); p = data.find(b'ctts'); assert p > 0
    data[p:p+4] = b'free'
    path = tmp_path/'no_ctts.mp4'; path.write_bytes(data)
    r = recover_mp4(path, tmp_path/'out'); v = r['videos'][0]
    assert v['file'] is None and 'B-picture' in v['decode_check']['reason']
    assert r['status'] != 'video_recovered'


# ---- #96 ------------------------------------------------------------------
@pytest.mark.parametrize('moov', ['intact', 'cut'])
def test_unlisted_top_level_box_touching_next_box_is_opaque(mp4_media, tmp_path, moov):
    source = mp4_media['avc']; data = source.read_bytes()
    boxes = top_boxes(data); mdat = next(x for x in boxes if x[0] == b'mdat'); mv = next(x for x in boxes if x[0] == b'moov')
    assert mdat[2] == mv[1]
    opaque = struct.pack('>I4s', 12, b'ICAT')+bytes(4)
    data = data[:mdat[2]]+opaque+(data[mv[1]:] if moov == 'intact' else b'')
    path = tmp_path/'icat.mp4'; path.write_bytes(data)
    found, _, _ = discover(data)
    assert [x.kind for x in found if x.kind in (b'ICAT', b'mdat')] == [b'mdat', b'ICAT']
    r = recover_mp4(path, tmp_path/'out', None if moov == 'intact' else source)
    assert r['status'] == 'video_recovered' and not r['damaged_box_gaps']
    assert pixels(tmp_path/'out'/r['videos'][0]['file']) == pixels(source)


def test_unlisted_box_not_touching_a_known_box_remains_damage(mp4_media, tmp_path):
    data = mp4_media['avc'].read_bytes(); boxes = top_boxes(data); mdat = next(x for x in boxes if x[0] == b'mdat')
    bad = struct.pack('>I4s', 20, b'ICAT')+bytes(4)
    data = data[:mdat[2]]+bad+data[mdat[2]:]
    found, _, gaps = discover(data)
    assert b'ICAT' not in [x.kind for x in found] and gaps


# ---- #129 -----------------------------------------------------------------
class BitWriter:
    def __init__(self):
        self.bits = []

    def u(self, value, width):
        self.bits += [(value >> (width-1-i)) & 1 for i in range(width)]

    def ue(self, value):
        code = value+1; width = code.bit_length(); self.u(0, width-1); self.u(code, width)

    def se(self, value):
        self.ue(2*value-1 if value > 0 else -2*value)

    def bytes(self):
        bits = self.bits+[1]; bits += [0]*(-len(bits) % 8)
        return bytes(int(''.join(map(str, bits[i:i+8])), 2) for i in range(0, len(bits), 8))


def test_avc_poc_lsb_is_separate_from_bottom_field_delta():
    from core.mp4_recovery import AVC
    sps = BitWriter(); sps.u(66, 8); sps.u(0, 8); sps.u(30, 8); sps.ue(0)
    sps.ue(0); sps.ue(0); sps.ue(4); sps.ue(1); sps.u(0, 1); sps.ue(9); sps.ue(5); sps.u(1, 1); sps.u(1, 1); sps.u(0, 1); sps.u(0, 1)
    pps = BitWriter(); pps.ue(0); pps.ue(0); pps.u(0, 1); pps.u(1, 1); pps.ue(0); pps.ue(0); pps.ue(0); pps.u(0, 1); pps.u(0, 2)
    pps.se(0); pps.se(0); pps.se(0); pps.u(0, 1); pps.u(0, 1); pps.u(0, 1)
    avc = AVC([b'\x67'+sps.bytes(), b'\x68'+pps.bytes()])
    assert avc.pps[0]['bottom'] == 1
    slice_header = BitWriter(); slice_header.ue(0); slice_header.ue(0); slice_header.ue(0); slice_header.u(3, 4)
    slice_header.u(5, 8); slice_header.se(-3); slice_header.u(0, 8)
    first, key, frame, modulus, ref, kind, poc_lsb = avc.picture(b'\x41'+slice_header.bytes()+bytes(8))
    assert (first, frame, modulus, ref, poc_lsb) == (0, 3, 16, True, 5)
    assert key[-1] == -3, 'bottom 플래그가 켜진 PPS에서는 key[-1]이 delta다'


# ---- #134 -----------------------------------------------------------------
def test_executables_under_cwd_or_evidence_folders_are_rejected(tmp_path, monkeypatch):
    evidence = tmp_path/'evidence'; evidence.mkdir(); planted = evidence/'ffmpeg'; planted.write_text('')
    deadline = RecoveryDeadline(None, 10, protected_dirs=(evidence,))
    monkeypatch.setattr(mp4_recovery.shutil, 'which', lambda name: str(planted))
    assert find_tool('ffmpeg', deadline) is None
    monkeypatch.setattr(mp4_recovery.shutil, 'which', lambda name: str(Path.cwd()/'ffmpeg'))
    assert find_tool('ffmpeg', None) is None
    elsewhere = tmp_path/'tools'/'ffmpeg'; elsewhere.parent.mkdir(); elsewhere.write_text('')
    monkeypatch.setattr(mp4_recovery.shutil, 'which', lambda name: str(elsewhere))
    assert find_tool('ffmpeg', deadline) == str(elsewhere)


def test_planted_tool_in_evidence_folder_is_not_run(mp4_media, tmp_path, monkeypatch):
    planted = tmp_path/'ffmpeg'; planted.write_text('#!/bin/sh\nexit 0\n'); planted.chmod(0o755)
    source = tmp_path/'source.mp4'; shutil.copyfile(mp4_media['avc'], source)
    original_which = shutil.which
    monkeypatch.setattr(mp4_recovery.shutil, 'which', lambda name: str(planted) if name == 'ffmpeg' else original_which(name))
    r = recover_mp4(source, tmp_path/'out')
    assert r['verification_tools']['ffmpeg']['available'] is False
    assert r['verification_tools']['ffmpeg']['rejected_path'] == str(planted)
    assert r['videos'][0]['decode_check']['status'] == 'not_run' and r['status'] == 'video_candidates_unverified'
