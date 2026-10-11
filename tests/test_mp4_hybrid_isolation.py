"""Untrusted orphan media must not erase independently indexed source pictures."""
import struct
import subprocess

import pytest

from core.mp4_recovery import recover_mp4
from core.mp4_structure import children, discover, parse_moov
from test_mp4_recovery import mp4_media, packet_map, pixels


@pytest.fixture(scope='module')
def hybrid_media(mp4_media, tmp_path_factory):
    root = tmp_path_factory.mktemp('hybrid')
    hevc = root/'fragmented_hevc.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(mp4_media['hevc']),
        '-map','0','-c','copy','-movflags','frag_keyframe+empty_moov+default_base_moof',str(hevc)],
        capture_output=True,check=True,timeout=20)
    other = root/'other_hevc.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=red:size=128x96:rate=30',
        '-t','0.1','-c:v','libx265','-threads','1','-x265-params',
        'pools=1:frame-threads=1:bframes=0:log-level=error',str(other)],
        capture_output=True,check=True,timeout=20)
    result = {}
    for codec, source, foreign in [('h264',mp4_media['fragmented'],mp4_media['wrong']),
                                   ('hevc',hevc,other)]:
        same_size = root/('same_size_'+codec+'.mp4')
        encoder = 'libx264' if codec=='h264' else 'libx265'
        options = ['-bf','0'] if codec=='h264' else ['-x265-params',
            'pools=1:frame-threads=1:bframes=0:log-level=error']
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
            'color=red:size=160x90:rate=25','-t','0.1','-c:v',encoder,'-threads','1',
            *options,str(same_size)],capture_output=True,check=True,timeout=20)
        prefixes = {}
        for dimensions, candidate in [('different',foreign),('same',same_size)]:
            data = candidate.read_bytes(); bs,_,_ = discover(data)
            tracks,_,_ = parse_moov(data,next(b for b in bs if b.kind==b'moov'))
            assert (tracks[0].width,tracks[0].height)==((160,90) if dimensions=='same' else (128,96))
            parameter = next(n for n in tracks[0].parameters
                             if (n[0]&31==7 if codec=='h264' else (n[0]>>1)&63==33))
            prefixes[dimensions] = len(parameter).to_bytes(4,'big')+parameter
        result[codec] = (source, prefixes)
    return result


def remove_video_run(data, moof):
    for traf in children(data,moof.payload,moof.end):
        if traf.kind != b'traf':
            continue
        sub = children(data,traf.payload,traf.end)
        header = next(b for b in sub if b.kind==b'tfhd')
        if int.from_bytes(data[header.payload+4:header.payload+8],'big')==1:
            run = next(b for b in sub if b.kind==b'trun')
            struct.pack_into('>I',data,run.payload+4,0xfffffffe)


@pytest.mark.parametrize('codec',['h264','hevc'])
@pytest.mark.parametrize('collision',['early','late','second_range','first_range'])
@pytest.mark.parametrize('dimensions',['different','same'])
def test_foreign_orphan_parameters_keep_verified_indexed_frames(hybrid_media,tmp_path,codec,collision,dimensions):
    source, prefixes = hybrid_media[codec]; prefix = prefixes[dimensions]
    data = bytearray(source.read_bytes()); bs,_,_ = discover(data)
    moofs = [b for b in bs if b.kind==b'moof']
    lost = 4 if collision=='second_range' else 2
    remove_video_run(data,moofs[lost])
    if collision in ('second_range','first_range'):
        remove_video_run(data,moofs[2 if collision=='second_range' else 4])
    mdat = next(b for b in bs if b.kind==b'mdat' and b.start==moofs[lost].end)
    at = int(packet_map(source)[44]['pos']) if collision=='late' else mdat.payload
    assert at+len(prefix) < mdat.end
    data[at:at+len(prefix)] = prefix
    path = tmp_path/'mixed.mp4'; path.write_bytes(data)
    r = recover_mp4(path,tmp_path/'out')
    assert r['status']=='video_recovered',r
    v = r['videos'][0]
    assert v['candidate_frames']==75 and v['decode_check']['status']=='passed'
    original = pixels(source)
    expected = original[:60]+original[75:] if collision=='second_range' else original[:30]+original[45:]
    assert pixels(tmp_path/'out'/v['file'])==expected
    assert v['carved_frames']==(15 if collision in ('second_range','first_range') else 0)
    assert len(v['withheld_unindexed_media_ranges'])==1
    assert v['withheld_unindexed_media_ranges'][0]['start']==mdat.payload
    assert v['payload_check']['status']=='passed'


@pytest.mark.parametrize('codec',['h264','hevc'])
def test_fully_unindexed_foreign_parameters_remain_withheld(hybrid_media,tmp_path,codec):
    source, prefixes = hybrid_media[codec]; prefix = prefixes['different']
    data = bytearray(source.read_bytes()); bs,_,_ = discover(data)
    moofs = [b for b in bs if b.kind==b'moof']
    for moof in moofs:
        remove_video_run(data,moof)
    at = int(packet_map(source)[44]['pos'])
    data[at:at+len(prefix)] = prefix
    path = tmp_path/'unindexed.mp4';path.write_bytes(data)
    r = recover_mp4(path,tmp_path/'out',source)
    assert not r['videos']
    assert any('parameters changed' in s or '다른 해상도' in s for s in r['diagnostics'])
