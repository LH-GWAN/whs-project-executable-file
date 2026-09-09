"""위경도 -> 주소 변환(카카오 로컬 API). 온라인 모드에서만 동작한다.

호출 규칙:
- describe_location()은 네트워크를 타므로 GUI 스레드에서 부르지 말 것.
  화면은 cached_address()로 즉시 확인하고, 없으면 ui/address_resolver.py에 맡긴다.
- 오프라인 모드에서는 어떤 경우에도 외부 요청이 나가지 않는다. 사건 좌표가
  밖으로 나가면 안 되는 것이 오프라인을 고른 이유이기 때문이다.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from core import kakao_api
from core.appconfig import MAP_MODE_ONLINE, get_map_mode, online_keys

STATUS_OK = "ok"
STATUS_EMPTY = "empty"              # 조회는 됐지만 주소가 없는 지점(바다 등)
STATUS_QUOTA = "quota"              # 사용 한도 초과 - 이 세션 동안 다시 시도하지 않는다
STATUS_DENIED = "denied"            # 키 오류 / 사용 설정 꺼짐 - 마찬가지로 중단
STATUS_NETWORK = "network"          # 일시적 실패 - 다음 지점에서 다시 시도
STATUS_UNAVAILABLE = "unavailable"  # 오프라인 모드거나 키 없음


@dataclass(frozen=True)
class LookupResult:
    status: str
    address: str = ""
    message: str = ""


# 같은 좌표를 반복 조회하지 않도록 세션 동안만 기억한다. 블랙박스 GPS는 1초에 한 번
# 갱신되는데 화면은 그보다 자주 다시 그려져서, 캐시가 없으면 같은 지점을 수십 번 묻는다.
_CACHE_PRECISION = 4  # 약 11m. 이보다 촘촘한 구분은 주소 표기에 의미가 없다.
_cache: Dict[Tuple[float, float], str] = {}
_lock = threading.Lock()

# 한도 초과나 키 오류가 한 번 나면 세션 동안 더 묻지 않는다. 한도 초과 상태에서
# 계속 두드리면 안내문만 반복되고, 카카오 쪽 집계에도 실패 호출이 쌓인다.
# 사용자가 지도 사용 방식을 다시 고르면(reset_block) 재시도한다.
_block: Optional[LookupResult] = None


def cache_key(lat: float, lon: float) -> Tuple[float, float]:
    return (round(lat, _CACHE_PRECISION), round(lon, _CACHE_PRECISION))


def cached_address(lat: float, lon: float) -> Optional[str]:
    """이미 조회한 지점이면 주소(없는 지점이면 빈 문자열), 아니면 None. 네트워크 없음."""
    with _lock:
        return _cache.get(cache_key(lat, lon))


def is_available() -> bool:
    """주소 조회가 가능한 상태인가. 오프라인 모드에서는 절대 True가 되지 않는다."""
    if get_map_mode() != MAP_MODE_ONLINE or not online_keys()["rest"]:
        return False
    with _lock:
        return _block is None


def blocked() -> Optional[LookupResult]:
    with _lock:
        return _block


def reset_block() -> None:
    global _block
    with _lock:
        _block = None


def describe_location(lat: Optional[float], lon: Optional[float]) -> LookupResult:
    """실제 조회. 워커 스레드 전용. 호출한 쪽은 ok/empty 이외를 모두 '주소 없음'으로 그리면 된다."""
    if lat is None or lon is None or not is_available():
        return LookupResult(STATUS_UNAVAILABLE)

    key = cache_key(lat, lon)
    with _lock:
        if key in _cache:
            address = _cache[key]
            return LookupResult(STATUS_OK if address else STATUS_EMPTY, address)

    kind, address, message = kakao_api.coord2address(online_keys()["rest"], lat, lon)
    return _apply(key, kind, address, message)


def _apply(key: Tuple[float, float], kind: str, address: str, message: str) -> LookupResult:
    global _block
    with _lock:
        if kind == kakao_api.KIND_OK:
            _cache[key] = address
            return LookupResult(STATUS_OK if address else STATUS_EMPTY, address)
        if kind == kakao_api.KIND_QUOTA:
            _block = LookupResult(STATUS_QUOTA, "", message)
            return _block
        if kind in (kakao_api.KIND_DOMAIN, kakao_api.KIND_DISABLED, kakao_api.KIND_KEY):
            _block = LookupResult(STATUS_DENIED, "", message)
            return _block
        return LookupResult(STATUS_NETWORK, "", message)


# 표에서 좌표를 눌렀을 때 열 외부 지도. 사용자의 기본 브라우저에서 열리며,
# 프로그램이 직접 접속하는 것은 아니다(오프라인 모드에서도 사용자가 원해서
# 누른 경우이므로 막지 않는다).
EXTERNAL_MAP_URL_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={lat},{lon}"


def external_map_url(lat: float, lon: float) -> str:
    return EXTERNAL_MAP_URL_TEMPLATE.format(lat=f"{lat:.6f}", lon=f"{lon:.6f}")
