"""전방/후방 영상 파일 짝 찾기.

대부분의 블랙박스는 같은 이름에 F/R(또는 front/rear)만 다른 파일을 나란히 쓴다:
  EVT_20240618_184124_F.avi  ↔  EVT_20240618_184124_R.avi   (VUGERA, INAVI)
  20260812-10h55m02s_N.avi   ↔  20260812-10h55m02s_R.avi    (FineVu: N=전방, R=후방)
전방 파일을 고르면 같은 폴더에서 이 규칙으로 후방 후보를 찾아 파일 선택 창의 기본값으로 준다.
못 찾으면 사용자가 직접 고른다.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
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


# ---------- 전방/후방이 같은 녹화인지 검사 ----------
# 전혀 다른 영상을 전·후방으로 넣으면 나란히 재생이 뒤죽박죽이 되고 리포트 근거도 흐려진다.
# 같은 녹화라면 길이가 거의 같고(같은 순간 시작·종료) 컨테이너에 적힌 녹화 시각도 몇 초 안이다.
DURATION_TOLERANCE_SEC = 2.0
DURATION_TOLERANCE_RATIO = 0.05
RECORDED_AT_TOLERANCE_SEC = 10.0


@dataclass
class PairCheck:
    front_path: str
    rear_path: str
    front_duration: Optional[float] = None
    rear_duration: Optional[float] = None
    front_recorded_at: Optional[float] = None   # Unix epoch 초 (MP4만)
    rear_recorded_at: Optional[float] = None
    same_folder: bool = False
    name_pair: bool = False
    problems: List[str] = field(default_factory=list)   # 하나라도 있으면 후방으로 받지 않는다
    notes: List[str] = field(default_factory=list)      # 참고 문구

    @property
    def ok(self) -> bool:
        return not self.problems


def _fmt_dur(sec: Optional[float]) -> str:
    if sec is None:
        return "알 수 없음"
    m, s = divmod(int(round(sec)), 60)
    return f"{m}분 {s:02d}초" if m else f"{s}초"


def _fmt_epoch(epoch: Optional[float]) -> str:
    if epoch is None:
        return "알 수 없음"
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def compare_pair(front_path: str, rear_path: str) -> PairCheck:
    """전방·후방 파일이 같은 녹화인지. 길이 차이와 녹화 시각 차이가 허용치를 넘으면 problems에
    적힌다. 파일명 짝·같은 폴더 여부는 막는 조건이 아니라 참고 문구다(기기마다 규칙이 달라서)."""
    from core import duration as _duration
    from core.format_sniffer import CONTAINER_MP4, sniff

    check = PairCheck(front_path=front_path, rear_path=rear_path)
    check.same_folder = os.path.dirname(os.path.abspath(front_path)) == os.path.dirname(os.path.abspath(rear_path))
    check.name_pair = os.path.basename(rear_path).lower() in {
        os.path.basename(c).lower() for c in rear_candidates(front_path)}

    def probe(path: str):
        """(길이, 녹화 시각). 못 읽으면 None - 비교를 건너뛴다. 녹화 시각은 MP4 컨테이너에만
        있으므로 AVI에는 MP4 파서를 돌리지 않는다(엉뚱한 경고만 난다)."""
        try:
            routing = sniff(path)
            dur = _duration.get_duration_sec(path, routing.container)
            rec = _duration.mp4_recorded_at_epoch(path) if routing.container == CONTAINER_MP4 else None
            return dur, rec
        except Exception:  # noqa: BLE001
            return None, None

    check.front_duration, check.front_recorded_at = probe(front_path)
    check.rear_duration, check.rear_recorded_at = probe(rear_path)

    if check.front_duration is not None and check.rear_duration is not None:
        diff = abs(check.front_duration - check.rear_duration)
        allowed = max(DURATION_TOLERANCE_SEC, DURATION_TOLERANCE_RATIO * max(check.front_duration, check.rear_duration))
        if diff > allowed:
            check.problems.append(
                f"영상 길이가 다릅니다: 전방 {_fmt_dur(check.front_duration)}, 후방 {_fmt_dur(check.rear_duration)}")
    else:
        check.notes.append("영상 길이를 읽지 못해 길이는 비교하지 않았습니다.")

    if check.front_recorded_at is not None and check.rear_recorded_at is not None:
        diff = abs(check.front_recorded_at - check.rear_recorded_at)
        if diff > RECORDED_AT_TOLERANCE_SEC:
            check.problems.append(
                f"녹화 시각이 다릅니다: 전방 {_fmt_epoch(check.front_recorded_at)}, "
                f"후방 {_fmt_epoch(check.rear_recorded_at)}")
    else:
        check.notes.append("컨테이너에 녹화 시각이 없어 시각은 비교하지 않았습니다.")

    if not check.same_folder:
        check.notes.append("두 파일이 다른 폴더에 있습니다.")
    if not check.name_pair:
        check.notes.append("파일명이 전방/후방 짝 규칙(_F↔_R 등)과 맞지 않습니다.")
    return check
