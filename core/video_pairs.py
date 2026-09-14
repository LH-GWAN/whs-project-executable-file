"""전방/후방 영상 파일 짝 찾기.

대부분의 블랙박스는 같은 이름에 F/R(또는 front/rear)만 다른 파일을 나란히 쓴다:
  EVT_20240618_184124_F.avi  ↔  EVT_20240618_184124_R.avi   (VUGERA, INAVI)
  20260812-10h55m02s_N.avi   ↔  20260812-10h55m02s_R.avi    (FineVu: N=전방, R=후방)
전방 파일을 고르면 같은 폴더에서 이 규칙으로 후방 후보를 찾아 파일 선택 창의 기본값으로 준다.
못 찾으면 사용자가 직접 고른다.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional

# (전방 표기 정규식, 후방 표기 후보들) - 확장자 앞 끝부분만 본다.
_PAIR_RULES = (
    (re.compile(r"([_\-])F$", re.IGNORECASE), ["{sep}R"]),
    (re.compile(r"([_\-])N$", re.IGNORECASE), ["{sep}R"]),
    (re.compile(r"([_\-])(front|fr)$", re.IGNORECASE), ["{sep}rear", "{sep}Rear", "{sep}REAR", "{sep}R", "{sep}back"]),
    (re.compile(r"F$", re.IGNORECASE), ["R"]),
)


def rear_candidates(front_path: str) -> List[str]:
    """후방 파일로 볼 만한 경로 후보(존재 여부와 무관). 앞쪽일수록 그럴듯한 것."""
    folder, name = os.path.split(front_path)
    stem, ext = os.path.splitext(name)
    out: List[str] = []
    for pattern, replacements in _PAIR_RULES:
        m = pattern.search(stem)
        if not m:
            continue
        sep = m.group(1) if m.groups() else ""
        for rep in replacements:
            new_stem = stem[:m.start()] + rep.format(sep=sep)
            for e in (ext, ext.lower(), ext.upper()):
                candidate = os.path.join(folder, new_stem + e)
                if candidate not in out and candidate != front_path:
                    out.append(candidate)
    return out


def find_rear_sibling(front_path: str) -> Optional[str]:
    """실제로 존재하는 후방 짝 파일. 없으면 None."""
    for candidate in rear_candidates(front_path):
        if os.path.isfile(candidate):
            return candidate
    # 대소문자가 다른 파일 시스템(macOS/Windows) 대비: 폴더를 훑어 이름만 비교
    folder = os.path.dirname(front_path)
    try:
        names = {n.lower(): n for n in os.listdir(folder)}
    except OSError:
        return None
    for candidate in rear_candidates(front_path):
        hit = names.get(os.path.basename(candidate).lower())
        if hit:
            return os.path.join(folder, hit)
    return None
