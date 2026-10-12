"""앱 전체 스타일(QSS). 팔레트는 디자인 시안 IDAS_UI.html(2026-10-12, 검정 + 민트)을 따른다.

- bg #0f1215 전체 배경 / panel #161a1f 상단바·패널 / panel-2 #1d2228 버튼·입력 / line #2a3139 테두리
- text #e8ecef 기본 글자 / text-2 #9aa4ae 보조 / text-3 #5e6873 흐린 글자
- mint #3ddc97 포인트 / mint-dim #25865c / red #ff5a5f
다른 모듈에서 인라인 색을 쓸 때는 COLORS를 가져다 쓴다(지도 페이지 map.html/map_kakao.html은 같은 값을 따로 적음).
"""

COLORS = {
    "bg": "#0f1215",
    "panel": "#161a1f",
    "panel2": "#1d2228",
    "line": "#2a3139",
    "text": "#e8ecef",
    "text2": "#9aa4ae",
    "text3": "#5e6873",
    "mint": "#3ddc97",
    "mint_dim": "#25865c",
    "mint_hover": "#56e6a7",
    "red": "#ff5a5f",
    "amber": "#f5b041",      # 경고(미기록·재생 상태)
    "purple": "#b97cff",     # 추정 좌표(어두운 배경용, 지도의 #9c36b5보다 밝게)
    "link": "#6fb7ff",
}

