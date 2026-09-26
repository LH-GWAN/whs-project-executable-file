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
import shutil
import statistics
import struct
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# (전방 표기 정규식, 후방 표기 후보들) - 확장자 앞 끝부분만 본다.
_PAIR_RULES = (
    (re.compile(r"([_\-])F(?P<suffix>[_\-]\d+)$", re.IGNORECASE), ["{sep}R{suffix}"]),
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
            new_stem = stem[:m.start()] + rep.format(sep=sep, suffix=m.groupdict().get("suffix", ""))
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
# GPS 대조: 같은 녹화면 첫 GPS 시각이 거의 같고(같은 수신기), 같은 시각의 좌표는 같다.
# 4번 샘플(랜드로버) 연속 파일 둘은 mvhd 시각·길이·기기 정보가 전부 같아서 GPS 시각(60초 차이)
# 으로만 걸러진다(검토 제보 "다른 영상인데 통과").
GPS_START_TOLERANCE_SEC = 3.0
COORD_TOLERANCE_M = 50.0
MIN_COORD_MATCHES = 3
FILENAME_TIME_TOLERANCE_SEC = 3.0


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
    front_signature: Dict[str, str] = field(default_factory=dict)   # 기기 정보(컨테이너 서명)
    rear_signature: Dict[str, str] = field(default_factory=dict)
    # GPS 대조 결과 (compare_pair_full)
    gps_checked: bool = False
    front_gps_start: Optional[_dt.datetime] = None
    rear_gps_start: Optional[_dt.datetime] = None
    gps_start_offset_sec: Optional[float] = None
    matched_count: int = 0
    median_distance_m: Optional[float] = None
    max_distance_m: Optional[float] = None
    cancelled: bool = False
    problems: List[str] = field(default_factory=list)   # 하나라도 있으면 후방으로 받지 않는다
    notes: List[str] = field(default_factory=list)      # 참고 문구

    @property
    def ok(self) -> bool:
        return not self.problems and not self.cancelled


def _fmt_dur(sec: Optional[float]) -> str:
    if sec is None:
        return "알 수 없음"
    m, s = divmod(int(round(sec)), 60)
    return f"{m}분 {s:02d}초" if m else f"{s}초"


def _fmt_offset(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 120:
        return f"{seconds:.0f}초 차이"
    if seconds < 86400:
        h, rem = divmod(int(round(seconds)), 3600)
        return f"{h}시간 {rem // 60}분 차이" if h else f"{rem // 60}분 차이"
    return f"{seconds / 86400:.0f}일 차이"


def _fmt_epoch(epoch: Optional[float]) -> str:
    if epoch is None:
        return "알 수 없음"
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------- 기기 정보(컨테이너 서명) ----------
_NMEA_RE = re.compile(r"^[A-Z]{4,5},")
_ATOM_RE = re.compile(r"^[a-zA-Z©]{4}$")


def _normalize_model(text: str) -> str:
    """모델 문자열 비교용: 채널 번호·버전 숫자를 떼고 영문자만 소문자로.
    'Land Rover DashcamV1.05 CH:1' → 'landroverdashcamvch' (후방 파일의 CH:2와도 같아진다)."""
    return re.sub(r"[^a-z]", "", text.lower())


def _mp4_signature(path: str) -> Dict[str, str]:
    """ftyp 브랜드, udta 하위 박스 이름, udta 안의 모델 문자열(있으면). 랜드로버는 udta/mamt에
    'Land Rover Dashcam V1.05 CH:1'을 쓰고, INAVI·벤츠(Ambarella)는 'AMBA' 박스만 있다."""
    sig: Dict[str, str] = {}
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = 0
        while pos + 8 <= size:
            f.seek(pos)
            header = f.read(8)
            if len(header) < 8:
                break
            box_size, box_type = struct.unpack(">I4s", header)
            header_len = 8
            if box_size == 1:
                box_size = struct.unpack(">Q", f.read(8))[0]
                header_len = 16
            elif box_size == 0:
                box_size = size - pos
            if box_size < header_len:
                break
            if box_type == b"ftyp":
                f.seek(pos + header_len)
                sig["brand"] = f.read(4).decode("latin1", "replace").strip()
            elif box_type == b"moov":
                sub, end = pos + header_len, pos + box_size
                while sub + 8 <= end:
                    f.seek(sub)
                    sub_header = f.read(8)
                    if len(sub_header) < 8:
                        break
                    sub_size, sub_type = struct.unpack(">I4s", sub_header)
                    if sub_size < 8:
                        break
                    if sub_type == b"udta":
                        f.seek(sub + 8)
                        data = f.read(min(sub_size - 8, 262144))
                        atoms: List[str] = []
                        q = 0
                        while q + 8 <= len(data):
                            a_size, a_type = struct.unpack(">I4s", data[q:q + 8])
                            atoms.append(a_type.decode("latin1", "replace"))
                            q += max(a_size, 8)
                        sig["udta"] = ",".join(sorted(set(atoms)))
                        for run in re.findall(rb"[\x20-\x7e]{5,}", data):
                            for piece in run.decode("latin1").split("$"):
                                piece = piece.strip()
                                letters = re.sub(r"[^A-Za-z]", "", piece)
                                if len(letters) < 6 or _NMEA_RE.match(piece) or _ATOM_RE.match(piece):
                                    continue
                                sig["model"] = piece
                                break
                            if "model" in sig:
                                break
                    sub += sub_size
                break
            pos += box_size
    return sig


def _avi_signature(path: str) -> Dict[str, str]:
    """비디오 코덱 fourcc와 INFO/ISFT(만든 소프트웨어) 문자열. 해상도는 넣지 않는다 -
    전방 1920×1088, 후방 1920×1080처럼 카메라마다 달라서(VUGERA 실측)."""
    sig: Dict[str, str] = {}
    with open(path, "rb") as f:
        head = f.read(2 * 1024 * 1024)
    m = re.search(rb"strh.{4}vids(.{4})", head, re.S)
    if m:
        sig["codec"] = m.group(1).decode("latin1", "replace").strip()
    m = re.search(rb"ISFT.{4}([\x20-\x7e]{3,})", head, re.S)
    if m:
        sig["model"] = m.group(1).decode("latin1").strip("\x00 ")
    return sig


def device_signature(path: str, container: str) -> Dict[str, str]:
    from core.format_sniffer import CONTAINER_AVI, CONTAINER_MP4
    try:
        if container == CONTAINER_MP4:
            sig = _mp4_signature(path)
        elif container == CONTAINER_AVI:
            sig = _avi_signature(path)
        else:
            sig = {}
    except (OSError, struct.error, ValueError):
        sig = {}
    sig["container"] = container
    return sig


def describe_signature(sig: Dict[str, str]) -> str:
    parts = [sig.get("container", "?").upper()]
    for key in ("brand", "udta", "codec"):
        if sig.get(key):
            parts.append(f"{key}={sig[key]}")
    if sig.get("model"):
        parts.append(f"모델 '{sig['model']}'")
    return " ".join(parts)


# ---------- 파일명의 시각 ----------
_FILENAME_TIME_RES = (
    re.compile(r"(\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})"),                 # 20250901_215628
    re.compile(r"(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})"),                  # 2019_12_18_01_22_41
    re.compile(r"(\d{4})(\d{2})(\d{2})-(\d{2})h(\d{2})m(\d{2})s"),                   # 20241024-11h11m18s
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[_ T](\d{2})[-:.](\d{2})[-:.](\d{2})"),      # 2024-10-24_11-11-18
)


def parse_filename_timestamp(path: str) -> Optional[_dt.datetime]:
    name = os.path.splitext(os.path.basename(path))[0]
    for pattern in _FILENAME_TIME_RES:
        m = pattern.search(name)
        if not m:
            continue
        try:
            return _dt.datetime(*(int(g) for g in m.groups()))
        except ValueError:
            continue
    return None


def compare_pair(front_path: str, rear_path: str) -> PairCheck:
    """빠른 검사(파일만 읽음): 컨테이너 종류·기기 정보, 길이, 컨테이너 녹화 시각, 파일명 시각.
    허용치를 넘으면 problems에 적힌다. 파일명 짝·같은 폴더 여부는 참고 문구다(기기마다 규칙이
    달라서). GPS 대조까지 하려면 compare_pair_full()."""
    from core import duration as _duration
    from core.format_sniffer import CONTAINER_MP4, sniff

    check = PairCheck(front_path=front_path, rear_path=rear_path)
    if os.path.abspath(front_path) == os.path.abspath(rear_path):
        check.problems.append("전방으로 고른 파일과 같은 파일입니다.")
        return check
    check.same_folder = os.path.dirname(os.path.abspath(front_path)) == os.path.dirname(os.path.abspath(rear_path))
    check.name_pair = os.path.basename(rear_path).lower() in {
        os.path.basename(c).lower() for c in rear_candidates(front_path)}

    def probe(path: str):
        """(길이, 녹화 시각, 기기 서명). 못 읽으면 None - 비교를 건너뛴다. 녹화 시각은 MP4
        컨테이너에만 있으므로 AVI에는 MP4 파서를 돌리지 않는다(엉뚱한 경고만 난다)."""
        try:
            routing = sniff(path)
            dur = _duration.get_duration_sec(path, routing.container)
            rec = _duration.mp4_recorded_at_epoch(path) if routing.container == CONTAINER_MP4 else None
            return dur, rec, device_signature(path, routing.container)
        except Exception:  # noqa: BLE001
            return None, None, {}

    check.front_duration, check.front_recorded_at, check.front_signature = probe(front_path)
    check.rear_duration, check.rear_recorded_at, check.rear_signature = probe(rear_path)

    fs, rs = check.front_signature, check.rear_signature
    if fs.get("container") and rs.get("container") and fs["container"] != rs["container"]:
        check.problems.append(
            f"파일 종류가 다릅니다: 전방 {fs['container'].upper()}, 후방 {rs['container'].upper()}")
    else:
        if fs.get("model") and rs.get("model") and _normalize_model(fs["model"]) != _normalize_model(rs["model"]):
            check.problems.append(f"기기 식별 문자열이 다릅니다: 전방 '{fs['model']}', 후방 '{rs['model']}'")
        for key, label in (("brand", "MP4 브랜드"), ("udta", "메타데이터 구성"), ("codec", "영상 코덱")):
            if fs.get(key) and rs.get(key) and fs[key] != rs[key]:
                check.problems.append(f"기기 정보가 다릅니다 ({label}): 전방 {fs[key]}, 후방 {rs[key]}")
        if not (fs.get("model") and rs.get("model")):
            check.notes.append("파일에 기기 식별 문자열이 없어 모델은 비교하지 않았습니다.")

    ft, rt = parse_filename_timestamp(front_path), parse_filename_timestamp(rear_path)
    if ft and rt and abs((ft - rt).total_seconds()) > FILENAME_TIME_TOLERANCE_SEC:
        check.problems.append(f"파일명의 시각이 다릅니다: 전방 {ft:%Y-%m-%d %H:%M:%S}, 후방 {rt:%Y-%m-%d %H:%M:%S}")

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


# ---------- GPS 대조 (엔진을 두 파일에 돌린다) ----------
def _gps_records(points) -> List[Tuple[_dt.datetime, Optional[float], Optional[float]]]:
    """(UTC 시각, 위도, 경도) - GPS 기록이 있는 행만, 같은 시각은 첫 행만. 좌표 없는 행(끊김)은
    좌표가 None."""
    from core import gpstime
    out = []
    seen = set()
    for p in points:
        if not p.has_gps_record or p.start_time_sec is None:
            continue
        d, t = gpstime.parse_date(p.gps_date), gpstime.parse_time(p.gps_utc_time)
        if d is None or t is None:
            continue
        key = (d, t)
        if key in seen:
            continue
        seen.add(key)
        lat, lon = (p.latitude, p.longitude) if p.has_coords else (None, None)
        out.append((_dt.datetime.combine(d, t, tzinfo=_dt.timezone.utc), lat, lon))
    out.sort(key=lambda r: r[0])
    return out


def compare_gps(check: PairCheck, front_points, rear_points) -> None:
    """GPS 시각과 같은 시각의 좌표를 대조해 check에 적는다. 후방(또는 전방)에 GPS 기록이 없으면
    검증할 근거가 없으므로 막는다(사용자 결정: 미검증 통과 없음)."""
    from core.outliers import haversine_m

    check.gps_checked = True
    front = _gps_records(front_points)
    rear = _gps_records(rear_points)
    if not rear:
        check.problems.append("후방으로 선택한 영상에 GPS 기록이 없어 전방과 대조할 근거가 없습니다.")
        return
    if not front:
        check.problems.append("전방 영상에 GPS 기록이 없어 후방과 대조할 수 없습니다.")
        return
    check.front_gps_start, check.rear_gps_start = front[0][0], rear[0][0]
    offset = (rear[0][0] - front[0][0]).total_seconds()
    check.gps_start_offset_sec = offset
    if abs(offset) > GPS_START_TOLERANCE_SEC:
        from core import gpstime
        f_disp = gpstime.format_display(front[0][0].strftime("%Y-%m-%d"), front[0][0].strftime("%H:%M:%S"))
        r_disp = gpstime.format_display(rear[0][0].strftime("%Y-%m-%d"), rear[0][0].strftime("%H:%M:%S"))
        check.problems.append(f"GPS 시각이 다릅니다: 전방 {f_disp} 시작, 후방 {r_disp} 시작 ({_fmt_offset(offset)})")

    front_by_time = {t: (lat, lon) for t, lat, lon in front if lat is not None}
    distances = []
    for t, lat, lon in rear:
        if lat is None:
            continue
        ref = front_by_time.get(t)
        if ref is not None:
            distances.append(haversine_m(ref[0], ref[1], lat, lon))
    check.matched_count = len(distances)
    if len(distances) >= MIN_COORD_MATCHES:
        check.median_distance_m = statistics.median(distances)
        check.max_distance_m = max(distances)
        if check.median_distance_m > COORD_TOLERANCE_M:
            check.problems.append(
                f"같은 시각의 좌표가 다릅니다: 중앙값 {check.median_distance_m:.0f} m 떨어짐 "
                f"({len(distances)}개 시각 비교)")
    elif abs(offset) <= GPS_START_TOLERANCE_SEC:
        check.notes.append("같은 시각의 좌표가 3개 미만이라 좌표는 비교하지 않았습니다.")


def compare_pair_full(front_path: str, rear_path: str, workdir: Optional[str] = None,
                      cancel_event: Optional[threading.Event] = None,
                      progress: Optional[Callable[[str], None]] = None) -> PairCheck:
    """빠른 검사 + 엔진을 두 파일에 돌려 GPS 시각·좌표 대조. workdir을 안 주면 임시 폴더를 만들고
    끝나면 지운다. cancel_event가 서면 cancelled=True로 돌려준다."""
    from engine import engine_adapter as _ea

    check = compare_pair(front_path, rear_path)
    if os.path.abspath(front_path) == os.path.abspath(rear_path):
        return check
    own_dir = workdir is None
    base = workdir or tempfile.mkdtemp(prefix="idas-pair-")
    try:
        results = {}
        for label, path in (("전방", front_path), ("후방", rear_path)):
            if cancel_event is not None and cancel_event.is_set():
                check.cancelled = True
                return check
            if progress:
                progress(f"{label} 영상의 GPS 기록을 읽는 중...")
            out_dir = os.path.join(base, "front" if label == "전방" else "rear")
            os.makedirs(out_dir, exist_ok=True)
            results[label] = _ea.run_full_extraction(path, out_dir, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            check.cancelled = True
            return check
        if progress:
            progress("GPS 시각과 좌표를 대조하는 중...")
        compare_gps(check, results["전방"].points, results["후방"].points)
    finally:
        if own_dir:
            shutil.rmtree(base, ignore_errors=True)
    return check
