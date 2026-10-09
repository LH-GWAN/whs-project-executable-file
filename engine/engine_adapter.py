from __future__ import annotations

import csv
import glob
import math
import os
import json
import subprocess
import time
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from core.format_sniffer import RoutingResult, sniff
from engine.registry import ENGINE_NAME, NEEDS_EXTRA_TRACK_PASSES

from core.paths import engine_entry_script, is_frozen

RUN_ENGINE_FLAG = "--run-engine"


@dataclass
class EngineRunResult:
    argv: List[str]
    exit_code: int
    stdout: str
    stderr: str
    started_at: datetime
    finished_at: datetime
    timed_out: bool = False
    note: str = ""


@dataclass
class TrackPoint:

    start_time_sec: Optional[float] = None
    end_time_sec: Optional[float] = None
    time_source: str = ""

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed_kmh: Optional[float] = None
    track_deg: Optional[float] = None
    gps_date: str = ""
    gps_utc_time: str = ""
    gps_checksum_ok: Optional[bool] = None
    gps_trusted: Optional[bool] = None
    # UTC가 없는 기기(FineVu)의 기록 근거: 측위 상태(A/V), 파일명 기준 절대 시각, 레코드 경과 초.
    gps_status: str = ""
    gps_abs_time: str = ""
    gps_elapsed: Optional[float] = None

    latitude_last: Optional[float] = None
    longitude_last: Optional[float] = None
    speed_kmh_last: Optional[float] = None

    x_g: Optional[float] = None
    y_g: Optional[float] = None
    z_g: Optional[float] = None
    x_g_cal: Optional[float] = None
    y_g_cal: Optional[float] = None
    z_g_cal: Optional[float] = None

    source_file: str = ""

    # 앱 판정(core/outliers.py). 좌표는 남아 있지만 화면·지도·그래프·급가감속 계산에서 뺀다.
    is_outlier: bool = False
    outlier_reason: str = ""
    # 연속 영상 이어보기에서 이 행이 속한 영상(0부터). 하나짜리 사건은 늘 0이다.
    segment_index: int = 0

    @property
    def has_coords(self) -> bool:
        """엔진이 좌표를 뽑았는가(이상치 판정과 무관)."""
        return self.latitude is not None and self.longitude is not None

    @property
    def has_fix(self) -> bool:
        return (self.has_coords and not self.is_outlier
                and self.gps_checksum_ok is not False and self.gps_trusted is not False
                and math.isfinite(self.latitude) and math.isfinite(self.longitude)
                and -90 <= self.latitude <= 90 and -180 <= self.longitude <= 180)

    @property
    def has_gps_record(self) -> bool:
        return bool((self.gps_utc_time or "").strip() or (self.gps_date or "").strip()
                    or (self.gps_status or "").strip() or (self.gps_abs_time or "").strip()
                    or self.gps_elapsed is not None)

    def record_key(self):
        """같은 GPS 측정인지 가르는 열쇠. UTC가 있으면 UTC, 없으면 기기가 준 절대 시각·경과 초, 그것도
        없으면 값(좌표·속도). 값으로만 묶으면 정차 중 같은 값이 한 측정이 되어 급출발을 놓치고 그래프가
        왜곡된다(리뷰 #7). 반복 기록 판정·급가감속·1초 표·Tracker 정보 줄이 모두 이 열쇠를 쓴다."""
        utc = (self.gps_utc_time or "").strip()
        if utc:
            return ("utc", self.gps_date or "", utc)
        if (self.gps_abs_time or "").strip():
            return ("abs", self.gps_abs_time.strip())
        if self.gps_elapsed is not None:
            return ("elapsed", self.gps_elapsed)
        return ("val", self.latitude, self.longitude, self.speed_kmh)

    @property
    def is_dropout(self) -> bool:
        # 좌표 자체가 없을 때만 끊김이다. 이상치는 좌표가 있지만 쓰지 않는 것이라 따로 센다.
        return self.has_gps_record and not self.has_coords

    @property
    def g_magnitude(self) -> Optional[float]:
        """충격 세기(합력, g). 축 방향은 기기 장착 각도에 따라 달라서 개별 축값은
        비교 기준이 못 되지만, 합력 크기는 장착 방향과 무관해서 충격 시점을 짚는 데
        쓸 수 있다. 엔진이 자가 보정한 *_g_cal이 있으면 그쪽을 우선한다."""
        for x, y, z in ((self.x_g_cal, self.y_g_cal, self.z_g_cal),
                        (self.x_g, self.y_g, self.z_g)):
            if x is not None and y is not None and z is not None:
                return math.sqrt(x * x + y * y + z * z)
        return None

    @property
    def display_latitude(self) -> Optional[float]:
        return self.latitude if self.latitude is not None else self.latitude_last

    @property
    def display_longitude(self) -> Optional[float]:
        return self.longitude if self.longitude is not None else self.longitude_last


