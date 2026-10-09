"""영상 파일 안의 비디오 트랙 수.

어떤 블랙박스(랜드로버 순정 등)는 전방·후방을 파일 하나에 비디오 트랙 둘로 같이 넣는다.
파일을 고르는 시점에 이를 알아야 "같이 볼지 / 전방만 / 후방만"을 물어볼 수 있다. 재생기
(QMediaPlayer)는 파일을 로드해야 트랙 수를 알려 주므로 컨테이너를 직접 읽는다.
  MP4: moov/trak/mdia/hdlr 의 handler_type == 'vide' 인 trak 수
  AVI: hdrl/strl/strh 의 fccType == 'vids' 인 스트림 수
읽지 못하면 0(모름)으로 돌려주고, 호출하는 쪽은 트랙 하나로 취급한다.
"""
from __future__ import annotations

import os
import struct

TRACK_MODE_BOTH = "both"     # 전방(1번 트랙) 왼쪽 + 후방(2번 트랙) 오른쪽
TRACK_MODE_FRONT = "front"   # 1번 트랙만
TRACK_MODE_REAR = "rear"     # 2번 트랙만
TRACK_MODES = (TRACK_MODE_BOTH, TRACK_MODE_FRONT, TRACK_MODE_REAR)

_MP4_CONTAINERS = ("moov", "trak", "mdia")


def _mp4_video_tracks(path: str) -> int:
    size = os.path.getsize(path)
    count = 0

    def walk(f, start: int, end: int, depth: int) -> None:
        nonlocal count
        pos = start
        while pos + 8 <= end:
            f.seek(pos)
            header = f.read(8)
            if len(header) < 8:
                return
            box_size, box_type = struct.unpack(">I4s", header)
            header_len = 8
            if box_size == 1:
                box_size = struct.unpack(">Q", f.read(8))[0]
                header_len = 16
            elif box_size == 0:
                box_size = end - pos
            if box_size < header_len:
                return
            name = box_type.decode("latin1", "replace")
            if name in _MP4_CONTAINERS and depth < 3:
                walk(f, pos + header_len, min(pos + box_size, end), depth + 1)
            elif name == "hdlr" and depth == 3:
                f.seek(pos + header_len + 8)   # version/flags(4) + pre_defined(4)
                if f.read(4) == b"vide":
                    count += 1
            pos += box_size
            if name == "moov":
                return  # moov 하나면 충분하다(뒤의 mdat은 읽지 않는다)

    with open(path, "rb") as f:
        walk(f, 0, size, 0)
    return count


def _avi_video_tracks(path: str) -> int:
    """첫 RIFF의 hdrl LIST 안 strl/strh 중 fccType 'vids'만 센다. 앞 2MB를 통째로 문자열 검색하면 뒤에
    이어붙은 옛 녹화 RIFF의 strh까지 세어 1트랙 파일이 2트랙으로 보였다(리뷰 #101)."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF":
            return 0
        riff_end = min(8 + struct.unpack("<I", head[4:8])[0], os.path.getsize(path))
        pos = 12
        count = 0
        while pos + 12 <= riff_end:
            f.seek(pos)
            hdr = f.read(12)
            if len(hdr) < 12:
                break
            ck_id, ck_size, list_type = hdr[:4], struct.unpack("<I", hdr[4:8])[0], hdr[8:12]
            if ck_id == b"LIST" and list_type == b"hdrl":
                hdrl_start, hdrl_end = pos + 12, min(pos + 8 + ck_size, riff_end)
                q = hdrl_start
                while q + 8 <= hdrl_end:
                    f.seek(q)
                    sub = f.read(12)
                    if len(sub) < 8:
                        break
                    sub_id, sub_size = sub[:4], struct.unpack("<I", sub[4:8])[0]
                    if sub_id == b"LIST" and sub[8:12] == b"strl":
                        f.seek(q + 12)
                        strh = f.read(12)
                        if strh[:4] == b"strh" and strh[8:12] == b"vids":
                            count += 1
                    q += 8 + sub_size + (sub_size & 1)
                return count
            if ck_id == b"LIST" and list_type == b"movi":
                break
            pos += 8 + ck_size + (ck_size & 1)
    return count


def count_video_tracks(path: str) -> int:
    """비디오 트랙 수. 못 읽으면 0."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
            return _avi_video_tracks(path)
        return _mp4_video_tracks(path)
    except (OSError, struct.error, ValueError):
        return 0


def has_dual_video_tracks(path: str) -> bool:
    return count_video_tracks(path) >= 2


def view_tag(track_mode: str, has_separate_rear: bool = False) -> str:
    """History·파일 정보 줄에 붙는 보기 표기. F=전방, B=후방.
    2트랙 파일: both → "F, B", front → "F", rear → "B". 후방을 별도 파일로 붙인 사건도 "F, B".
    보통 전방 파일 하나면 빈 문자열(표기 없음)."""
    if track_mode == TRACK_MODE_BOTH:
        return "F, B"
    if track_mode == TRACK_MODE_FRONT:
        return "F"
    if track_mode == TRACK_MODE_REAR:
        return "B"
    return "F, B" if has_separate_rear else ""


def same_view(mode_a: str, mode_b: str) -> bool:
    """같은 파일이라도 보기 방식이 다르면(전방만 vs 후방만) 다른 분석으로 본다 - '이미 분석한
    파일' 경고는 보기 방식까지 같을 때만 낸다."""
    return (mode_a or "") == (mode_b or "")
