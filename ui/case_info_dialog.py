from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from core.driving_events import DEFAULT_VEHICLE, VEHICLE_LABELS, VEHICLE_TYPES, criteria_lines


@dataclass
class CaseInfoInput:
    case_number: str
    examiner: str
    memo: str
    settings: Dict
    vehicle_type: str


class CaseInfoDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Case Information")
        self.setModal(True)
        self.setMinimumWidth(360)

        self._case_number = QLineEdit()
        self._case_number.textChanged.connect(lambda _t: self._case_number.setStyleSheet(""))
        self._tracker_cb = QCheckBox("Tracker")
        self._speed_cb = QCheckBox("Speed")
        self._location_cb = QCheckBox("Location")
        for cb in (self._tracker_cb, self._speed_cb, self._location_cb):
            cb.setChecked(True)
        self._examiner = QLineEdit()
        self._memo = QTextEdit()
        self._memo.setFixedHeight(70)
        # 위험운전 판별 기준(국토부 DTG 기준표)이 차종마다 다르다. 승용차는 택시 기준을 쓴다.
        self._vehicle = QComboBox()
        for code in VEHICLE_TYPES:
            self._vehicle.addItem(VEHICLE_LABELS[code], code)
            self._vehicle.setItemData(self._vehicle.count() - 1,
                                      "\n".join(criteria_lines(code)), Qt.ToolTipRole)
        self._vehicle.setCurrentIndex(VEHICLE_TYPES.index(DEFAULT_VEHICLE))
        self._vehicle.setToolTip("위험운전 행동(급가속·급감속·급회전 등)을 이 차종의 기준으로 판정합니다.\n"
                                 "국토교통부 DTG 위험운전행동 판별 기준(2022), 승용차는 택시 기준")

        title = QLabel("Case Information")
        title.setProperty("role", "title")

        form = QFormLayout()
        form.addRow("Case Number", self._case_number)

        analysis_row = QHBoxLayout()
        analysis_row.addWidget(self._tracker_cb)
        analysis_row.addWidget(self._speed_cb)
        analysis_row.addWidget(self._location_cb)
        form.addRow("Analysis Setting", analysis_row)

        form.addRow("Examiner", self._examiner)
        form.addRow("memo", self._memo)
        form.addRow("차종", self._vehicle)

        start_btn = QPushButton("Start")
        start_btn.setProperty("role", "primary")
        # Enter를 치면 Start가 눌리게 한다. 기본 버튼을 안 정하면 Qt가 먼저 만든 버튼
        # (여기선 Cancel)을 autoDefault로 골라, 사건번호만 치고 Enter를 누르면 취소됐다.
        start_btn.setDefault(True)
        start_btn.setAutoDefault(True)
        start_btn.clicked.connect(self._on_start)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        cancel_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(start_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(form)
        layout.addLayout(btn_row)

        self.result_input: Optional[CaseInfoInput] = None

    def _on_start(self) -> None:
        if not self._case_number.text().strip():
            self._case_number.setStyleSheet("border: 1px solid red;")
            return
        if not any(cb.isChecked() for cb in (self._tracker_cb, self._speed_cb, self._location_cb)):
            QMessageBox.warning(self, "분석 항목", "분석 항목을 최소 한 개 선택하세요.")
            return
        self.result_input = CaseInfoInput(
            case_number=self._case_number.text().strip(),
            examiner=self._examiner.text().strip(),
            memo=self._memo.toPlainText().strip(),
            settings={
                "tracker": self._tracker_cb.isChecked(),
                "speed": self._speed_cb.isChecked(),
                "location": self._location_cb.isChecked(),
                "vehicle_type": self._vehicle.currentData(),
            },
            vehicle_type=self._vehicle.currentData(),
        )
        self.accept()