# NMEA 뒤에 128KB 넘는 문자열이 붙은 조작 파일에서 csv 기본 한도(131072)에 걸려 분석 전체가 실패했다
# (리뷰 #63). 행 하나가 커도 읽고, 슬랙 로드 실패는 본 분석과 분리한다.
csv.field_size_limit(min(2 ** 31 - 1, 256 * 1024 * 1024))

_MAX_CAPTURED_OUTPUT = 2 * 1024 * 1024

STATUS_OK = "ok"
STATUS_UNSUPPORTED = "unsupported"
STATUS_ENGINE_FAILED = "engine_failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_NO_GPS = "no_gps"
STATUS_GPS_UNTRUSTED = "gps_untrusted"


@dataclass
class ExtractionResult:
    routing: RoutingResult
    points: List[TrackPoint]
    engine_runs: List[EngineRunResult]
    used_input_path: str
    primary_source_file: Optional[str] = None
    time_source: str = ""
    warnings: List[str] = field(default_factory=list)
    status: str = STATUS_OK
    status_detail: str = ""
    slack_points: List[TrackPoint] = field(default_factory=list)
    avi_repaired: bool = False

    @property
    def fix_count(self) -> int:
        return sum(1 for p in self.points if p.has_fix)

    @property
    def dropout_count(self) -> int:
        return sum(1 for p in self.points if p.is_dropout)

    @property
    def outlier_count(self) -> int:
        return sum(1 for p in self.points if p.is_outlier)

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_OK

    @property
    def status_message(self) -> str:
        if self.status == STATUS_OK:
            return f"GPS 좌표 {self.fix_count}개를 추출했습니다."
        if self.status == STATUS_UNSUPPORTED:
            return f"처리할 수 없는 파일입니다. {self.status_detail}"
        if self.status == STATUS_TIMED_OUT:
            return f"분석이 제한 시간을 초과해 중단됐습니다. {self.status_detail}"
        if self.status == STATUS_ENGINE_FAILED:
            return f"분석 엔진이 실패했습니다. {self.status_detail}"
        if self.status == STATUS_GPS_UNTRUSTED:
            return "GPS 기록은 있으나 신뢰할 수 있는 좌표가 없습니다. 원본 CSV를 확인하세요."
        if self.status == STATUS_NO_GPS:
            return "이 영상에서 GPS를 추출하지 못했습니다 (미기록 또는 미지원 메타데이터 형식)."
        return self.status_detail


def build_subprocess_argv(engine_args: List[str]) -> List[str]:
    if is_frozen():
        return [sys.executable, RUN_ENGINE_FLAG, ENGINE_NAME, *engine_args]
    return [sys.executable, engine_entry_script(), ENGINE_NAME, *engine_args]


class CancelledError(Exception):
    pass


