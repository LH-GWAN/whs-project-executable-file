from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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

# 팀 공용 키 파일을 받을 곳. 배경지도(core/basemap.py의 BASEMAP_DOWNLOAD_URL)와 같은
# 방식으로 외부 공유 링크에 올려두고, 안내 창의 [키 파일 내려받기] 버튼이 연다.
# 링크를 아는 사람만 받을 수 있고 저장소에는 키가 남지 않는다. 파일을 옮기면 여기만 바꾼다.
# 비워 두면 안내 창에서 내려받기 버튼과 문구가 빠지고 직접 발급 절차만 남는다.
ONLINE_KEYS_DOWNLOAD_URL = (
    "https://drive.google.com/file/d/1OnogXYR8ZCImaeHLQbt_R13Au-nhXr1f/view?usp=sharing"
)

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


def keys_dir() -> str:
    """키 파일을 찾는 폴더. 소스 실행은 프로젝트의 assets/, exe는 _internal/assets/."""
    return os.path.join(resource_root(), "assets")


def keys_file_path() -> str:
    """키 파일의 기본 위치(새로 만들 때 쓰는 이름). 읽을 때는 폴더 안을 더 넓게 찾는다."""
    return os.path.join(keys_dir(), KEYS_FILENAME)


# 카카오 앱 키는 영문 소문자·숫자 32자다. 형식이 안 맞으면(예시 문구, 공백, 따옴표 포함)
# 키로 치지 않는다 - 그래야 예시 파일이나 잘못 붙여 넣은 값을 "키 없음"으로 정확히 안내한다.
_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_KEY_FILE_BYTES = 64 * 1024
_RESCAN_INTERVAL_SEC = 2.0


@dataclass
class KeyFileInfo:
    path: str
    js: str = ""
    rest: str = ""
    problem: str = ""  # 비어 있으면 사용 가능

    @property
    def usable(self) -> bool:
        return not self.problem and bool(self.js)


def is_valid_key(value: Any) -> bool:
    return isinstance(value, str) and bool(_KEY_RE.match(value.strip().lower()))


def _clean_key(value: Any) -> str:
    return value.strip().lower() if is_valid_key(value) else ""


def _keys_from_text(text: str) -> tuple:
    """(js, rest, problem). JSON으로 읽되, BOM·따옴표 깨짐 등으로 실패하면 이름 뒤의 키 값을 훑는다."""
    data: Dict[str, Any] = {}
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            data = loaded
    except ValueError:
        pass
    js_raw = data.get("kakao_js_key")
    rest_raw = data.get("kakao_rest_key")
    if js_raw is None and rest_raw is None:
        # JSON이 아니거나 키 이름이 없다. 이름 뒤에 오는 32자 값을 직접 찾는다.
        m_js = re.search(r"kakao_js_key\W+([0-9a-fA-F]{32})", text)
        m_rest = re.search(r"kakao_rest_key\W+([0-9a-fA-F]{32})", text)
        js_raw = m_js.group(1) if m_js else None
        rest_raw = m_rest.group(1) if m_rest else None
        if js_raw is None and rest_raw is None:
            if "kakao" not in text.lower():
                return "", "", "카카오 키 파일이 아닙니다"
            return "", "", "kakao_js_key / kakao_rest_key 값을 찾지 못했습니다"
    js = _clean_key(js_raw)
    rest = _clean_key(rest_raw)
    if not js:
        if js_raw in (None, ""):
            return js, rest, "kakao_js_key 값이 비어 있습니다"
        return js, rest, f"kakao_js_key 값이 키 형식(영문 소문자·숫자 32자)이 아닙니다: {str(js_raw)[:40]!r}"
    if not rest:
        # 지도는 뜨고 주소만 안 되는 상태. 문제로 막지 않고 이유만 남긴다.
        return js, rest, ""
    return js, rest, ""


