"""손상 파일 안내 - 분석 전에 손상 내용을 보이고 복구 방식을 고르게 한다.

사용자 결정(2026-10-11): '복구하지 않음'을 추천으로 둔다(잘못 복구될 가능성이 있어서). 헤더가 사라진 파일은
같은 기기의 정상 영상을 자동으로 찾아 쓰고, 없으면 사용자에게 묻는다.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PySide6.QtWidgets import QFileDialog, QMessageBox

from core.damage import DamageReport
from core.damage_marks import MODE_INTERPOLATE, MODE_NONE

VIDEO_FILTER = "영상 (*.avi *.AVI *.mp4 *.MP4 *.mov *.MOV *.m4v *.M4V);;모든 파일 (*)"


def ask_recovery_mode(parent, report: DamageReport, label: str = "") -> Optional[str]:
    """손상 파일임을 알리고 복구 방식을 고른다. MODE_NONE / MODE_INTERPOLATE, 취소면 None.
    복구할 수 없는 파일이면 안내만 하고 None."""
    name = os.path.basename(report.path)
    title = f"손상 파일{(' - ' + label) if label else ''}"
    if not report.recoverable:
        QMessageBox.critical(
            parent, title,
            f"{name}\n\n손상으로 인해 파싱할 수 없습니다.\n\n" + "\n".join(f"· {line}" for line in report.summary_lines())
            + "\n\n파일 전체를 훑었지만 영상 데이터 영역(미디어)의 크기를 확정할 수 없어 임의 복구도 할 수 없습니다.")
        return None
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(title)
    box.setText(f"{name}\n\n손상된 파일입니다. 어떻게 분석할까요?")
    lines = [f"· {line}" for line in report.summary_lines()]
    detail = "\n".join(lines) + (
        "\n\n복구하지 않음(추천): 손상 구간은 비워 두고 지도·그래프·표에 '손상'으로 표시합니다. "
        "잘못 복구될 가능성이 없습니다.\n"
        "임의 복구: 손상 직전 좌표와 손상 뒤 첫 좌표를 직선으로 이어 추정 좌표를 넣습니다(지도에 다른 색). "
        "속도는 만들지 않으며, 두 좌표가 직전 속도로 갈 수 있는 거리를 넘으면 복구하지 않고 알립니다.\n"
        "영상은 원본을 그대로 재생합니다(손상 구간은 깨져 보일 수 있습니다).")
    box.setInformativeText(detail)
    none_btn = box.addButton("복구하지 않음 (추천)", QMessageBox.AcceptRole)
    interp_btn = box.addButton("임의 복구", QMessageBox.AcceptRole)
    box.addButton("취소", QMessageBox.RejectRole)
    box.setDefaultButton(none_btn)
    box.exec()
    clicked = box.clickedButton()
    if clicked is none_btn:
        return MODE_NONE
    if clicked is interp_btn:
        return MODE_INTERPOLATE
    return None


def ask_reference(parent, report: DamageReport, candidates: List[str]) -> Tuple[Optional[str], bool]:
    """헤더가 사라진 파일의 참조 영상. (경로 또는 "" (참조 없이), 취소 여부)."""
    name = os.path.basename(report.path)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Question)
    box.setWindowTitle("헤더 참조 영상")
    if candidates:
        box.setText(f"{name}의 헤더(영상 설정)가 사라졌습니다.\n같은 폴더에서 같은 기기의 정상 영상을 찾았습니다:\n"
                    f"{os.path.basename(candidates[0])}")
        box.setInformativeText("이 영상의 헤더(스트림 구성·코덱·FPS)만 이식해 분석·재생합니다. 영상·GPS 데이터는 손상 파일 것만 씁니다. "
                               "이식 사실은 사건 정보와 리포트에 남깁니다.")
        use_btn = box.addButton("이 영상 사용", QMessageBox.AcceptRole)
    else:
        box.setText(f"{name}의 헤더(영상 설정)가 사라졌습니다.\n같은 폴더에서 같은 기기의 정상 영상을 찾지 못했습니다.")
        box.setInformativeText("같은 기기·같은 녹화 종류(상시/이벤트)의 정상 영상을 고르면 그 헤더를 이식해 분석·재생합니다. "
                               "참조 없이 진행하면 영상은 재생되지 않고 GPS만 UTC 기준 시간축으로 회수합니다.")
        use_btn = None
    pick_btn = box.addButton("다른 파일 선택" if candidates else "파일 선택", QMessageBox.AcceptRole)
    skip_btn = box.addButton("참조 없이 진행 (GPS만)", QMessageBox.AcceptRole)
    box.addButton("취소", QMessageBox.RejectRole)
    box.setDefaultButton(use_btn or pick_btn)
    box.exec()
    clicked = box.clickedButton()
    if use_btn is not None and clicked is use_btn:
        return candidates[0], False
    if clicked is pick_btn:
        path, _ = QFileDialog.getOpenFileName(parent, "같은 기기의 정상 영상 선택", os.path.dirname(report.path), VIDEO_FILTER)
        if not path:
            return None, True
        return path, False
    if clicked is skip_btn:
        return "", False
    return None, True


def confirm_reference_problem(parent, reason: str) -> bool:
    """참조 검증 실패 안내. True면 참조 없이 계속, False면 취소."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("헤더 참조 영상")
    box.setText("고른 참조 영상을 쓸 수 없습니다.")
    box.setInformativeText(reason + "\n\n참조 없이 진행하면 영상은 재생되지 않고 GPS만 UTC 기준으로 회수합니다.")
    go = box.addButton("참조 없이 진행", QMessageBox.AcceptRole)
    box.addButton("취소", QMessageBox.RejectRole)
    box.exec()
    return box.clickedButton() is go
