"""주소 조회를 GUI 밖에서 돌리는 워커.

영상 재생 중에는 위치가 초당 수십 번 갱신되는데, 주소 조회는 네트워크 왕복이라
그때마다 동기로 부르면 창이 멈춘다. 이 워커는 요청자(owner)마다 "가장 최근 요청 하나"만
처리한다 - 조회하는 동안 같은 요청자가 보낸 중간 지점들은 버리고 마지막 것만 본다. 어차피 화면에는
현재 위치 하나만 보이므로 지나간 지점의 주소는 필요 없다. 슬롯을 하나만 두면 재생 중인 Tracker의
요청이 Location의 행 선택 요청을 덮어 '조회 중…'에서 멈췄다(리뷰 #77).

요청 간격도 최소 0.4초로 띄운다. 카카오는 짧은 시간에 몰린 요청도 429로 거절하는데,
그걸 일일 한도 초과와 구분할 수 없어서 애초에 몰리지 않게 한다.

스레드는 파이썬 데몬 스레드다. 종료할 때 조회(DNS 등)가 길어져도 기다리거나 terminate()로 끊지 않는다 -
잠금을 쥔 채 강제 종료되면 교착할 수 있고, 데몬 스레드는 프로세스가 끝날 때 같이 사라진다(리뷰 #132).
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from core import geocode

_MIN_INTERVAL_SEC = 0.4


class AddressResolver(QObject):
    # (위도, 경도, 주소). 주소가 빈 문자열이면 표시할 것이 없다는 뜻(실패 포함).
    resolved = Signal(float, float, str)
    # (상태, 서버 메시지). 한도 초과/키 오류처럼 세션 동안 계속되는 실패를 한 번만 알린다.
    failed = Signal(str, str)

    _instance: Optional["AddressResolver"] = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cond = threading.Condition()
        self._pending: Dict[str, Tuple[float, float]] = {}   # 요청자 -> 마지막 요청
        self._stopping = False
        self._failure_reported = False
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def instance(cls) -> "AddressResolver":
        if cls._instance is None:
            cls._instance = AddressResolver()
        return cls._instance

    def isRunning(self) -> bool:  # noqa: N802 - 예전 QThread 인터페이스와 같은 이름
        return self._thread is not None and self._thread.is_alive()

    def request(self, lat: float, lon: float, owner: str = "default") -> None:
        with self._cond:
            if self._stopping:
                return   # 종료 중에는 새 조회를 시작하지 않는다
            self._pending.pop(owner, None)
            self._pending[owner] = (lat, lon)   # 뒤로 보내 요청자 사이에서는 먼저 온 것부터
            self._cond.notify()
            if not self.isRunning():
                self._thread = threading.Thread(target=self._run, name="AddressResolver", daemon=True)
                self._thread.start()

    def reset(self) -> None:
        """지도 사용 방식을 다시 고른 뒤 실패 안내를 다시 할 수 있게 한다."""
        self._failure_reported = False

    def stop(self) -> None:
        """종료 신호만 보낸다. 진행 중인 조회의 결과는 버리고, 스레드가 끝나길 기다리지 않는다."""
        with self._cond:
            self._stopping = True
            self._pending.clear()
            self._cond.notify()

    def _run(self) -> None:
        last_call = 0.0
        while True:
            with self._cond:
                while not self._pending and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
            gap = _MIN_INTERVAL_SEC - (time.monotonic() - last_call)
            if gap > 0:
                time.sleep(gap)
            with self._cond:
                if self._stopping:
                    return
                if not self._pending:
                    continue
                owner = next(iter(self._pending))
                lat, lon = self._pending.pop(owner)  # 대기하는 동안 같은 요청자의 더 새 요청이 왔으면 그것

            result = geocode.describe_location(lat, lon)
            last_call = time.monotonic()
            if self._stopping:
                return   # 종료 중에 늦게 온 결과는 화면에 보내지 않는다(받을 위젯이 사라졌을 수 있다)

            if result.status in (geocode.STATUS_OK, geocode.STATUS_EMPTY):
                self.resolved.emit(lat, lon, result.address)
                continue
            if result.status in (geocode.STATUS_QUOTA, geocode.STATUS_DENIED) \
                    and not self._failure_reported:
                self._failure_reported = True
                self.failed.emit(result.status, result.message)
            self.resolved.emit(lat, lon, "")