APP_QSS = """
QWidget {
    background-color: #0f1215;
    color: #e8ecef;
    font-family: "IBM Plex Sans KR", -apple-system, "Segoe UI", "Malgun Gothic", sans-serif;
    font-size: 13px;
    selection-background-color: #25865c;
    selection-color: #e8ecef;
}
QToolTip {
    background-color: #1d2228;
    color: #e8ecef;
    border: 1px solid #2a3139;
    padding: 4px 6px;
}

QLabel { background: transparent; }
QLabel[role="title"] {
    font-size: 17px;
    font-weight: 700;
}
QLabel[role="heading"] {
    font-size: 19px;
    font-weight: 700;
}

QPushButton {
    border: 1px solid #2a3139;
    background-color: #1d2228;
    color: #e8ecef;
    padding: 7px 14px;
    border-radius: 4px;
    min-height: 18px;
}
QPushButton:hover { border-color: #25865c; }
QPushButton:pressed { background-color: #161a1f; }
QPushButton:disabled { color: #5e6873; border-color: #2a3139; }
QPushButton[role="primary"] {
    background-color: #3ddc97;
    border-color: #3ddc97;
    color: #07130d;
    font-weight: 600;
}
QPushButton[role="primary"]:hover { background-color: #56e6a7; border-color: #56e6a7; }
QPushButton[role="primary"]:disabled { background-color: #25865c; border-color: #25865c; color: #0f1215; }
QPushButton:checked { border-color: #3ddc97; color: #3ddc97; }

QFrame[role="panel"] {
    border: 1px solid #2a3139;
    border-radius: 4px;
    background-color: #161a1f;
}
QFrame[role="upload-box"] {
    border: 1px dashed #2a3139;
    border-radius: 4px;
}

#HeaderBar {
    background-color: #161a1f;
    border-bottom: 1px solid #2a3139;
}
#HeaderBar QLabel { background: transparent; }

#FileInfoBar { border-bottom: 1px solid #2a3139; }
#FileInfoBar QLabel[role="badge"] {
    border: 1.5px solid #3ddc97;
    color: #3ddc97;
    border-radius: 4px;
    padding: 1px 9px;
    font-weight: 700;
    font-size: 12px;
}
#FileInfoBar QLabel[role="info-key"] { color: #5e6873; }
QLabel[role="info-key"] { color: #5e6873; }
#FileInfoBar QFrame[role="vsep"] { color: #2a3139; }
#FileInfoBar QLabel[role="hash"] {
    font-family: "JetBrains Mono", Menlo, Consolas, "Courier New", monospace;
}
#FileInfoBar QLabel[role="integrity"] { padding-left: 2px; }
#SlackBar { border-bottom: 1px solid #2a3139; }

#HashPopup {
    background-color: #1d2228;
    border: 1px solid #2a3139;
    border-radius: 4px;
}
#HashPopup QLabel { background: transparent; }

QMenu {
    background-color: #1d2228;
    border: 1px solid #2a3139;
    padding: 4px 0;
}
QMenu::item {
    padding: 7px 28px 7px 16px;
    background-color: transparent;
    color: #e8ecef;
}
QMenu::item:selected {
    background-color: rgba(61, 220, 151, 0.16);
    color: #3ddc97;
}
QMenu::item:disabled { color: #5e6873; }
QMenu::separator {
    height: 1px;
    background-color: #2a3139;
    margin: 4px 8px;
}

QToolButton[role="settings"] {
    border: 1px solid #2a3139;
    border-radius: 4px;
    padding: 6px 12px;
    background-color: #1d2228;
}
QToolButton[role="settings"]:hover { border-color: #25865c; }
QToolButton[role="settings"]::menu-indicator { image: none; width: 0; }
QToolButton { background-color: #1d2228; border: 1px solid #2a3139; border-radius: 3px; padding: 4px; }
QToolButton:hover { border-color: #25865c; }

QTabWidget::pane { border: none; border-top: 1px solid #2a3139; }
QTabBar { background: transparent; }
QTabBar::tab {
    padding: 10px 16px;
    border: none;
    background: transparent;
    color: #9aa4ae;
    font-weight: 500;
}
QTabBar::tab:hover { color: #e8ecef; }
QTabBar::tab:selected {
    color: #e8ecef;
    font-weight: 600;
    border-bottom: 2px solid #3ddc97;
}

QRadioButton { spacing: 8px; padding: 2px 0; }
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border-radius: 9px;
    border: 1px solid #5e6873;
    background-color: #1d2228;
}
QRadioButton::indicator:hover { border-color: #3ddc97; }
QRadioButton::indicator:checked {
    border: 1px solid #3ddc97;
    background-color: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #3ddc97, stop:0.78 #3ddc97, stop:0.84 #1d2228, stop:1 #1d2228);
}
QRadioButton::indicator:disabled { border-color: #2a3139; }

QCheckBox { spacing: 8px; color: #9aa4ae; }
QCheckBox:checked { color: #e8ecef; }
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 3px;
    border: 1px solid #5e6873;
    background-color: #1d2228;
}
QCheckBox::indicator:hover { border-color: #3ddc97; }
QCheckBox::indicator:checked {
    border: 1px solid #3ddc97;
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3ddc97, stop:1 #3ddc97);
}
QCheckBox::indicator:disabled { border-color: #2a3139; background-color: #161a1f; }

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QComboBox {
    background-color: #1d2228;
    border: 1px solid #2a3139;
    border-radius: 3px;
    padding: 5px 7px;
    color: #e8ecef;
    selection-background-color: #25865c;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #3ddc97;
}
QLineEdit:disabled, QComboBox:disabled { color: #5e6873; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #1d2228;
    border: 1px solid #2a3139;
    selection-background-color: rgba(61, 220, 151, 0.16);
    selection-color: #3ddc97;
    outline: none;
}

QTableWidget, QTableView, QListWidget, QListView, QTreeView {
    background-color: #161a1f;
    alternate-background-color: #181d22;
    gridline-color: #2a3139;
    border: 1px solid #2a3139;
    border-radius: 4px;
    outline: none;
}
QTableWidget::item, QListWidget::item { padding: 3px 6px; }
QTableWidget::item:selected, QTableView::item:selected, QListWidget::item:selected, QListView::item:selected {
    background-color: rgba(61, 220, 151, 0.14);
    color: #3ddc97;
}
QListWidget::item:hover { background-color: #1d2228; }
QHeaderView { background-color: #161a1f; }
QHeaderView::section {
    background-color: #1d2228;
    color: #9aa4ae;
    border: none;
    border-bottom: 1px solid #2a3139;
    border-right: 1px solid #2a3139;
    padding: 5px 6px;
    font-weight: 600;
}
QTableCornerButton::section { background-color: #1d2228; border: none; border-bottom: 1px solid #2a3139; }

QScrollBar:vertical { background: #161a1f; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #2a3139; min-height: 24px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #3a434d; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #161a1f; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #2a3139; min-width: 24px; border-radius: 5px; }
QScrollBar::handle:horizontal:hover { background: #3a434d; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QSlider::groove:horizontal { height: 4px; background: #2a3139; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #3ddc97; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 14px; height: 14px; margin: -5px 0;
    border-radius: 7px; background: #3ddc97; border: 2px solid #0f1215;
}
QSlider::handle:horizontal:hover { background: #56e6a7; }
QSlider:disabled::sub-page:horizontal { background: #25865c; }

QProgressBar {
    background-color: #1d2228;
    border: 1px solid #2a3139;
    border-radius: 3px;
    text-align: center;
    color: #e8ecef;
}
QProgressBar::chunk { background-color: #3ddc97; border-radius: 2px; }

QSplitter::handle { background-color: #2a3139; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }

QGroupBox {
    border: 1px solid #2a3139;
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 6px;
}
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #9aa4ae; }

QDialog, QMessageBox, QProgressDialog, QFileDialog { background-color: #161a1f; }
QMessageBox QLabel { background: transparent; }
QStatusBar { background-color: #161a1f; border-top: 1px solid #2a3139; color: #9aa4ae; }
QFrame[frameShape="4"], QFrame[frameShape="5"] { color: #2a3139; }
"""
