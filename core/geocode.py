from __future__ import annotations

from typing import Dict, Optional, Tuple

from core.appconfig import MAP_MODE_ONLINE, get_map_mode

# 역지오코딩(위경도 -> 주소) 서비스 주소. 아직 어떤 서비스를 쓸지 정하지 않아 비워둔다.
# 비어 있으면 조회를 시도하지 않으므로 외부로 요청이 나가지 않는다.
REVERSE_GEOCODE_URL_TEMPLATE = ""
REVERSE_GEOCODE_API_KEY = ""

# 같은 좌표를 반복 조회하지 않도록 세션 동안만 기억한다. 블랙박스 GPS는 1초에 한 번
# 갱신되는데 화면은 그보다 자주 다시 그려져서, 캐시가 없으면 같은 지점을 수십 번 묻는다.
_CACHE: Dict[Tuple[float, float], str] = {}
_CACHE_PRECISION = 4  # 약 11m. 이보다 촘촘한 구분은 주소 표기에 의미가 없다.


def is_available() -> bool:
    """주소 조회가 가능한 상태인가.

    오프라인 모드에서는 절대 True가 되지 않는다 - 사건 좌표가 외부로 나가면
    안 되는 것이 오프라인을 고른 이유이기 때문이다.
    """
    return bool(REVERSE_GEOCODE_URL_TEMPLATE) and get_map_mode() == MAP_MODE_ONLINE


def describe_location(lat: Optional[float], lon: Optional[float]) -> str:
    """위경도에 대응하는 사람이 읽을 수 있는 주소. 조회할 수 없으면 빈 문자열.

    호출한 쪽은 빈 문자열을 정상 상황으로 다뤄야 한다(오프라인 모드, 미설정,
    조회 실패 모두 빈 문자열이다).
    """
    if lat is None or lon is None or not is_available():
        return ""

    key = (round(lat, _CACHE_PRECISION), round(lon, _CACHE_PRECISION))
    if key in _CACHE:
        return _CACHE[key]

    address = _lookup(lat, lon)
    _CACHE[key] = address
    return address


def _lookup(lat: float, lon: float) -> str:
    """실제 조회. 서비스가 정해지면 여기만 채우면 된다.

    주의: 화면을 그리는 스레드에서 불리므로, 실제 네트워크 호출을 넣을 때는
    반드시 워커 스레드로 옮기고 결과를 시그널로 돌려줘야 한다. 지금처럼
    동기 호출을 그대로 두면 응답이 느릴 때 창이 멈춘다.
    """
    return ""


# 표에서 좌표를 눌렀을 때 열 외부 지도. 사용자의 기본 브라우저에서 열리며,
# 프로그램이 직접 접속하는 것은 아니다(오프라인 모드에서도 사용자가 원해서
# 누른 경우이므로 막지 않는다).
EXTERNAL_MAP_URL_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={lat},{lon}"


def external_map_url(lat: float, lon: float) -> str:
    return EXTERNAL_MAP_URL_TEMPLATE.format(lat=f"{lat:.6f}", lon=f"{lon:.6f}")
