"""주소 조회를 GUI 밖에서 돌리는 워커.

영상 재생 중에는 위치가 초당 수십 번 갱신되는데, 주소 조회는 네트워크 왕복이라
그때마다 동기로 부르면 창이 멈춘다. 이 워커는 "가장 최근 요청 하나"만 처리한다 -
조회하는 동안 들어온 중간 지점들은 버리고 마지막 것만 본다. 어차피 화면에는 현재
위치 하나만 보이므로 지나간 지점의 주소는 필요 없다.

요청 간격도 최소 0.4초로 띄운다. 카카오는 짧은 시간에 몰린 요청도 429로 거절하는데,
그걸 일일 한도 초과와 구분할 수 없어서 애초에 몰리지 않게 한다.
"""
from __future__ import annotations

import threading
import time
from typing import Optional, Tuple

from PySide6.QtCore import QThread, Signal

from core import geocode

_MIN_INTERVAL_SEC = 0.4


class AddressResolver(QThread):
    # (위도, 경도, 주소). 주소가 빈 문자열이면 표시할 것이 없다는 뜻(실패 포함).
    resolved = Signal(float, float, str)
    # (상태, 서버 메시지). 한도 초과/키 오류처럼 세션 동안 계속되는 실패를 한 번만 알린다.
    failed = Signal(str, str)

    _instance: Optional["AddressResolver"] = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cond = threading.Condition()
        self._pending: Optional[Tuple[float, float]] = None
        self._stopping = False
        self._failure_reported = False

    @classmethod
    def instance(cls) -> "AddressResolver":
        if cls._instance is None:
            cls._instance = AddressResolver()
        return cls._instance

    def request(self, lat: float, lon: float) -> None:
        with self._cond:
            self._pending = (lat, lon)
            self._cond.notify()
        if not self.isRunning():
            self.start()

    def reset(self) -> None:
        """지도 사용 방식을 다시 고른 뒤 실패 안내를 다시 할 수 있게 한다."""
        self._failure_reported = False

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()
        if self.isRunning():
            # 조회 시간 제한(4초)보다 길게 기다린다. 요청이 진행 중일 때 짧게 끊으면
            # 종료 시점에 QThread가 살아 있는 채로 파괴돼 크래시가 난다.
            self.wait(8000)

    def run(self) -> None:  # noqa: D401
        last_call = 0.0
        while True:
            with self._cond:
                while self._pending is None and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
            gap = _MIN_INTERVAL_SEC - (time.monotonic() - last_call)
            if gap > 0:
                time.sleep(gap)
            with self._cond:
                if self._stopping:
                    return
                lat, lon = self._pending  # 대기하는 동안 더 새 요청이 왔으면 그것을 쓴다
                self._pending = None

            result = geocode.describe_location(lat, lon)
            last_call = time.monotonic()

            if result.status in (geocode.STATUS_OK, geocode.STATUS_EMPTY):
                self.resolved.emit(lat, lon, result.address)
                continue
            if result.status in (geocode.STATUS_QUOTA, geocode.STATUS_DENIED) \
                    and not self._failure_reported:
                self._failure_reported = True
                self.failed.emit(result.status, result.message)
            self.resolved.emit(lat, lon, "")
