"""Explicit recovery workflow, independent of evidentiary GPS/video synchronization."""
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                              QLineEdit, QMessageBox, QPushButton, QVBoxLayout)
from core.avi_recovery import recover_avi, TIMING_WARNING
from core.mp4_recovery import recover_mp4, TIMING_WARNING as MP4_WARNING
from ui.workers import TaskWorker


class RecoveryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('손상 AVI/MP4 복원')
        self.resize(720, 410)
        self._worker = None
        self._output = ''
        layout = QVBoxLayout(self)
        self._notice = QLabel()
        self._notice.setWordWrap(True)
        layout.addWidget(self._notice)
        form = QFormLayout()
        self._format = QComboBox()
        self._format.addItems(['AVI', 'MP4 / fMP4'])
        form.addRow('복원 형식', self._format)
        self._source = self._path_row(form, '손상 영상', False)
        self._reference = self._path_row(form, '같은 설정의 정상 참조 영상 (선택)', False)
        self._destination = self._path_row(form, '결과 저장 위치', True)
        layout.addLayout(form)
        self._warning = QLabel()
        self._warning.setWordWrap(True)
        layout.addWidget(self._warning)
        self._format.currentIndexChanged.connect(self._format_changed)
        self._format_changed()
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

    def _format_changed(self, *_):
        mp4 = self._format.currentIndex() == 1
        self._notice.setText(
            ('MP4/fMP4의 남아 있는 H.264/HEVC 영상과 GPS·G센서를 회수합니다.\n'
             'moov가 사라졌으면 동일 기기·코덱·해상도·FPS의 정상 MP4를 지정하세요.\n'
             '전체 재생 검증에는 PATH에 FFmpeg가 필요합니다. 없으면 미검증 후보로 저장합니다.') if mp4 else
            ('AVI의 남아 있는 MJPEG/H.264 영상과 NMEA GPS를 회수합니다.\n'
             '헤더가 소실됐으면 동일 기기·해상도·코덱·FPS·채널 순서의 정상 AVI를 지정하세요.\n'
             '설정이 없으면 가능한 JPEG 정지영상과 GPS만 회수합니다.'))
        self._warning.setText(MP4_WARNING if mp4 else TIMING_WARNING)

    def _path_row(self, form, title, directory):
        edit = QLineEdit()
        button = QPushButton('찾기')
        def choose():
            value = (QFileDialog.getExistingDirectory(self, title) if directory else
                     QFileDialog.getOpenFileName(self, title, '',
                         '영상 (*.avi *.AVI *.mp4 *.MP4 *.mov *.MOV *.m4v *.M4V);;모든 파일 (*)')[0])
            if value:
                edit.setText(value)
                if not directory and title == '손상 영상':
                    self._format.setCurrentIndex(0 if Path(value).suffix.lower() == '.avi' else 1)
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
            QMessageBox.warning(self, '복원', '손상 영상과 결과 저장 위치를 선택하세요.')
            return
        mp4 = self._format.currentIndex() == 1
        recover = recover_mp4 if mp4 else recover_avi
        self._output = str(Path(destination)/(('mp4' if mp4 else 'avi')+'_recovery_'+uuid4().hex[:12]))
        output = self._output
        self._start.setEnabled(False)
        self._open.setEnabled(False)
        self._cancel.setText('취소')
        self._format.setEnabled(False)
        self._worker = TaskWorker(lambda cancel, progress: recover(
            source, output, reference or None, cancel, progress), self)
        self._worker.status.connect(self._status.setText)
        self._worker.finished_task.connect(self._done)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._finished)
        self._worker.start()

    def _done(self, result):
        verified = sum(v['decode_check']['status'] == 'passed' for v in result['videos'])
        self._status.setText(f"결과: 디코딩 검증 성공 영상 {verified}개 / 영상 후보 {len(result['videos'])}개 / "
            f"JPEG {result['jpeg_stills']}개 / GPS {result['gps_records']}개 / "
            f"G센서 {result.get('gsensor_records', 0)}개\n{result['output_dir']}\n"
            '영상 후보는 재생 성공을 보장하지 않습니다. recovery.json에서 검증 결과를 확인하세요.'
            + ('\n'+result['video_withheld_reason'] if result.get('video_withheld_reason') else ''))
        self._open.setEnabled(True)

    def _failed(self, message):
        self._status.setText(message)

    def _finished(self):
        worker = self._worker
        self._worker = None
        worker.deleteLater()
        self._start.setEnabled(True)
        self._format.setEnabled(True)
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
