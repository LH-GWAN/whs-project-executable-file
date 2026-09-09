from __future__ import annotations

import hashlib
import os
from typing import Callable, Optional

ProgressCallback = Optional[Callable[[int, int], bool]]


def sha256_file(path: str, chunk_size: int = 1024 * 1024,
                progress_cb: ProgressCallback = None) -> str:
    """파일 SHA-256. progress_cb(읽은 바이트, 전체 바이트)가 False를 돌려주면 중단하고 빈 문자열.

    수 GB 영상도 있어서 화면에서 부를 때는 진행률을 보여주고 취소할 수 있어야 한다.
    """
    total = os.path.getsize(path)
    done = 0
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress_cb is not None and progress_cb(done, total) is False:
                return ""
    return digest.hexdigest()
