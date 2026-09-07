from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

MAP_MODE_OFFLINE = "offline"
MAP_MODE_ONLINE = "online"

# 온라인 모드에서 쓸 지도 타일 주소. {z}/{x}/{y}는 MapLibre가 채운다.
# 아직 어떤 서비스를 쓸지 정하지 않아 비워둔다 - 비어 있으면 온라인 모드를 골라도
# 외부로 요청이 나가지 않고 궤적만 표시된다(의도치 않은 외부 호출 방지).
ONLINE_TILE_URL_TEMPLATE = ""
ONLINE_TILE_ATTRIBUTION = ""
ONLINE_API_KEY = ""

_CONFIG_FILENAME = "settings.json"


def _app_data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "GPSTracer")


def _config_path() -> str:
    return os.path.join(_app_data_dir(), _CONFIG_FILENAME)


def load_config() -> Dict[str, Any]:
    try:
        with open(_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: Dict[str, Any]) -> None:
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


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


def online_tile_config() -> Dict[str, str]:
    """온라인 타일 설정. url이 비어 있으면 지도 페이지가 온라인 배경을 켜지 않는다."""
    url = ONLINE_TILE_URL_TEMPLATE
    if url and ONLINE_API_KEY:
        url = url.replace("{key}", ONLINE_API_KEY)
    return {
        "url": url if ONLINE_API_KEY or "{key}" not in ONLINE_TILE_URL_TEMPLATE else "",
        "attribution": ONLINE_TILE_ATTRIBUTION,
    }
