from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from core import kakao_api
from core.appconfig import (
    MAP_MODE_OFFLINE,
    MAP_SERVER_PREFERRED_PORTS,
    get_map_mode,
    is_online_map_ready,
    online_keys,
    online_map_config,
)
from core.basemap import basemap_path
from core.paths import resource_root

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".pmtiles": "application/octet-stream",
}

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

OFFLINE_PAGE = "map.html"        # MapLibre + PMTiles
ONLINE_PAGE = "map_kakao.html"   # 카카오맵 JavaScript SDK


def web_dir() -> str:
    return os.path.join(resource_root(), "ui", "web")


def vendor_dir() -> str:
    return os.path.join(resource_root(), "ui", "vendor")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _resolve(self, url_path: str) -> Optional[str]:
        name = url_path.lstrip("/").split("?", 1)[0]
        if not name:
            name = OFFLINE_PAGE
        if "/" in name or "\\" in name or name.startswith("."):
            return None
        if name == "basemap.pmtiles":
            return basemap_path()
        for base in (web_dir(), vendor_dir()):
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate):
                return candidate
        return None

    def _origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _send_json(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self._serve(head_only=True)

    def do_GET(self):  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route in ("/basemap-info", "/map-config"):
            payload = {
                "available": basemap_path() is not None,
                "mode": get_map_mode() or MAP_MODE_OFFLINE,
                "origin": self._origin(),
            }
            payload.update(online_map_config())
            self._send_json(payload)
            return
        if route == "/online-map-diagnose":
            # 지도 페이지에서 SDK 로드가 실패했을 때만 불린다. 브라우저는 실패 이유를
            # 숨기므로 여기서 같은 조건(Referer=우리 출처)으로 받아 보고 원인을 돌려준다.
            self._send_json(kakao_api.probe_sdk(online_keys()["js"], self._origin()))
            return
        self._serve()

    def _serve(self, head_only: bool = False):
        path = self._resolve(self.path)
        if path is None:
            self.send_error(404)
            return

        size = os.path.getsize(path)
        ctype = _MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        rng = self.headers.get("Range")

        start, end = 0, size - 1
        partial = False
        if rng:
            m = _RANGE_RE.search(rng)
            if m:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:
                    start = max(0, size - int(e))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if head_only:
            return

        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


class _Server(ThreadingHTTPServer):
    # 고정 포트를 쓰므로 주소 재사용(SO_REUSEADDR)을 끈다. 켜 두면 Windows에서 앱을
    # 두 번 띄웠을 때 둘 다 같은 포트에 묶여 요청이 엉뚱한 프로세스로 간다. 대신
    # 재시작 직후 이전 연결이 남아 bind가 거절되면 다음 후보 포트로 넘어간다.
    allow_reuse_address = False
    daemon_threads = True


def _bind_server() -> ThreadingHTTPServer:
    for port in MAP_SERVER_PREFERRED_PORTS:
        try:
            return _Server(("127.0.0.1", port), _Handler)
        except OSError:
            continue
    return _Server(("127.0.0.1", 0), _Handler)


class MapServer:

    _instance: Optional["MapServer"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._httpd = _bind_server()
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                         name="MapServer", daemon=True)
        self._thread.start()

    @classmethod
    def instance(cls) -> "MapServer":
        with cls._lock:
            if cls._instance is None:
                cls._instance = MapServer()
            return cls._instance

    @classmethod
    def shutdown_if_running(cls) -> None:
        with cls._lock:
            if cls._instance is not None:
                cls._instance.shutdown()
                cls._instance = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def on_preferred_port(self) -> bool:
        return self.port in MAP_SERVER_PREFERRED_PORTS

    def map_url(self) -> str:
        """현재 지도 사용 방식에 맞는 페이지. 온라인인데 키가 없으면 오프라인 페이지로 간다."""
        page = ONLINE_PAGE if is_online_map_ready() else OFFLINE_PAGE
        return f"{self.base_url}/{page}"

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
