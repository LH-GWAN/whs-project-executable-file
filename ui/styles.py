
APP_QSS = """
QWidget {
    background-color: #ffffff;
    color: #111111;
    font-family: -apple-system, "Segoe UI", "Malgun Gothic", sans-serif;
    font-size: 13px;
}

QLabel[role="title"] {
    font-size: 16px;
    font-weight: 600;
}

QLabel[role="heading"] {
    font-size: 20px;
    font-weight: 700;
}

QPushButton {
    border: 1px solid #111111;
    background-color: #ffffff;
    padding: 6px 14px;
    border-radius: 2px;
}
QPushButton:hover {
    background-color: #f0f0f0;
}

QPushButton[role="primary"] {
    background-color: #111111;
    color: #ffffff;
}
QPushButton[role="primary"]:hover {
    background-color: #333333;
}

QFrame[role="panel"] {
    border: 1px solid #cccccc;
}

QFrame[role="upload-box"] {
    border: 1px dashed #999999;
}

#HeaderBar {
    border-bottom: 1px solid #111111;
}

#FileInfoBar {
    border-bottom: 1px solid #dddddd;
}
#FileInfoBar QLabel[role="badge"] {
    border: 1px solid #111111;
    border-radius: 2px;
    padding: 1px 7px;
    font-weight: 700;
}
#FileInfoBar QLabel[role="info-key"] {
    color: #666666;
}
#FileInfoBar QFrame[role="vsep"] {
    color: #cccccc;
}
#FileInfoBar QLabel[role="hash"] {
    font-family: Menlo, Consolas, "Courier New", monospace;
}

/* 해시 전체 값 팝업 */
#HashPopup {
    background-color: #ffffff;
    border: 1px solid #999999;
    border-radius: 3px;
}

/* 메뉴: 전역 QWidget 배경색 때문에 기본 하이라이트가 사라져서 직접 그린다.
   마우스를 올린 항목은 웹사이트처럼 파란 배경·흰 글씨. */
QMenu {
    background-color: #ffffff;
    border: 1px solid #cccccc;
    padding: 4px 0;
}
QMenu::item {
    padding: 7px 28px 7px 16px;
    background-color: transparent;
    color: #111111;
}
QMenu::item:selected {
    background-color: #1c7ed6;
    color: #ffffff;
}
QMenu::item:disabled {
    color: #999999;
}
QMenu::separator {
    height: 1px;
    background-color: #e0e0e0;
    margin: 4px 8px;
}

/* 설정 메뉴 버튼(화면 우측 상단). 메뉴바 대신 눈에 띄는 자리에 둔다. */
QToolButton[role="settings"] {
    border: 1px solid #cccccc;
    border-radius: 2px;
    padding: 5px 10px;
    background-color: #ffffff;
}
QToolButton[role="settings"]:hover {
    background-color: #f0f0f0;
}
QToolButton[role="settings"]::menu-indicator {
    image: none;
    width: 0;
}

QTabBar::tab {
    padding: 8px 16px;
    border: none;
    font-weight: 500;
}
QTabBar::tab:selected {
    font-weight: 700;
    border-bottom: 2px solid #111111;
}

/* 라디오(지도 사용 방식 선택): 전역 QWidget 배경색 때문에 기본 표시가 흐려져서
   고른 항목이 눈에 안 띈다. 고른 쪽은 동그라미 안을 지름의 80%쯤 되는 파란 점으로
   채운다. 체크박스(분석 항목 설정)는 손대지 않는다. */
QRadioButton {
    spacing: 8px;
    padding: 2px 0;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border-radius: 9px;
    border: 1px solid #8a8a8a;
    background-color: #ffffff;
}
QRadioButton::indicator:hover {
    border-color: #1c7ed6;
}
QRadioButton::indicator:checked {
    border: 1px solid #1c7ed6;
    background-color: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #1c7ed6, stop:0.78 #1c7ed6, stop:0.84 #ffffff, stop:1 #ffffff);
}
QRadioButton::indicator:disabled {
    border-color: #cccccc;
}

QTableWidget {
    gridline-color: #dddddd;
    border: 1px solid #cccccc;
}
QHeaderView::section {
    background-color: #fafafa;
    border: none;
    border-bottom: 1px solid #cccccc;
    padding: 4px;
    font-weight: 600;
}
"""
