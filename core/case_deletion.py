"""사건 이력 삭제. 되돌릴 수 없는 동작이라 안전장치를 여기 한곳에 모은다.

원칙:
- 사건 폴더는 cases 루트 **바로 아래**의, 그 사건 id로 끝나는 폴더만 지운다.
  DB가 손상되거나 조작돼 output_folder가 엉뚱한 곳(홈 폴더, 루트 자체 등)을 가리켜도
  다른 곳은 절대 건드리지 않는다.
- 폴더는 **먼저 이름을 바꾼 뒤**(`<폴더>.deleting`) 지운다. Windows는 안에 열린 파일이
  있으면 이름 변경 자체가 실패하므로, 실패하면 아무것도 지워지지 않은 상태로 레코드를
  남겨 다시 시도할 수 있다. 이름 변경이 됐으면 사건은 앱에서 사라진 것이므로 레코드도
  지우고, 그 뒤 파일 삭제가 일부 실패하면 남은 `.deleting` 폴더 경로를 알려준다.
- 원본에서 복사된 읽기 전용 속성(증거 파일에 흔함) 때문에 삭제가 막히면 쓰기 가능으로
  바꿔 한 번 더 시도한다.
- 사용자가 직접 다른 곳에 저장한 리포트 PDF는 지우지 않는다(사건 폴더 밖이므로).
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass
from typing import Iterable, List, Optional

from storage.history_store import CaseRecord, HistoryStore

DELETING_SUFFIX = ".deleting"


@dataclass
class DeletionResult:
    case_id: int
    case_number: str
    folder: Optional[str]
    folder_removed: bool = False
    record_removed: bool = False
    leftover: str = ""     # 레코드는 지웠지만 파일 일부가 남은 폴더
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.record_removed and not self.error


def case_folder_of(case: CaseRecord) -> Optional[str]:
    """사건 폴더(source/, engine_output/, case.json이 있는 곳). 없으면 None.

    output_folder는 <사건 폴더>/engine_output 이므로 그 부모가 사건 폴더다.
    분석이 실패·취소돼 폴더 없이 남은 옛 레코드는 output_folder가 비어 있다.
    """
    if not case.output_folder:
        return None
    return os.path.dirname(os.path.normpath(case.output_folder))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def is_safe_case_folder(folder: str, cases_root_dir: str, case_id: int) -> bool:
    """지워도 되는 폴더인가 - cases 루트 바로 아래의 '<사건번호>_<id>' 폴더만 허용."""
    if not folder or not cases_root_dir:
        return False
    try:
        real = _norm(folder)
        root = _norm(cases_root_dir)
    except OSError:
        return False
    if real == root or os.path.dirname(real) != root:
        return False
    if not os.path.basename(real).endswith(f"_{case_id}"):
        return False
    return os.path.isdir(real) and not os.path.islink(folder)


def _make_writable_and_retry(func, path, exc_info):
    """rmtree 오류 처리기: 읽기 전용이라 실패했으면 쓰기 가능으로 바꿔 한 번 더 시도.

    Windows는 파일의 읽기 전용 속성이, POSIX는 상위 폴더의 쓰기 권한이 삭제를 막으므로
    둘 다 푼다.
    """
    exc = exc_info if isinstance(exc_info, BaseException) else exc_info[1]
    if isinstance(exc, PermissionError):
        writable = stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH
        for target in (path, os.path.dirname(path)):
            try:
                os.chmod(target, writable)
            except OSError:
                pass
        try:
            func(path)
            return
        except OSError:
            pass
    raise exc


def _rmtree(path: str) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    else:  # pragma: no cover - 구버전 시그니처
        shutil.rmtree(path, onerror=_make_writable_and_retry)


def sweep_leftovers(cases_root_dir: str) -> List[str]:
    """이전에 다 못 지운 '<폴더>.deleting' 을 다시 지워 본다. 못 지운 것의 경로를 돌려준다."""
    remaining: List[str] = []
    try:
        names = os.listdir(cases_root_dir)
    except OSError:
        return remaining
    for name in names:
        if not name.endswith(DELETING_SUFFIX):
            continue
        path = os.path.join(cases_root_dir, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            _rmtree(path)
        except OSError:
            remaining.append(path)
    return remaining


def delete_cases(store: HistoryStore, cases_root_dir: str, case_ids: Iterable[int],
                 remove_folders: bool) -> List[DeletionResult]:
    """사건들을 지운다. 각 사건의 결과를 순서대로 돌려준다(하나가 실패해도 계속 진행)."""
    results: List[DeletionResult] = []
    if remove_folders:
        sweep_leftovers(cases_root_dir)
    for case_id in case_ids:
        case = store.get_case(case_id)
        if case is None:
            results.append(DeletionResult(case_id, "", None, error="이미 삭제된 이력입니다."))
            continue
        folder = case_folder_of(case)
        result = DeletionResult(case_id, case.case_number, folder)

        if remove_folders and folder and os.path.lexists(folder):
            if not is_safe_case_folder(folder, cases_root_dir, case_id):
                result.error = f"사건 폴더가 예상 위치가 아니라 지우지 않았습니다: {folder}"
                results.append(result)
                continue
            staging = folder.rstrip("/\\") + DELETING_SUFFIX
            try:
                if os.path.lexists(staging):
                    _rmtree(staging)
                os.rename(folder, staging)
            except OSError as exc:
                # 아무것도 지워지지 않은 상태. 레코드를 남겨 다시 시도할 수 있게 한다.
                result.error = (f"사건 폴더를 지우지 못했습니다: {exc}\n"
                                "폴더 안의 파일이 다른 프로그램에서 열려 있는지 확인한 뒤 다시 시도하세요.")
                results.append(result)
                continue
            try:
                _rmtree(staging)
                result.folder_removed = True
            except OSError as exc:
                # 사건 폴더는 이미 옮겨져 앱에서는 사라졌다. 레코드는 지우되 남은 위치를 알린다.
                result.leftover = staging
                result.error = (f"파일 일부를 지우지 못해 남아 있습니다: {staging}\n({exc})\n"
                                "다음 삭제 때 다시 시도하며, 직접 지우셔도 됩니다.")

        store.delete_case(case_id)
        result.record_removed = True
        results.append(result)
    return results
