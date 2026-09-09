from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from core.paths import resource_root

MAP_MODE_OFFLINE = "offline"
MAP_MODE_ONLINE = "online"

# 온라인 지도 제공자. 지도 표시는 카카오맵 JavaScript SDK, 좌표→주소 변환은 카카오
# 로컬 REST API를 쓴다. 그래서 키가 두 개(JavaScript 키, REST API 키) 필요하다.
# 네이티브 앱 키는 Android/iOS SDK 전용이라 데스크톱 앱에서는 쓸 곳이 없다.
ONLINE_PROVIDER = "kakao"

# 카카오 JavaScript SDK는 요청을 보낸 페이지의 출처(스킴+호스트+포트)가 카카오
# 디벨로퍼스에 등록된 Web 사이트 도메인과 정확히 일치해야만 응답한다(포트까지 본다.
# 실측: "domain mismatched! caller=http://127.0.0.1:48213"). 지도 페이지는 로컬
# 서버에서 열리므로 포트가 매번 바뀌면 등록할 방법이 없다. 그래서 아래 포트를
# 순서대로 시도한다. 셋 다 막혀 있으면 임의 포트로 뜨는데, 그 경우 온라인 지도는
# "도메인 불일치"로 거절되고 안내문이 뜬다.
MAP_SERVER_PREFERRED_PORTS = (48213, 48214, 48215)

# 배포용 키 파일(assets/ 안). git에는 절대 넣지 않고(.gitignore) 빌드할 때만 번들에
# 들어간다. 저장소가 공개돼 있어서 키가 커밋되면 누구나 이 앱의 한도를 소진시킬 수 있다.
KEYS_FILENAME = "online_keys.json"

_CONFIG_FILENAME = "settings.json"

# 설정과 키는 화면을 그릴 때마다(재생 중 초당 수십 번) 조회되므로 메모리에 들고 있는다.
# 파일이 바뀌는 경로는 save_config() 하나뿐이라 그때 같이 비운다.
_config_cache: Optional[Dict[str, Any]] = None
_keys_cache: Optional[Dict[str, str]] = None


def _app_data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "GPSTracer")


def _config_path() -> str:
    return os.path.join(_app_data_dir(), _CONFIG_FILENAME)


def load_config() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is None:
        try:
            with open(_config_path(), encoding="utf-8") as f:
                data = json.load(f)
            _config_cache = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            _config_cache = {}
    return dict(_config_cache)


def save_config(data: Dict[str, Any]) -> None:
    global _config_cache, _keys_cache
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    _config_cache = dict(data)
    _keys_cache = None


def get_map_mode() -> Optional[str]:
    """아직 고르지 않았으면 None. 첫 실행에서 물어보기 위한 구분이다."""
    mode = load_config().get("map_mode")
    return mode if mode in (MAP_MODE_OFFLINE, MAP_MODE_ONLINE) else None


def set_map_mode(mode: str) -> None:
    if mode not in (MAP_MODE_OFFLINE, MAP_MODE_ONLINE):
        return
    data = load_config()
    data["map_mode"] = mode
    save_config(data)


def keys_file_path() -> str:
    return os.path.join(resource_root(), "assets", KEYS_FILENAME)


def _load_keys_file() -> Dict[str, Any]:
    try:
        with open(keys_file_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def online_keys() -> Dict[str, str]:
    """{"js": JavaScript 키, "rest": REST API 키}. 없으면 빈 문자열.

    우선순위는 사용자 설정(settings.json의 kakao_js_key / kakao_rest_key) >
    번들된 키 파일(assets/online_keys.json). 설정을 위에 두는 이유는 exe를 다시
    빌드하지 않고도 키를 바꿀 수 있어야 해서다(키 유출로 재발급하는 경우 등).
    """
    global _keys_cache
    if _keys_cache is None:
        file_keys = _load_keys_file()
        cfg = load_config()
        _keys_cache = {
            "js": str(cfg.get("kakao_js_key") or file_keys.get("kakao_js_key") or "").strip(),
            "rest": str(cfg.get("kakao_rest_key") or file_keys.get("kakao_rest_key") or "").strip(),
        }
    return dict(_keys_cache)


def is_online_map_ready() -> bool:
    """온라인 모드이고 지도용 키까지 있어 실제로 온라인 지도를 띄울 수 있는 상태인가."""
    return get_map_mode() == MAP_MODE_ONLINE and bool(online_keys()["js"])


def online_map_config() -> Dict[str, str]:
    """지도 페이지(map_kakao.html)에 넘길 값. REST 키는 페이지에 필요 없으니 주지 않는다."""
    return {"provider": ONLINE_PROVIDER, "kakaoJsKey": online_keys()["js"]}


def mask_key(key: str) -> str:
    """진단 출력용. 키 전체를 화면/로그에 찍지 않는다."""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"
