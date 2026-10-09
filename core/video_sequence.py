"""연속 영상 이어보기 - 여러 파일이 한 번의 주행을 끊김 없이 나눠 담은 것인지 검사하고 순서를 정한다.

블랙박스는 10분을 한 파일로 쓰지 않고 1분 파일 10개로 나눠 쓴다. 이어보기는 그 파일들을 한 사건으로
묶어 재생·지도·그래프를 이어 붙인다. 엉뚱한 파일(다른 날, 다른 차, 사이가 빈 녹화)이 섞이면 궤적과
시간축이 거짓이 되므로 전·후방 같이 보기처럼 엄격하게 막는다(사용자 결정: 끊김 없는 것만 허용).

검사 (파일 크기는 보지 않는다 - 같은 녹화라도 장면에 따라 크기가 다르다):
  1. 형식·기기: 컨테이너 종류, 기기 서명(모델 문자열·MP4 브랜드·udta 구성·AVI 코덱)이 모두 같다.
  2. 시각: 영상마다 "0초의 시각"을 GPS 기록(UTC 시각 - 그 행의 영상 시각)으로 구한다. GPS 시각이
     없는 기기(FineVu)는 파일명의 시각을 쓴다. 모든 영상이 같은 근거를 가져야 비교할 수 있다.
     근거가 없는 영상이 있으면 막는다("검증할 근거가 없습니다").
  3. 이어짐: 앞 영상의 시작 + 길이 와 다음 영상의 시작이 ±5초 안이어야 한다.
  4. 위치: 앞 영상의 마지막 좌표와 다음 영상의 첫 좌표 거리가, 그 사이 시간 동안 두 영상의 최고
     속도로 갈 수 있는 거리 + 50 m 안이어야 한다. 좌표가 없는 영상은 막는다.
전·후방 이어보기: 후방 파일마다 짝이 되는 전방을 찾아(파일명 규칙 또는 시작 시각 3초 안) 전·후방
대조(core/video_pairs)를 한다. 짝이 없는 후방은 그 자리의 후방만 있는 구간이 되어 이어짐 검사에
들어간다.
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from core.video_pairs import (
    PairCheck,
    _normalize_model,
    compare_gps,
    compare_pair,
    parse_filename_timestamp,
    rear_candidates,
)

GAP_TOLERANCE_SEC = 5.0
POSITION_SLACK_M = 50.0
MIN_SPEED_FOR_REACH_KMH = 10.0
PAIR_START_TOLERANCE_SEC = 3.0

BASIS_GPS = "gps"
BASIS_FILENAME = "filename"


@dataclass
class ClipProbe:
    """영상 하나를 읽은 결과(엔진 GPS 추출 포함)."""
    path: str
    container: str = ""
    signature: Dict[str, str] = field(default_factory=dict)
    duration: Optional[float] = None
    points: list = field(default_factory=list)
    gps_start: Optional[_dt.datetime] = None     # 영상 0초의 UTC 시각(GPS 기록으로 구함)
    name_start: Optional[_dt.datetime] = None    # 파일명의 시각(기기 현지 시각)
    first_fix: Optional[Tuple[float, float, float]] = None   # (영상 시각, 위도, 경도)
    last_fix: Optional[Tuple[float, float, float]] = None
    max_speed_kmh: float = 0.0
    error: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass
class SequenceItem:
    """이어보기 한 구간. 전방이 없으면 후방이 주 영상(분석·재생 대상)이 된다."""
    front: str = ""
    rear: str = ""
    track_mode: str = ""   # 전·후방 트랙이 한 파일에 든 영상의 보기 방식(core/video_tracks)

    @property
    def primary(self) -> str:
        return self.front or self.rear

    @property
    def primary_is_rear(self) -> bool:
        return not self.front and bool(self.rear)


@dataclass
class SequenceSlot:
    front: Optional[ClipProbe] = None
    rear: Optional[ClipProbe] = None

    @property
    def primary(self) -> ClipProbe:
        return self.front or self.rear


@dataclass
class SequencePlan:
    slots: List[SequenceSlot] = field(default_factory=list)
    basis: str = ""
    problems: List[str] = field(default_factory=list)   # 하나라도 있으면 이어보기를 막는다
    notes: List[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems and not self.cancelled

    def items(self, track_mode: str = "") -> List[SequenceItem]:
        return [SequenceItem(front=s.front.path if s.front else "",
                             rear=s.rear.path if s.rear else "", track_mode=track_mode)
                for s in self.slots]


# ---------------------------------------------------------------------------
# 영상 읽기
# ---------------------------------------------------------------------------

def _gps_start(points) -> Optional[_dt.datetime]:
    """영상 0초의 UTC 시각. 앞쪽 GPS 기록 몇 개의 (UTC - 영상 시각) 중앙값."""
    from core import gpstime
    offsets = []
    seen = set()
    for p in points:
        # 측위 전(V) 기록이나 체크섬이 깨진 문장의 시각(1980년 등)은 근거로 쓰지 않는다(리뷰 #37).
        if not p.has_fix or p.start_time_sec is None or p.gps_checksum_ok is False:
            continue
        d, t = gpstime.parse_date(p.gps_date), gpstime.parse_time(p.gps_utc_time)
        if d is None or t is None or (d, t) in seen:
            continue
        seen.add((d, t))
        utc = _dt.datetime.combine(d, t, tzinfo=_dt.timezone.utc)
        offsets.append(utc - _dt.timedelta(seconds=p.start_time_sec))
        if len(offsets) >= 7:
            break
    if not offsets:
        return None
    offsets.sort()
    return offsets[len(offsets) // 2]


def summarize_points(probe: ClipProbe) -> None:
    from core.acceleration import _distinct_fix_indices
    probe.gps_start = _gps_start(probe.points)
    usable = _distinct_fix_indices(probe.points)
    if usable:
        a, b = probe.points[usable[0]], probe.points[usable[-1]]
        probe.first_fix = (a.start_time_sec, a.latitude, a.longitude)
        probe.last_fix = (b.start_time_sec, b.latitude, b.longitude)
        probe.max_speed_kmh = max(probe.points[i].speed_kmh for i in usable)


def probe_clip(path: str, workdir: str, cancel_event: Optional[threading.Event] = None) -> ClipProbe:
    from core import duration as _duration, outliers
    from core.format_sniffer import sniff
    from core.video_pairs import device_signature
    from engine import engine_adapter as _ea

    probe = ClipProbe(path=path, name_start=parse_filename_timestamp(path))
    try:
        routing = sniff(path)
        probe.container = routing.container
        if not routing.supported:
            probe.error = routing.reason or "지원하지 않는 파일입니다."
            return probe
        probe.signature = device_signature(path, routing.container)
        probe.duration = _duration.get_duration_sec(path, routing.container)
        out_dir = tempfile.mkdtemp(prefix="clip-", dir=workdir)
        result = _ea.run_full_extraction(path, out_dir, cancel_event=cancel_event)
        if result.status not in ("ok", "no_gps"):
            probe.error = result.status_message   # 엔진 실패를 'GPS 없음'으로 안내하지 않는다(리뷰 #100)
            return probe
        outliers.mark_outliers(result.points)
        probe.points = result.points
        if probe.duration is None:
            probe.duration = _duration.get_duration_sec(path, routing.container, engine_output_dir=out_dir)
        summarize_points(probe)
    except _ea.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 파일 하나의 실패를 검사 결과로 알린다
        probe.error = f"{type(exc).__name__}: {exc}"
    return probe


# ---------------------------------------------------------------------------
# 검사
# ---------------------------------------------------------------------------

def signature_problems(a: ClipProbe, b: ClipProbe, label_a: str, label_b: str) -> List[str]:
    sa, sb = a.signature, b.signature
    if a.container and b.container and a.container != b.container:
        return [f"파일 종류가 다릅니다: {label_a} {a.container.upper()}, {label_b} {b.container.upper()}"]
    out = []
    if sa.get("model") and sb.get("model") and _normalize_model(sa["model"]) != _normalize_model(sb["model"]):
        out.append(f"기기 식별 문자열이 다릅니다: {label_a} '{sa['model']}', {label_b} '{sb['model']}'")
    for key, label in (("brand", "MP4 브랜드"), ("udta", "메타데이터 구성"), ("codec", "영상 코덱")):
        if sa.get(key) and sb.get(key) and sa[key] != sb[key]:
            out.append(f"기기 정보가 다릅니다 ({label}): {label_a} {sa[key]}, {label_b} {sb[key]}")
    return out


def _start_of(probe: ClipProbe, basis: str) -> Optional[_dt.datetime]:
    if basis == BASIS_GPS:
        return probe.gps_start
    if probe.name_start is None:
        return None
    return probe.name_start.replace(tzinfo=_dt.timezone.utc)   # 비교만 하므로 기준만 맞춘다


def choose_basis(probes: List[ClipProbe]) -> str:
    if probes and all(p.gps_start is not None for p in probes):
        return BASIS_GPS
    if probes and all(p.name_start is not None for p in probes):
        return BASIS_FILENAME
    return ""


def _fmt_gap(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 120:
        return f"{seconds:.1f}초"
    if seconds < 7200:
        return f"{seconds / 60:.0f}분"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}시간"
    return f"{seconds / 86400:.0f}일"


def check_chain(slots: List[SequenceSlot]) -> Tuple[List[SequenceSlot], str, List[str], List[str]]:
    """구간들을 시각순으로 세우고 이어짐을 검사한다. (정렬된 구간, 근거, 문제, 참고)."""
    from core.outliers import haversine_m

    problems: List[str] = []
    notes: List[str] = []
    primaries = [s.primary for s in slots]
    for s in slots:
        if s.primary.error:
            problems.append(f"{s.primary.name}: 파일을 읽지 못했습니다 ({s.primary.error})")
    if problems:
        return slots, "", problems, notes

    seen_paths = {}
    for p in primaries:
        key = os.path.normcase(os.path.abspath(p.path))
        if key in seen_paths:
            problems.append(f"같은 파일을 두 번 골랐습니다: {p.name}")
        seen_paths[key] = True

    basis = choose_basis(primaries)
    if not basis:
        for p in primaries:
            if p.gps_start is None and p.name_start is None:
                problems.append(f"{p.name}에 GPS 기록이 없어 이어지는 영상인지 검증할 근거가 없습니다.")
        if not problems:
            problems.append("영상마다 시각 근거(GPS 시각 / 파일명 시각)가 달라 이어지는지 비교할 수 없습니다.")
        return slots, "", problems, notes
    if basis == BASIS_FILENAME:
        notes.append("GPS에 시각이 기록되지 않는 기기라 파일명의 시각으로 순서와 이어짐을 확인했습니다.")

    ordered = sorted(slots, key=lambda s: _start_of(s.primary, basis))
    first = ordered[0].primary
    for n, slot in enumerate(ordered[1:], start=2):
        problems += [f"{n}번 영상({slot.primary.name}): {msg}"
                     for msg in signature_problems(first, slot.primary, "1번", f"{n}번")]

    for n, (a_slot, b_slot) in enumerate(zip(ordered, ordered[1:]), start=1):
        a, b = a_slot.primary, b_slot.primary
        where = f"{n}번({a.name}) → {n + 1}번({b.name})"
        if a.duration is None:
            problems.append(f"{n}번 영상({a.name})의 길이를 읽지 못해 다음 영상과 이어지는지 확인할 수 없습니다.")
            continue
        gap = (_start_of(b, basis) - _start_of(a, basis)).total_seconds() - a.duration
        if gap > GAP_TOLERANCE_SEC:
            problems.append(f"{where}: 앞 영상이 끝나고 {_fmt_gap(gap)} 뒤에 다음 영상이 시작합니다 (이어지는 녹화가 아님).")
            continue
        if gap < -GAP_TOLERANCE_SEC:
            problems.append(f"{where}: 두 영상이 {_fmt_gap(gap)} 겹칩니다 (같은 시간대의 다른 녹화).")
            continue
        if a.last_fix is None or b.first_fix is None:
            empty = a.name if a.last_fix is None else b.name
            problems.append(f"{where}: {empty}에 좌표가 없어 위치가 이어지는지 검증할 근거가 없습니다.")
            continue
        ta = _start_of(a, basis) + _dt.timedelta(seconds=a.last_fix[0])
        tb = _start_of(b, basis) + _dt.timedelta(seconds=b.first_fix[0])
        dt = max(1.0, abs((tb - ta).total_seconds()))
        speed = max(a.max_speed_kmh, b.max_speed_kmh, MIN_SPEED_FOR_REACH_KMH) / 3.6
        reach = speed * dt + POSITION_SLACK_M
        distance = haversine_m(a.last_fix[1], a.last_fix[2], b.first_fix[1], b.first_fix[2])
        if distance > reach:
            problems.append(f"{where}: 앞 영상의 마지막 위치와 다음 영상의 첫 위치가 {distance:.0f} m 떨어져 있습니다 "
                            f"({dt:.0f}초 동안 갈 수 있는 거리 {reach:.0f} m 초과).")
    return ordered, basis, problems, notes


def check_pair_probes(front: ClipProbe, rear: ClipProbe) -> PairCheck:
    """이미 읽어 둔 두 영상으로 전·후방 대조(core/video_pairs와 같은 기준)."""
    check = compare_pair(front.path, rear.path)
    if check.problems and "같은 파일" in check.problems[0]:
        return check
    compare_gps(check, front.points, rear.points)
    return check


def _pair_candidates(rear: ClipProbe, fronts: List[ClipProbe]) -> List[ClipProbe]:
    """이 후방의 짝이 될 만한 전방: 파일명 짝 규칙이 맞거나 시작 시각이 3초 안."""
    out = []
    for f in fronts:
        by_name = rear.name.lower() in {os.path.basename(c).lower() for c in rear_candidates(f.path)}
        by_time = False
        for a, b in ((f.gps_start, rear.gps_start), (f.name_start, rear.name_start)):
            if a is not None and b is not None and abs((a - b).total_seconds()) <= PAIR_START_TOLERANCE_SEC:
                by_time = True
        if by_name or by_time:
            out.append(f)
    return out


def plan_sequence(fronts: List[ClipProbe], rears: List[ClipProbe]) -> SequencePlan:
    plan = SequencePlan()
    slots = [SequenceSlot(front=f) for f in fronts]
    for rear in rears:
        if rear.error:
            plan.problems.append(f"{rear.name}: 파일을 읽지 못했습니다 ({rear.error})")
            continue
        free = [s.front for s in slots if s.front is not None and s.rear is None]
        candidates = _pair_candidates(rear, free)
        matched = False
        failures = []
        for front in candidates:
            check = check_pair_probes(front, rear)
            if check.ok:
                next(s for s in slots if s.front is front).rear = rear
                matched = True
                break
            failures.append((front, check))
        if matched:
            continue
        if failures:
            front, check = failures[0]
            plan.problems.append(f"후방 {rear.name}이(가) 짝이 되는 전방 {front.name}과 맞지 않습니다: "
                                 + "; ".join(check.problems))
            continue
        slots.append(SequenceSlot(rear=rear))   # 이 자리는 후방만 있다
    if plan.problems:
        plan.slots = slots
        return plan
    plan.slots, plan.basis, problems, notes = check_chain(slots)
    plan.problems += problems
    plan.notes += notes
    return plan


def probe_and_plan(fronts: List[str], rears: List[str],
                   cancel_event: Optional[threading.Event] = None,
                   progress: Optional[Callable[[str], None]] = None) -> SequencePlan:
    """파일을 모두 읽고(엔진 실행) 구간을 짜서 검사한다. 취소하면 cancelled=True."""
    from engine.engine_adapter import CancelledError

    workdir = tempfile.mkdtemp(prefix="idas-seq-")
    try:
        probes: Dict[str, ClipProbe] = {}
        paths = [("전방", p) for p in fronts] + [("후방", p) for p in rears]
        for n, (label, path) in enumerate(paths, start=1):
            if cancel_event is not None and cancel_event.is_set():
                return SequencePlan(cancelled=True)
            if progress:
                progress(f"영상 GPS 기록을 읽는 중 ({n}/{len(paths)}) - {label} {os.path.basename(path)}")
            try:
                probes[(label, path)] = probe_clip(path, workdir, cancel_event)
            except CancelledError:
                return SequencePlan(cancelled=True)
        if progress:
            progress("순서와 이어짐을 검사하는 중...")
        return plan_sequence([probes[("전방", p)] for p in fronts], [probes[("후방", p)] for p in rears])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def probe_single(path: str, cancel_event: Optional[threading.Event] = None) -> Optional[ClipProbe]:
    """나중에 한 개를 더 고를 때(빠진 짝 채우기). 취소하면 None."""
    from engine.engine_adapter import CancelledError

    workdir = tempfile.mkdtemp(prefix="idas-seq-")
    try:
        return probe_clip(path, workdir, cancel_event)
    except CancelledError:
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def slot_start_text(slot: SequenceSlot, basis: str) -> str:
    """확인 창에 보일 시작 시각(한국 시간)."""
    from core import gpstime
    start = _start_of(slot.primary, basis)
    if start is None:
        return ""
    if basis == BASIS_GPS:
        return gpstime.format_display(start.strftime("%Y-%m-%d"), start.strftime("%H:%M:%S"))
    return start.strftime("%Y-%m-%d %H:%M:%S") + " (파일명)"


