"""카카오맵 관련 HTTP 호출. 표준 라이브러리만 쓴다(requests 의존 추가 금지 - 번들 크기).

이 모듈은 Qt에 의존하지 않으며, 어느 스레드에서 불러도 된다. 단 네트워크를 타므로
GUI 스레드에서 직접 부르면 안 된다(ui/address_resolver.py가 워커에서 부른다).

실측한 카카오 응답(2026-09-09):
  403 {"errorType":"NotAuthorizedError","message":"App(IDAS) disabled OPEN_MAP_AND_LOCAL service."}
      -> 카카오 디벨로퍼스 앱에서 [카카오맵 > 사용 설정]이 꺼져 있음
  401 {"errorType":"AccessDeniedError","message":"domain mismatched! caller=http://127.0.0.1:48213. check out registered web domains."}
      -> Web 플랫폼 사이트 도메인 미등록 (JavaScript SDK). 포트까지 비교한다.
  401 {"errorType":"AccessDeniedError","message":"appKey(...) does not exist"}
      -> 키 오류
  429 -> 일일 한도 초과 또는 짧은 시간에 과다 요청 (카카오 문서: 로컬 API 쿼터 초과 시 429)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from core.appinfo import USER_AGENT

SDK_URL = "https://dapi.kakao.com/v2/maps/sdk.js"
COORD2ADDRESS_URL = "https://dapi.kakao.com/v2/local/geo/coord2address.json"

PROBE_TIMEOUT_SEC = 6.0
LOOKUP_TIMEOUT_SEC = 4.0

# 실패 원인 분류. 페이지(map_kakao.html)와 안내문(ui/main_window.py)이 같은 값을 본다.
KIND_OK = "ok"
KIND_QUOTA = "quota"        # 사용 한도 초과 (429)
KIND_DOMAIN = "domain"      # JS 키: 사이트 도메인 미등록
KIND_DISABLED = "disabled"  # 앱에서 카카오맵 사용 설정 꺼짐
KIND_KEY = "key"            # 키가 없거나 틀림
KIND_NETWORK = "network"    # 연결 실패 / 시간 초과
KIND_UNKNOWN = "unknown"


@dataclass
class HttpResult:
    status: int          # 0이면 연결 자체가 실패
    body: bytes = b""
    error: str = ""      # 연결 실패 사유


def _request(url: str, headers: Dict[str, str], timeout: float) -> HttpResult:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResult(resp.status, resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            body = b""
        return HttpResult(e.code, body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", None)
        return HttpResult(0, b"", str(reason if reason is not None else e))


def parse_error(body: bytes) -> Tuple[str, str]:
    """카카오 에러 본문에서 (errorType 또는 code, message)를 뽑는다. 못 읽으면 빈 문자열."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    error_type = data.get("errorType")
    if error_type is None and "code" in data:
        error_type = str(data.get("code"))
    message = data.get("message") or data.get("msg") or ""
    return str(error_type or ""), str(message)


def classify(status: int, error_type: str, message: str) -> str:
    if status == 200:
        return KIND_OK
    if status == 0:
        return KIND_NETWORK
    lowered = message.lower()
    if status == 429 or error_type == "-10" or "limit has been exceeded" in lowered or "quota" in lowered:
        return KIND_QUOTA
    if status == 401:
        return KIND_DOMAIN if "domain" in lowered else KIND_KEY
    if status == 403:
        return KIND_DISABLED if "disabled" in lowered else KIND_KEY
    return KIND_UNKNOWN


def probe_sdk(js_key: str, origin: str) -> Dict[str, Any]:
    """JavaScript SDK를 브라우저와 같은 조건(Referer=우리 페이지 출처)으로 받아 본다.

    지도 페이지에서 sdk.js 로드가 실패하면 브라우저는 이유(401/403/429)를 알려주지
    않으므로, 실패했을 때만 이 함수를 불러 정확한 원인을 안내한다. 성공 경로에서는
    부르지 않는다(SDK 로드 횟수가 한도에 잡히므로 헛호출을 만들지 않기 위함).
    """
    if not js_key:
        return {"status": 0, "kind": KIND_KEY, "errorType": "", "message": "JavaScript 키 없음"}
    query = urllib.parse.urlencode({"appkey": js_key, "autoload": "false"})
    result = _request(f"{SDK_URL}?{query}",
                      {"Referer": origin.rstrip("/") + "/"}, PROBE_TIMEOUT_SEC)
    error_type, message = ("", "") if result.status == 200 else parse_error(result.body)
    return {
        "status": result.status,
        "kind": classify(result.status, error_type, message),
        "errorType": error_type,
        "message": message or result.error,
    }


def format_address(document: Dict[str, Any]) -> str:
    """coord2address 응답 한 건을 표시용 문자열로. 지번 주소(…구 …동 번지)를 우선한다.

    검토 의견이 "서울시 XX구 XX동" 형태를 원했고, 도로명보다 지번이 동 이름을
    바로 보여준다. 지번이 없으면 도로명으로 대신한다.
    """
    address = document.get("address") or {}
    road = document.get("road_address") or {}
    return str(address.get("address_name") or road.get("address_name") or "").strip()


def coord2address(rest_key: str, lat: float, lon: float) -> Tuple[str, str, str]:
    """(kind, 주소, 메시지). kind가 ok이면 주소가 채워진다(바다 등이면 빈 문자열)."""
    if not rest_key:
        return KIND_KEY, "", "REST API 키 없음"
    query = urllib.parse.urlencode({"x": f"{lon:.6f}", "y": f"{lat:.6f}", "input_coord": "WGS84"})
    result = _request(f"{COORD2ADDRESS_URL}?{query}",
                      {"Authorization": f"KakaoAK {rest_key}"}, LOOKUP_TIMEOUT_SEC)
    if result.status != 200:
        error_type, message = parse_error(result.body)
        return classify(result.status, error_type, message), "", message or result.error
    try:
        data = json.loads(result.body.decode("utf-8"))
        documents = data.get("documents") or []
    except (ValueError, AttributeError, UnicodeDecodeError):
        return KIND_UNKNOWN, "", "응답을 해석할 수 없음"
    return KIND_OK, (format_address(documents[0]) if documents else ""), ""
