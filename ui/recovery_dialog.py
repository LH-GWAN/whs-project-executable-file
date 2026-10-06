"""Explicit recovery workflow, independent of evidentiary GPS/video synchronization."""
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                              QLineEdit, QMessageBox, QPushButton, QVBoxLayout)
from core.avi_recovery import recover_avi, TIMING_WARNING
from ui.workers import TaskWorker


class RecoveryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('손상 AVI 복원')
        self.resize(680, 330)
        self._worker = None
        self._output = ''
        layout = QVBoxLayout(self)
        notice = QLabel('AVI의 남아 있는 MJPEG/H.264 영상과 NMEA GPS를 회수합니다.\n'
                        '헤더가 소실됐으면 동일 기기·해상도·코덱·FPS·채널 순서의 정상 AVI를 지정하세요.\n'
                        '참조가 없으면 가능한 JPEG 정지영상과 GPS만 회수합니다. MP4는 지원하지 않습니다.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self._source = self._path_row(form, '손상 AVI', False)
        self._reference = self._path_row(form, '정상 참조 AVI (선택)', False)
        self._destination = self._path_row(form, '결과 저장 위치', True)
        layout.addLayout(form)
        warning = QLabel(TIMING_WARNING)
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self._status = QLabel('원본은 수정하지 않습니다. 선택한 위치에 새 결과 폴더를 만듭니다.')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        buttons = QHBoxLayout()
        self._start = QPushButton('복원 시작')
        self._start.clicked.connect(self._run)
        self._open = QPushButton('결과 폴더 열기')
        self._open.setEnabled(False)
        self._open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self._output)))
        self._cancel = QPushButton('닫기')
        self._cancel.clicked.connect(self.reject)
        for b in (self._start, self._open, self._cancel):
            buttons.addWidget(b)
        layout.addLayout(buttons)

    def _path_row(self, form, title, directory):
        edit = QLineEdit()
        button = QPushButton('찾기')
        def choose():
            value = (QFileDialog.getExistingDirectory(self, title) if directory else
                     QFileDialog.getOpenFileName(self, title, '', 'AVI (*.avi);;모든 파일 (*)')[0])
            if value:
                edit.setText(value)
        button.clicked.connect(choose)
        row = QHBoxLayout()
        row.addWidget(edit)
        row.addWidget(button)
        form.addRow(title, row)
        return edit

    def _run(self):
        source, reference = self._source.text().strip(), self._reference.text().strip()
        destination = self._destination.text().strip()
        if not source or not destination:
            QMessageBox.warning(self, '복원', '손상 AVI와 결과 저장 위치를 선택하세요.')
            return
        self._output = str(Path(destination)/('avi_recovery_'+uuid4().hex[:12]))
        output = self._output
        self._start.setEnabled(False)
        self._open.setEnabled(False)
        self._cancel.setText('취소')
        self._worker = TaskWorker(lambda cancel, progress: recover_avi(
            source, output, reference or None, cancel, progress), self)
        self._worker.status.connect(self._status.setText)
        self._worker.finished_task.connect(self._done)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._finished)
        self._worker.start()

    def _done(self, result):
        verified = sum(v['decode_check']['status'] == 'passed' for v in result['videos'])
        self._status.setText(f"결과: 디코딩 검증 성공 영상 {verified}개 / 영상 후보 {len(result['videos'])}개 / "
            f"JPEG {result['jpeg_stills']}개 / GPS {result['gps_records']}개\n{result['output_dir']}\n"
            '영상 후보는 재생 성공을 보장하지 않습니다. recovery.json에서 검증 결과를 확인하세요.')
        self._open.setEnabled(True)

    def _failed(self, message):
        self._status.setText(message)

    def _finished(self):
        worker = self._worker
        self._worker = None
        worker.deleteLater()
        self._start.setEnabled(True)
        self._cancel.setText('닫기')

    def reject(self):
        if self._worker is not None:
            self._worker.cancel()
            self._cancel.setEnabled(False)
            self._status.setText('취소 중... 임시 복원 결과를 정리합니다.')
            self._worker.finished.connect(lambda: self._cancel.setEnabled(True))
            return
        super().reject()

    def closeEvent(self, event):
        if self._worker is not None:
            self.reject()
            event.ignore()
        else:
            event.accept()