def scan_key_files() -> List[KeyFileInfo]:
    """assets/ 안에서 키 파일 후보를 전부 읽어 본다. online_keys.json이 먼저, 나머지는 이름순.

    이름을 정확히 맞추지 못한 경우(online_keys (1).json, online_keys.json.txt, 예시 파일에
    값을 넣은 경우)도 잡는다. 어떤 파일을 왜 못 썼는지 problem에 남겨 안내 창에 보여준다.
    """
    base = keys_dir()
    try:
        names = os.listdir(base)
    except OSError:
        return []
    candidates = []
    for name in names:
        lower = name.lower()
        if not (lower.endswith(".json") or lower.endswith(".txt")):
            continue
        if lower == "readme.txt":
            continue
        candidates.append(name)
    candidates.sort(key=lambda n: (0 if n.lower() == KEYS_FILENAME else 1, n.lower()))

    infos: List[KeyFileInfo] = []
    for name in candidates:
        path = os.path.join(base, name)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > _MAX_KEY_FILE_BYTES:
                continue
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as exc:
            infos.append(KeyFileInfo(path, problem=f"읽지 못했습니다: {exc}"))
            continue
        text = raw.decode("utf-8-sig", errors="replace")
        if "kakao" not in text.lower():
            continue
        js, rest, problem = _keys_from_text(text)
        infos.append(KeyFileInfo(path, js, rest, problem))
    return infos


def _keys_signature() -> tuple:
    """키 파일 목록과 수정 시각. 바뀌면 캐시를 버린다(프로그램을 다시 켜지 않아도 인식)."""
    sig = []
    base = keys_dir()
    try:
        for name in sorted(os.listdir(base)):
            lower = name.lower()
            if lower.endswith(".json") or lower.endswith(".txt"):
                try:
                    st = os.stat(os.path.join(base, name))
                    sig.append((name, st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
    except OSError:
        pass
    return tuple(sig)


_keys_signature_cache: Optional[tuple] = None
_keys_checked_at = 0.0


def online_keys(force: bool = False) -> Dict[str, str]:
    """{"js": JavaScript 키, "rest": REST API 키, "source": 어디서 읽었는지}. 없으면 빈 문자열.

    우선순위는 사용자 설정(settings.json의 kakao_js_key / kakao_rest_key, 키 형식일 때만) >
    assets/ 안의 키 파일. 설정을 위에 두는 이유는 exe를 다시 빌드하지 않고도 키를 바꿀 수
    있어야 해서다. 파일이 바뀌면 2초 안에 다시 읽으므로 재시작이 필요 없다.
    """
    global _keys_cache, _keys_signature_cache, _keys_checked_at
    now = time.monotonic()
    if force or _keys_cache is None or now - _keys_checked_at >= _RESCAN_INTERVAL_SEC:
        _keys_checked_at = now
        sig = _keys_signature()
        if force or _keys_cache is None or sig != _keys_signature_cache:
            _keys_signature_cache = sig
            cfg = load_config()
            js = _clean_key(cfg.get("kakao_js_key"))
            rest = _clean_key(cfg.get("kakao_rest_key"))
            source = "settings.json" if js else ""
            if not js:
                for info in scan_key_files():
                    if info.usable:
                        js, rest, source = info.js, (info.rest or rest), info.path
                        break
            _keys_cache = {"js": js, "rest": rest, "source": source}
    return dict(_keys_cache)


def key_status_report() -> str:
    """안내 창·진단용. 어디를 봤고 어떤 파일을 왜 못 썼는지 사람이 읽을 수 있게."""
    keys = online_keys(force=True)
    lines = [f"키 파일을 찾는 폴더: {keys_dir()}"]
    if keys["js"]:
        lines.append(f"사용 중인 키: {keys['source']} (JavaScript {mask_key(keys['js'])}, "
                     f"REST {mask_key(keys['rest']) or '없음'})")
    infos = scan_key_files()
    if not infos:
        lines.append("  이 폴더에 카카오 키 파일이 없습니다 (online_keys.json 을 넣으세요)")
    for info in infos:
        name = os.path.basename(info.path)
        if info.usable:
            note = "사용 가능" + ("" if info.rest else " (REST API 키 없음 - 주소 표시 안 됨)")
        else:
            note = info.problem
        lines.append(f"  - {name}: {note}")
    cfg = load_config()
    if cfg.get("kakao_js_key") and not _clean_key(cfg.get("kakao_js_key")):
        lines.append("  - settings.json 의 kakao_js_key 값이 키 형식이 아니라 무시했습니다")
    return "\n".join(lines)


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
