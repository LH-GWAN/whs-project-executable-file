"""사건 정보(사건번호·담당자·메모) 수정 창. History 우클릭 > 사건 정보 수정."""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)


class CaseEditDialog(QDialog):
    def __init__(self, case_number: str, examiner: str, memo: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("사건 정보 수정")
        self.setModal(True)
        self.setMinimumWidth(420)

        title = QLabel("사건 정보 수정")
        title.setProperty("role", "title")

        self._case_number = QLineEdit(case_number)
        self._examiner = QLineEdit(examiner)
        self._memo = QTextEdit()
        self._memo.setPlainText(memo)
        self._memo.setFixedHeight(90)

        form = QFormLayout()
        form.addRow("사건번호", self._case_number)
        form.addRow("담당자", self._examiner)
        form.addRow("메모", self._memo)

        note = QLabel("사건 폴더 이름과 원본 사본·분석 결과는 바뀌지 않습니다. 이력 목록과 case.json, 리포트에 반영됩니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #666; font-size: 11px;")

        save_btn = QPushButton("저장")
        save_btn.setProperty("role", "primary")
        save_btn.setDefault(True)
        save_btn.setAutoDefault(True)
        save_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("취소")
        cancel_btn.setAutoDefault(False)
        cancel_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addLayout(buttons)

        self.result_values: Optional[Tuple[str, str, str]] = None

    def _on_save(self) -> None:
        number = self._case_number.text().strip()
        if not number:
            self._case_number.setStyleSheet("border: 1px solid red;")
            return
        self.result_values = (number, self._examiner.text().strip(), self._memo.toPlainText().strip())
        self.accept()
