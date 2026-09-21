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
    # hdrl LIST는 파일 앞쪽에 있다. 앞 2MB 안의 'strh' 청크 중 fccType 'vids'를 센다.
    with open(path, "rb") as f:
        head = f.read(2 * 1024 * 1024)
    count = 0
    pos = 0
    while True:
        pos = head.find(b"strh", pos)
        if pos < 0:
            break
        if head[pos + 8:pos + 12] == b"vids":
            count += 1
        pos += 4
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
