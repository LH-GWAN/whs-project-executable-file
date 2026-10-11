"""User-visible October feedback and recovery dialog regressions."""
from types import SimpleNamespace
import pytest
from test_review_regressions import qapp, point
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from core.location_table import gps_slot_rows, has_frame_detail, row_texts
from engine.engine_adapter import TrackPoint


@pytest.mark.parametrize('count', [1, 3])
def test_front_choice_button_is_present_and_selectable(qapp, monkeypatch, count):
    from ui.home_view import HomeView
    def click_front(box):
        matches = [b for b in box.buttons() if b.text() == '전방만 보기']
        assert len(matches) == 1
        QTimer.singleShot(0, matches[0].click)
        return original(box)
    original = QMessageBox.exec
    monkeypatch.setattr(QMessageBox, 'exec', click_front)
    home = HomeView()
    assert home._ask_dual_track_mode('synthetic.avi', count) == 'front'
    home.close()


def test_front_notice_precedes_file_picker(qapp, monkeypatch):
    from ui.home_view import HomeView, QFileDialog
    calls = []
    home = HomeView()
    home._dual_cb.setChecked(True)
    monkeypatch.setattr(home, '_notice', lambda text: calls.append(('notice', text)))
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (calls.append(('picker','')) or ('','')))
    home._on_upload_clicked()
    assert [x[0] for x in calls] == ['notice', 'picker']
    assert '전방' in calls[0][1]
    home.close()


def test_frame_slots_and_missing_gps():
    points = [point(i//30, speed=30) for i in range(90)]
    assert has_frame_detail(points)
    assert gps_slot_rows(points) == [0, 30, 60]
    cells = row_texts(TrackPoint(start_time_sec=.2, x_g=0, y_g=0, z_g=1), [])
    assert cells[1:4] == ['-', '-', '-']
    assert cells[5] == '1.00'


def test_detail_notice_single_and_sequence(qapp, monkeypatch):
    from ui.main_window import MainWindow
    notices = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *a: notices.append(a[2]))
    dense = [point(i//30) for i in range(90)]
    host = SimpleNamespace()
    MainWindow._notify_frame_detail(host, SimpleNamespace(is_sequence=False, points=dense))
    assert len(notices) == 1 and '상세보기' in notices[0]
    MainWindow._notify_frame_detail(host, SimpleNamespace(is_sequence=True,
        segments=[SimpleNamespace(index=0, points=dense), SimpleNamespace(index=1, points=[])]))
    assert len(notices) == 2
    MainWindow._notify_frame_detail(host, SimpleNamespace(is_sequence=False, points=[point(0)]))
    assert len(notices) == 2


def test_recovery_dialog_background_lifecycle(qapp, monkeypatch, tmp_path):
    from ui import recovery_dialog
    from PySide6.QtTest import QTest
    result = {'videos': [], 'jpeg_stills': 0, 'gps_records': 1, 'output_dir': str(tmp_path)}
    monkeypatch.setattr(recovery_dialog, 'recover_avi', lambda *a: result)
    dialog = recovery_dialog.RecoveryDialog()
    dialog._source.setText(str(tmp_path/'broken.avi'))
    dialog._destination.setText(str(tmp_path))
    dialog._run()
    for _ in range(100):
        qapp.processEvents()
        if dialog._worker is None: break
        QTest.qWait(10)
    assert dialog._worker is None
    assert dialog._open.isEnabled() and dialog._start.isEnabled()
    assert 'GPS 1개' in dialog._status.text()
    dialog.close()