def run_engine(input_path: str, output_dir: str, slack: bool = False,
                extra_args: Optional[List[str]] = None, note: str = "",
                timeout_sec: Optional[float] = 1800,
                cancel_event: Optional["threading.Event"] = None) -> EngineRunResult:
    os.makedirs(output_dir, exist_ok=True)
    engine_args = ["-o", output_dir]
    if slack:
        engine_args.append("--slack")
    engine_args.extend(extra_args or [])
    engine_args.append(input_path)

    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("분석이 취소되었습니다.")

    argv = build_subprocess_argv(engine_args)
    started_at = datetime.now()
    started_mono = time.monotonic()   # 제한 시간은 벽시계가 아니라 단조 시계로 잰다(리뷰 #94)
    timed_out = False
    cancelled = False

    # subprocess.run은 블로킹이라 중간에 멈출 수 없다. 대용량 파일 분석이 수십 분
    # 걸릴 수 있으므로, 취소 요청이 오면 자식 프로세스를 실제로 종료할 수 있게
    # Popen으로 띄우고 짧은 간격으로 지켜본다.
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    stdout = stderr = ""
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if timeout_sec is not None and time.monotonic() - started_mono > timeout_sec:
                timed_out = True
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
            stdout = stdout or out or ""
            stderr = stderr or err or ""

    exit_code = proc.returncode if not (timed_out or cancelled) else -1
    if timed_out:
        stderr += f"\n[engine_adapter] {timeout_sec}초 초과로 강제 종료됨"
    if cancelled:
        stderr += "\n[engine_adapter] 사용자가 취소함"

    finished_at = datetime.now()
    # 경고가 수백만 건인 조작 파일은 stdout이 수백 MB가 된다. 앞·뒤만 남긴다(리뷰 #62).
    if len(stdout) > _MAX_CAPTURED_OUTPUT:
        stdout = (stdout[:_MAX_CAPTURED_OUTPUT // 2] + "\n… (출력이 길어 가운데를 생략함) …\n"
                  + stdout[-_MAX_CAPTURED_OUTPUT // 2:])
    if len(stderr) > _MAX_CAPTURED_OUTPUT:
        stderr = stderr[:_MAX_CAPTURED_OUTPUT // 2] + "\n… (생략) …\n" + stderr[-_MAX_CAPTURED_OUTPUT // 2:]
    result = EngineRunResult(
        argv=argv, exit_code=exit_code, stdout=stdout, stderr=stderr,
        started_at=started_at, finished_at=finished_at, timed_out=timed_out, note=note,
    )
    if cancelled:
        raise CancelledError("분석이 취소되었습니다.")
    return result


def _f(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def _b(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    value = value.strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    return None


def find_csvs(output_dir: str, filename: str) -> List[str]:
    # 경로에 '[ ]'가 있으면(clip[1].mp4 폴더) glob이 문자 집합으로 읽어 CSV를 못 찾았다 - 그러면
    # 신뢰도 사이드카(coordinates.csv)를 못 붙여 status=V 좌표가 정상 좌표가 됐다(리뷰 #4).
    return sorted(glob.glob(os.path.join(glob.escape(output_dir), "**", filename), recursive=True))


def _count_fixes(csv_path: str) -> int:
    try:
        loader = load_timeline if os.path.basename(csv_path) == "timeline.csv" else load_coordinates_as_points
        return sum(p.has_fix for p in loader(csv_path))
    except (OSError, ValueError):
        return 0


def pick_primary_csv(csv_paths: List[str]) -> Optional[str]:
    if not csv_paths:
        return None
    if len(csv_paths) == 1:
        return csv_paths[0]
    return max(csv_paths, key=_count_fixes)


def load_timeline(csv_path: str) -> List[TrackPoint]:
    points: List[TrackPoint] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            points.append(TrackPoint(
                start_time_sec=_f(row.get("start_time_sec")),
                end_time_sec=_f(row.get("end_time_sec")),
                time_source=(row.get("time_source") or "").strip(),
                latitude=_f(row.get("latitude")),
                longitude=_f(row.get("longitude")),
                speed_kmh=_f(row.get("speed_kmh")),
                track_deg=_f(row.get("track_deg")),
                gps_date=(row.get("gps_date") or "").strip(),
                gps_utc_time=(row.get("gps_utc_time") or "").strip(),
                gps_checksum_ok=_b(row.get("gps_checksum_ok")),
                gps_trusted=_b(row.get("gps_trusted")),
                gps_status=(row.get("gps_status") or "").strip(),
                gps_abs_time=(row.get("abs_time") or "").strip(),
                gps_elapsed=_f(row.get("gps_elapsed_sec")),
                latitude_last=_f(row.get("latitude_last")),
                longitude_last=_f(row.get("longitude_last")),
                speed_kmh_last=_f(row.get("speed_kmh_last")),
                x_g=_f(row.get("x_g")), y_g=_f(row.get("y_g")), z_g=_f(row.get("z_g")),
                x_g_cal=_f(row.get("x_g_cal")), y_g_cal=_f(row.get("y_g_cal")),
                z_g_cal=_f(row.get("z_g_cal")),
                source_file=csv_path,
            ))
    _restore_timeline_trust(points, csv_path)
    return points


def _restore_timeline_trust(points: List[TrackPoint], csv_path: str) -> None:
    """Pinned engines omit trusted/status from timeline but retain it in coordinates.csv.

    Match observed time/coordinates/GPS time, not row positions (sensor rows differ).
    Ambiguous matches fail closed if any matched record is explicitly untrusted.
    """
    def key(p):
        return (p.start_time_sec, p.latitude, p.longitude, p.gps_date, p.gps_utc_time)
    trust = {}
    for path in find_csvs(os.path.dirname(csv_path), "coordinates.csv"):
        for p in load_coordinates_as_points(path):
            value = p.gps_trusted
            if value is not None:
                k = key(p)
                trust[k] = trust.get(k, True) and value
    for p in points:
        if key(p) in trust:
            p.gps_trusted = trust[key(p)] if p.gps_trusted is None else p.gps_trusted and trust[key(p)]


def load_coordinates_as_points(csv_path: str) -> List[TrackPoint]:
    points: List[TrackPoint] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            points.append(TrackPoint(
                start_time_sec=_f(row.get("start_time_sec")),
                end_time_sec=_f(row.get("end_time_sec")),
                time_source=(row.get("time_source") or "").strip(),
                latitude=_f(row.get("latitude")),
                longitude=_f(row.get("longitude")),
                speed_kmh=_f(row.get("speed_kmh")),
                track_deg=_f(row.get("track_deg")),
                gps_date=(row.get("date") or "").strip(),
                gps_utc_time=(row.get("utc_time") or "").strip(),
                gps_checksum_ok=_b(row.get("checksum_ok")),
                gps_trusted=(False if _b(row.get("status_valid")) is False
                             else _b(row.get("trusted"))),
                gps_status=(row.get("status") or "").strip(),
                gps_abs_time=(row.get("abs_time") or "").strip(),
                gps_elapsed=_f(row.get("elapsed_delta_sec")),
                source_file=csv_path,
            ))
    _fill_last_known(points)
    return points


def _fill_last_known(points: List[TrackPoint]) -> None:
    last_lat = last_lon = last_speed = None
    for p in points:
        if p.has_fix:
            last_lat, last_lon, last_speed = p.latitude, p.longitude, p.speed_kmh
        p.latitude_last = last_lat
        p.longitude_last = last_lon
        p.speed_kmh_last = last_speed


MAX_WARNINGS_KEPT = 300


def _preserve_warning_logs(output_dir: str, tag: str) -> None:
    for log_path in find_csvs(output_dir, "warnings.log"):
        try:
            os.replace(log_path, os.path.join(os.path.dirname(log_path), f"warnings_{tag}.log"))
        except OSError:
            pass


def _collect_warnings(output_dir: str) -> List[str]:
    """엔진 경고. 종류별로 앞부분만 남기고 나머지는 '외 n건'으로 접는다 - 조작된 작은 파일 하나가
    경고 100만 건을 만들어 메모리 1.2GB를 쓰고 리포트가 실패했다(리뷰 #62)."""
    messages: List[str] = []
    dropped = 0
    paths = sorted(set(find_csvs(output_dir, "warnings.log") + find_csvs(output_dir, "warnings_*.log")))
    for log_path in paths:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    if len(messages) < MAX_WARNINGS_KEPT:
                        messages.append(line.rstrip()[:500])
                    else:
                        dropped += 1
        except OSError:
            continue
    if dropped:
        messages.append(f"… 외 경고 {dropped}건 (engine_output 의 warnings 로그 참조)")
    return messages


def _out_of_range_samples(output_dir: str) -> int:
    """index.csv에서 파일 범위를 벗어난(잘린) GPS sample 수(리뷰 #40)."""
    n = 0
    for path in find_csvs(output_dir, "index.csv"):
        try:
            with open(path, newline="", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    if (row.get("validation") or "").strip().upper() == "OUT_OF_RANGE":
                        n += 1
        except (OSError, csv.Error):
            continue
    return n


# 엔진이 'GPS 메타데이터가 없는 정상 영상'을 알리는 문구들. 경로마다 문구가 달라 하나만 보면 나머지가
# '엔진 실패'로 표시됐다(리뷰 #38).
_NO_GPS_MARKERS = (
    "text track도 없고 udta 안에 mamt도 없음",
    "text track도 없고 moov 안에 udta Box도 없음",
    "text/sbtl/subt handler를 가진 Track을 찾지 못함",
    "지원 text/subtitle handler(text/sbtl/subt) Track을 하나도 찾지 못함",
    "선택된 스트림이 없습니다",
)
# 엔진이 처리를 중단했다고 알리는 문구들(산출물이 일부 있어도 실패다, 리뷰 #39).
_FAILURE_MARKERS = ("종료 코드", "예외:", "Traceback (most recent call last)", "처리할 수 없습니다", "No space left")


def _pending_track_ids(output_dir: str) -> List[int]:
    if not NEEDS_EXTRA_TRACK_PASSES:
        return []
    pending: List[int] = []
    for table_path in find_csvs(output_dir, "track_table.csv"):
        try:
            with open(table_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if (row.get("handler_type") or "").strip() not in ("text", "sbtl", "subt"):
                        continue
                    if _b(row.get("is_text_track")):
                        continue
                    track_id = _f(row.get("track"))
                    if track_id is not None:
                        pending.append(int(track_id))
        except OSError:
            continue
    return sorted(set(pending))


def run_full_extraction(input_path: str, output_dir: str, slack: bool = False,
                          timeout_sec: Optional[float] = 1800,
                          cancel_event: Optional[threading.Event] = None) -> ExtractionResult:
    routing = sniff(input_path)
    if not routing.supported:
        return ExtractionResult(
            routing=routing, points=[], engine_runs=[], used_input_path=input_path,
            status=STATUS_UNSUPPORTED, status_detail=routing.reason,
        )

    if os.path.isdir(output_dir) and os.listdir(output_dir):
        raise ValueError("분석 출력 폴더가 비어 있지 않습니다. 새 폴더를 사용하세요.")

    engine_runs = [run_engine(input_path, output_dir, slack=slack,
                              timeout_sec=timeout_sec, cancel_event=cancel_event)]

    for track_id in _pending_track_ids(output_dir):
        # 같은 out_dir에 다시 돌리면 엔진이 warnings.log를 새로 써서 주 트랙의 경고가 사라졌다
        # (리뷰 #41). 실행마다 이름을 바꿔 보존한다.
        _preserve_warning_logs(output_dir, f"run{len(engine_runs)}")
        engine_runs.append(run_engine(
            input_path, output_dir, slack=slack,
            extra_args=[f"--mp4-opt=--track-id {track_id}"],
            note=f"추가 text Track {track_id}", timeout_sec=timeout_sec,
            cancel_event=cancel_event,
        ))

    points, primary = _load_points(output_dir)
    points.sort(key=lambda p: (p.start_time_sec is None, p.start_time_sec or 0.0))
    time_source = next((p.time_source for p in points if p.time_source), "")

    status, detail = _classify_outcome(engine_runs, output_dir, points)
    # Pinned MP4 engine intentionally SKIPs standard video/audio-only files without writing CSV.
    # Only this explicit outcome plus readable duration is no_gps; arbitrary SKIP/errors are failures.
    if (status == STATUS_ENGINE_FAILED and all(r.exit_code == 0 and not r.timed_out for r in engine_runs)
            and any("text track도 없고 udta 안에 mamt도 없음" in r.stdout for r in engine_runs)):
        from core.duration import get_duration_sec
        if get_duration_sec(input_path, routing.container):
            status, detail = STATUS_NO_GPS, "지원하는 GPS 메타데이터 트랙이 없습니다."

    return ExtractionResult(
        routing=routing, points=points, engine_runs=engine_runs,
        used_input_path=input_path, primary_source_file=primary,
        time_source=time_source, warnings=_collect_warnings(output_dir),
        status=status, status_detail=detail,
        slack_points=load_slack_points(output_dir) if slack else [],
    )


def load_slack_points(output_dir: str) -> List[TrackPoint]:
    """슬랙(과거 녹화분) 카빙 결과. 본 궤적과 절대 합치지 않는다 - 이 데이터에는
    영상 재생 시각이 없어서(sample table 밖 영역이라 절대 offset만 남는다) 지도의
    시간축이나 재생 위치 동기화에 쓸 수 없다."""
    out: List[TrackPoint] = []
    for path in find_csvs(output_dir, "slack_coordinates.csv"):
        try:
            _read_slack_csv(path, out)
        except (OSError, csv.Error, UnicodeDecodeError):
            continue   # 슬랙 한 파일이 깨져도 본 분석은 계속(리뷰 #63)
    return out


def _read_slack_csv(path: str, out: List[TrackPoint]) -> None:
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(TrackPoint(
                latitude=_f(row.get("latitude")),
                longitude=_f(row.get("longitude")),
                speed_kmh=_f(row.get("speed_kmh")),
                track_deg=_f(row.get("track_deg")),
                gps_date=(row.get("date") or "").strip(),
                gps_utc_time=(row.get("utc_time") or "").strip(),
                gps_checksum_ok=_b(row.get("checksum_ok")),
                gps_trusted=(False if _b(row.get("status_valid")) is False
                             else _b(row.get("trusted"))),
                source_file=path,
            ))


def _classify_outcome(engine_runs: List[EngineRunResult], output_dir: str,
                       points: List[TrackPoint]) -> tuple[str, str]:
    timed_out = [r for r in engine_runs if r.timed_out]
    if timed_out:
        return STATUS_TIMED_OUT, f"{len(timed_out)}개 실행이 시간 초과됐습니다."

    failed = [r for r in engine_runs if r.exit_code != 0]
    if failed:
        tail = (failed[0].stderr or failed[0].stdout or "").strip().splitlines()
        return STATUS_ENGINE_FAILED, (tail[-1] if tail else f"종료 코드 {failed[0].exit_code}")

    # 엔진은 하위 스크립트 예외를 잡아 요약만 찍고 종료 코드 0으로 끝나므로 종료
    # 코드만으로는 실패를 알 수 없다. 대신 산출물로 구분한다:
    #   좌표 있음            -> 정상
    #   좌표는 없지만 다른 산출물은 있음 -> 엔진은 돌았고 이 영상에 GPS가 없는 것
    #   산출물이 아예 없음    -> 엔진이 파일을 처리하지 못한 것
    # 단, 엔진이 stdout에 중단·예외를 명시했으면 산출물이 일부 있어도 실패다(리뷰 #39).
    failure_line = _explicit_failure_line(engine_runs)
    if failure_line and not any(p.has_fix for p in points):
        return STATUS_ENGINE_FAILED, failure_line
    out_of_range = _out_of_range_samples(output_dir)
    if any(p.has_fix for p in points):
        if failure_line:
            return STATUS_ENGINE_FAILED, f"일부만 추출됨: {failure_line}"
        return STATUS_OK, ""
    if out_of_range:
        # 파일이 잘려 GPS sample이 범위 밖인 경우는 'GPS 미기록'이 아니다(리뷰 #40).
        return STATUS_ENGINE_FAILED, f"파일이 잘려 GPS sample {out_of_range}개가 파일 범위 밖입니다."

    if any(p.has_coords or p.gps_checksum_ok is False or p.gps_trusted is False for p in points):
        return STATUS_GPS_UNTRUSTED, "GPS 검증 실패 또는 유효 범위 밖 좌표"

    ran = any(find_csvs(output_dir, name) for name in
              ("stream_table.csv", "track_table.csv", "index.csv", "warnings.log", "warnings_run1.log"))
    if ran:
        return STATUS_NO_GPS, ""
    if any(marker in (run.stdout or "") for run in engine_runs for marker in _NO_GPS_MARKERS):
        return STATUS_NO_GPS, "지원하는 GPS 메타데이터 트랙이 없습니다."
    return STATUS_ENGINE_FAILED, (_first_failure_line(engine_runs)
                                   or "엔진이 산출물을 만들지 못했습니다.")


def _explicit_failure_line(engine_runs: List[EngineRunResult]) -> str:
    for run in engine_runs:
        for line in (run.stdout or "").splitlines():
            stripped = line.strip()
            if any(marker in stripped for marker in _FAILURE_MARKERS):
                return stripped[:300]
    return ""


def _first_failure_line(engine_runs: List[EngineRunResult]) -> str:
    for run in engine_runs:
        for line in (run.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("SKIP") or "찾지 못" in stripped or "처리할 수 없" in stripped:
                return stripped
        err = (run.stderr or "").strip()
        if err:
            return err.splitlines()[-1]
    return ""


def _load_points(output_dir: str, include_slack: bool = False
                  ) -> tuple[List[TrackPoint], Optional[str]]:
    timeline_paths = find_csvs(output_dir, "timeline.csv")
    primary = pick_primary_csv(timeline_paths)
    if primary is not None:
        points = load_timeline(primary)
    else:
        coord_paths = find_csvs(output_dir, "coordinates.csv")
        primary = pick_primary_csv(coord_paths)
        points = load_coordinates_as_points(primary) if primary else []
    return points, primary


def load_existing_results(output_dir: str) -> tuple[List[TrackPoint], Optional[str], str]:
    timeline_paths = find_csvs(output_dir, "timeline.csv")
    primary = pick_primary_csv(timeline_paths)
    if primary is not None:
        points = load_timeline(primary)
    else:
        coord_paths = find_csvs(output_dir, "coordinates.csv")
        primary = pick_primary_csv(coord_paths)
        points = load_coordinates_as_points(primary) if primary else []
    points.sort(key=lambda p: (p.start_time_sec is None, p.start_time_sec or 0.0))
    time_source = next((p.time_source for p in points if p.time_source), "")
    return points, primary, time_source


def detect_slack(paths: List[str], timeout_sec: float = 300,
                 cancel_event: Optional[threading.Event] = None) -> Dict[str, dict]:
    """영상마다 슬랙(컨테이너가 참조하지 않는 영역) 유무·크기. 엔진 `--detect-slack`(추출 없음)을
    서브프로세스로 돌리고 `SLACK_JSON …` 줄을 읽는다. 못 읽은 파일은 has_slack=False, error.
    cancel_event가 서면 자식 프로세스를 끝내고 CancelledError(리뷰 #43: 예전엔 취소를 눌러도
    끝날 때까지 GUI가 멈췄다)."""
    out: Dict[str, dict] = {}
    if not paths:
        return out
    argv = build_subprocess_argv(["--detect-slack", *paths])
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        return {p: {"path": p, "has_slack": False, "error": str(exc)} for p in paths}
    deadline = time.monotonic() + timeout_sec
    stdout = ""
    try:
        while True:
            try:
                stdout, _stderr = proc.communicate(timeout=0.3)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.communicate()
                    raise CancelledError("슬랙 확인이 취소되었습니다.")
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    return {p: {"path": p, "has_slack": False, "error": "슬랙 확인 시간 초과"} for p in paths}
    except CancelledError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        return {p: {"path": p, "has_slack": False, "error": str(exc)} for p in paths}
    stdout = stdout or ""
    for line in stdout.splitlines():
        if not line.startswith("SLACK_JSON "):
            continue
        try:
            info = json.loads(line[len("SLACK_JSON "):])
        except ValueError:
            continue
        if isinstance(info, dict) and info.get("path"):
            out[info["path"]] = info
    for p in paths:
        out.setdefault(p, {"path": p, "has_slack": False, "error": "감지 결과 없음"})
    return out
