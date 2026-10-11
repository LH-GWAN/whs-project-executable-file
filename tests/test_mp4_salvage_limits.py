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
