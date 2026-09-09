"""사건 이력 삭제. 되돌릴 수 없는 동작이라 안전장치를 여기 한곳에 모은다.

원칙:
- 사건 폴더는 cases 루트 **바로 아래**의, 그 사건 id로 끝나는 폴더만 지운다.
  DB가 손상되거나 조작돼 output_folder가 엉뚱한 곳(홈 폴더, 루트 자체 등)을 가리켜도
  다른 곳은 절대 건드리지 않는다.
- 폴더를 먼저 지우고 성공했을 때만 레코드를 지운다. 폴더 삭제가 실패하면(다른 프로그램이
  파일을 열고 있는 등) 레코드를 남겨 나중에 다시 시도할 수 있게 한다.
- 사용자가 직접 다른 곳에 저장한 리포트 PDF는 지우지 않는다(사건 폴더 밖이므로).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Iterable, List, Optional

from storage.history_store import CaseRecord, HistoryStore


@dataclass
class DeletionResult:
    case_id: int
    case_number: str
    folder: Optional[str]
    folder_removed: bool = False
    record_removed: bool = False
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


def delete_cases(store: HistoryStore, cases_root_dir: str, case_ids: Iterable[int],
                 remove_folders: bool) -> List[DeletionResult]:
    """사건들을 지운다. 각 사건의 결과를 순서대로 돌려준다(하나가 실패해도 계속 진행)."""
    results: List[DeletionResult] = []
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
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                result.error = (f"사건 폴더를 지우지 못했습니다: {exc}\n"
                                "폴더 안의 파일이 다른 프로그램에서 열려 있는지 확인한 뒤 다시 시도하세요.")
                results.append(result)
                continue
            result.folder_removed = True

        store.delete_case(case_id)
        result.record_removed = True
        results.append(result)
    return results
