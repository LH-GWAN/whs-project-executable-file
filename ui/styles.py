
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

QTabBar::tab {
    padding: 8px 16px;
    border: none;
    font-weight: 500;
}
QTabBar::tab:selected {
    font-weight: 700;
    border-bottom: 2px solid #111111;
}

/* 라디오/체크박스: 전역 QWidget 배경색 때문에 기본 스타일의 표시가 흐려져서
   고른 항목이 눈에 안 띈다. 고른 쪽은 파란 테두리에 가운데 점이 켜지게 직접 그린다. */
QRadioButton {
    spacing: 8px;
    padding: 2px 0;
}
QRadioButton:checked {
    color: #1c7ed6;
    font-weight: 600;
}
QRadioButton::indicator {
    width: 14px;
    height: 14px;
    border-radius: 9px;
    border: 2px solid #9a9a9a;
    background-color: #ffffff;
}
QRadioButton::indicator:hover {
    border-color: #1c7ed6;
}
QRadioButton::indicator:checked {
    border: 2px solid #1c7ed6;
    background-color: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #1c7ed6, stop:0.55 #1c7ed6, stop:0.65 #ffffff, stop:1 #ffffff);
}
QRadioButton::indicator:disabled {
    border-color: #cccccc;
}

QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border-radius: 3px;
    border: 2px solid #9a9a9a;
    background-color: #ffffff;
}
QCheckBox::indicator:hover {
    border-color: #1c7ed6;
}
QCheckBox::indicator:checked {
    border: 2px solid #1c7ed6;
    background-color: #1c7ed6;
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
