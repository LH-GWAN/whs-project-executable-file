from __future__ import annotations

import os
import shutil
import zipfile
from typing import Optional

from core.paths import resource_root

# 배경지도를 내려받을 곳. 파일이 수십~수백 MB라 git으로는 배포할 수 없다
# (GitHub 웹 업로드는 25MB에서 막히고, 일반 파일은 100MB 제한).
# 그래서 외부 공유 링크에 올려두고 받아 쓴다. 파일을 옮기면 이 주소만 바꾸면 된다.
BASEMAP_DOWNLOAD_URL = (
    "https://drive.google.com/file/d/1WEV52yIpNVDtSla2f5ZLGmWk_wE0SCic/view?usp=drive_link"
)


def assets_dir() -> str:
    return os.path.join(resource_root(), "assets")


def basemap_path() -> Optional[str]:
    # 파일명을 고정하지 않는다. 관할 구역만 잘라 만든 지도(seoul.pmtiles 등)를
    # 이름 안 바꾸고 그대로 넣을 수 있게 assets 안의 .pmtiles를 찾아 쓴다.
    # 여러 개면 큰 것(더 넓거나 상세한 쪽)을 고른다.
    base = assets_dir()
    try:
        found = [os.path.join(base, n) for n in os.listdir(base)
                 if n.lower().endswith(".pmtiles")]
    except OSError:
        return None
    found = [p for p in found if os.path.isfile(p)]
    return max(found, key=os.path.getsize) if found else None


def extract_zipped_basemap() -> Optional[str]:
    """assets 안에 .pmtiles는 없고 .zip만 있으면 한 번 풀어준다.

    배포처에 올릴 때 확장자 때문에 거절당하면 zip으로 감싸게 되는데, 받는 사람이
    압축 푸는 걸 잊으면 "지도가 안 나온다"로 이어진다. PMTiles는 내부가 이미
    gzip이라 zip으로 용량이 줄지도 않으므로, 포장을 푸는 건 프로그램이 대신 한다.

    Qt에 의존하지 않는다 - app.py가 Qt를 import 하기 전에 부를 수 있어야 하기
    때문이다. UI 초기화가 막히는 환경에서도 지도는 준비돼 있어야 한다.
    """
    if basemap_path() is not None:
        return None

    base = assets_dir()
    try:
        zips = [os.path.join(base, n) for n in os.listdir(base)
                if n.lower().endswith(".zip")]
    except OSError:
        return None

    for archive in sorted(zips, key=os.path.getsize, reverse=True):
        try:
            with zipfile.ZipFile(archive) as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".pmtiles")]
                if not members:
                    continue
                member = max(members, key=lambda m: zf.getinfo(m).file_size)
                target = os.path.join(base, os.path.basename(member))
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                return target
        except (zipfile.BadZipFile, OSError, PermissionError):
            continue
    return None
