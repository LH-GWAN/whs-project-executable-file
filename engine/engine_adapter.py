from __future__ import annotations

import csv
import glob
import math
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

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

    @property
    def has_fix(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def has_gps_record(self) -> bool:
        return bool((self.gps_utc_time or "").strip() or (self.gps_date or "").strip())

    @property
    def is_dropout(self) -> bool:
        return self.has_gps_record and not self.has_fix

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


STATUS_OK = "ok"
STATUS_UNSUPPORTED = "unsupported"
STATUS_ENGINE_FAILED = "engine_failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_NO_GPS = "no_gps"


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

    @property
    def fix_count(self) -> int:
        return sum(1 for p in self.points if p.has_fix)

    @property
    def dropout_count(self) -> int:
        return sum(1 for p in self.points if p.is_dropout)

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
        if self.status == STATUS_NO_GPS:
            return "분석은 정상 완료됐지만 이 영상에는 GPS 데이터가 없습니다."
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
            if timeout_sec is not None and \
                    (datetime.now() - started_at).total_seconds() > timeout_sec:
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
        return float(value)
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
    return sorted(glob.glob(os.path.join(output_dir, "**", filename), recursive=True))


def _count_fixes(csv_path: str) -> int:
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            return sum(
                1 for row in csv.DictReader(f)
                if (row.get("latitude") or "").strip()
            )
    except OSError:
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
                latitude_last=_f(row.get("latitude_last")),
                longitude_last=_f(row.get("longitude_last")),
                speed_kmh_last=_f(row.get("speed_kmh_last")),
                x_g=_f(row.get("x_g")), y_g=_f(row.get("y_g")), z_g=_f(row.get("z_g")),
                x_g_cal=_f(row.get("x_g_cal")), y_g_cal=_f(row.get("y_g_cal")),
                z_g_cal=_f(row.get("z_g_cal")),
                source_file=csv_path,
            ))
    return points


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


def _collect_warnings(output_dir: str) -> List[str]:
    messages: List[str] = []
    for log_path in find_csvs(output_dir, "warnings.log"):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                messages.extend(line.rstrip() for line in f if line.strip())
        except OSError:
            continue
    return messages


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

    engine_runs = [run_engine(input_path, output_dir, slack=slack,
                              timeout_sec=timeout_sec, cancel_event=cancel_event)]

    for track_id in _pending_track_ids(output_dir):
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
                    source_file=path,
                ))
    return out


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
    if any(p.has_fix for p in points):
        return STATUS_OK, ""

    ran = any(find_csvs(output_dir, name) for name in
              ("stream_table.csv", "track_table.csv", "index.csv", "warnings.log"))
    if ran:
        return STATUS_NO_GPS, ""
    return STATUS_ENGINE_FAILED, (_first_failure_line(engine_runs)
                                   or "엔진이 산출물을 만들지 못했습니다.")


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
