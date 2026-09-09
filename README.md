# 스크립트 모음
https://github.com/LH-GWAN/whs-project
여기에 모아 놨다

# GPS Tracer

블랙박스 영상(AVI/MP4)에서 GPS·속도·G센서 메타데이터를 추출해 지도와 그래프로
시각화하고, 사건 리포트(PDF)를 생성하는 데스크톱 포렌식 도구. Windows exe로 배포한다.

분석 엔진은 별도 저장소([LH-GWAN/whs-project](https://github.com/LH-GWAN/whs-project))에서
개발된 것을 `engine/vendor/`에 그대로 가져와 쓴다. **이 저장소의 코드는 그 엔진을
호출하고 결과를 시각화하는 레이어**다.

---

## 목차

- [빠른 시작](#빠른-시작)
- [전체 구조](#전체-구조)
- [모듈별 역할](#모듈별-역할)
- [데이터 흐름](#데이터-흐름)
- [⚠ 주의할 점](#-주의할-점)
- [검증 현황](#검증-현황)

---

## 빠른 시작

```bash
python -m venv .venv          # macOS에서는 ~/Desktop 밖에 만들 것 (아래 주의사항 참고)
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

exe 빌드는 [`BUILD.md`](BUILD.md), 배경지도 준비는 [`assets/README.md`](assets/README.md) 참고.

---


## 독립 실행 (엔진 저장소 불필요)

이 폴더 하나만 있으면 동작한다. 옆에 있는 `../whs-project/`(엔진 원본 저장소)는
**실행에 필요 없다** — 엔진 스크립트 3개가 `engine/vendor/`에 복사돼 있고, 앱 코드
어디에도 그 폴더를 참조하는 경로가 없다.

검증(엔진 저장소가 없는 곳으로 앱만 복사해 확인):
- 소스 실행: 실샘플 추출 정상 (600지점 / fix 60 / `tfdt_trun`)
- exe 실행: **소스 트리를 전부 지운 뒤** `GPSTracer` + `_internal/`만으로
  MP4 fragmented 600지점, AVI FineVu 1035좌표 추출 + GUI 기동 정상

`../whs-project/`가 필요한 경우는 **엔진을 고칠 때뿐**이다(주의사항 2번 참고).
그때도 원본에서 고친 뒤 `engine/vendor/`로 복사하면 실행 폴더는 계속 독립적이다.

---

## 전체 구조

```
app.py                    진입점. --run-engine 분기를 Qt 초기화보다 먼저 처리
engine_entry.py           엔진을 서브프로세스에서 실행하기 위한 경계

core/                     플랫폼/UI 무관 핵심 로직
├── paths.py              개발 실행 vs 패키징 실행 경로 차이 흡수
├── format_sniffer.py     업로드 파일이 처리 가능한지 사전 확인
├── duration.py           영상 재생시간 계산
├── acceleration.py       급가속 의심 구간 판정
├── hashing.py            SHA-256 무결성 해시
├── pipeline.py           전체 분석 파이프라인 (위 모듈들을 순서대로 엮음)
├── appconfig.py          지도 사용 방식(오프라인/온라인) 설정 + 온라인 API 키 로딩
├── kakao_api.py          카카오맵 HTTP 호출 (SDK 진단, 좌표→주소). 표준 라이브러리만 사용
├── geocode.py            좌표→주소 변환 (캐시, 한도 초과 시 세션 동안 차단)
├── case_deletion.py      사건 이력 삭제 (폴더 안전장치, 폴더 유지 옵션)
└── basemap.py            오프라인 배경지도 파일 탐색 / zip 자동 해제

engine/                   분석 엔진 오케스트레이션
├── registry.py           엔진 진입 모듈 이름 (엔진 교체 시 여기만 수정)
├── engine_adapter.py     서브프로세스 실행 + CSV → 데이터 모델 변환
└── vendor/               ★ 원본 엔진 (수정 금지)

storage/history_store.py  사건 이력 (SQLite)
report/report_builder.py  Extraction Report PDF 생성

ui/                       PySide6 화면
├── main_window.py        Home ↔ 분석화면 전환, 워커/리포트 연결
├── home_view.py          업로드 + 사건 이력 목록
├── case_info_dialog.py   사건 정보 입력 모달
├── analysis_view.py      공통 헤더 + 3개 탭 구성
├── tracker_tab.py        영상 재생 + 지도 (재생 위치 동기화)
├── speed_tab.py          속도 그래프 + 통계
├── speed_chart_widget.py 속도 그래프 렌더링 (QPainter)
├── location_tab.py       지도 + 좌표 테이블
├── map_view.py           지도 위젯 (QWebEngineView 래퍼)
├── map_server.py         지도 리소스 로컬 서버 (고정 포트, map-config / 진단 엔드포인트)
├── map_mode_dialog.py    오프라인/온라인 선택 창 (첫 실행, 설정 > 지도 사용 방식)
├── basemap_notice.py     오프라인 지도 파일이 없을 때 안내 창
├── online_keys_notice.py 온라인 키가 없을 때 발급·등록·저장 안내 창 (키 파일 틀 생성)
├── address_resolver.py   주소 조회 워커 (최신 요청 하나만, 0.4초 간격)
├── workers.py            분석을 백그라운드 스레드에서 실행
├── styles.py             QSS
├── web/map.html          오프라인 지도 페이지 (MapLibre + PMTiles)
├── web/map_kakao.html    온라인 지도 페이지 (카카오맵 JavaScript SDK). JS 함수 이름은 map.html과 동일
└── vendor/               MapLibre GL JS, PMTiles (로컬 번들, CDN 금지)

assets/korea.pmtiles      오프라인 배경지도 (371MB, git 제외)
assets/online_keys.json   카카오 API 키 (git 제외, 빌드 시 번들). 없으면 안내 창의 [키 파일 만들기]
gpstracer.spec            PyInstaller 빌드 정의
```

---

## 모듈별 역할

### `app.py` — 진입점

`--run-engine` 플래그를 **QApplication을 만들기 전에** 확인하고 `engine_entry`로 위임한다.
패키징된 exe에는 별도 `python.exe`가 없어서, 엔진을 서브프로세스로 돌릴 때 앱이 자기
자신을 재호출하기 때문이다. 이 순서가 바뀌면 자식 프로세스가 GUI를 하나 더 띄운다.

### `core/paths.py` — 경로 해석

PyInstaller는 번들 리소스를 `sys._MEIPASS`에 풀어놓으므로 `__file__` 기준 경로가 전부
깨진다. frozen 여부에 따른 분기를 이 파일 하나로 모아, 나머지 모듈은 신경 쓰지 않게 한다.

### `core/format_sniffer.py` — 사전 확인

엔진을 돌리기 전에 "이 파일을 처리할 수 있는가"를 즉시 알려준다. **판별 로직을 다시
구현하지 않고 엔진의 `detect_container()`를 그대로 호출**한다 — 앱은 "지원함"이라 했는데
엔진은 건너뛰는 불일치를 원천 차단하기 위함. 세부 경로(AVI 내 스트림 종류, MP4 내
fragmented/sampletable/udta-mamt)는 엔진이 알아서 고르므로 앱은 관여하지 않는다.

### `engine/registry.py` — 엔진 진입점 정의

엔진이 `integration_blackbox.py` 하나로 통합돼 있어 항목이 하나뿐이다. 엔진이 또 바뀌면
**이 파일만 수정**하면 되도록 격리해 뒀다.

### `engine/engine_adapter.py` — 오케스트레이션

엔진을 서브프로세스로 실행하고, 결과 CSV를 `TrackPoint` 리스트로 표준화한다.
앱의 모든 화면과 리포트가 이 데이터 모델 하나만 본다.

`TrackPoint`의 세 가지 상태 구분이 이 도구의 핵심 개념이다:

| 상태 | 판별 | 의미 |
|---|---|---|
| `has_fix` | 좌표 있음 | GPS 수신 정상 |
| `is_dropout` | GPS 시각은 있는데 좌표 없음 | **수신 끊김** (NMEA status=V) |
| (둘 다 아님) | GPS 정보 자체가 없음 | 이 시점엔 GPS 미기록 (G센서 전용 행) |

### `core/acceleration.py` — 급가속 판정

이 결과 하나를 속도 그래프의 강조 구간과 좌표 테이블의 빨간 행이 함께 사용한다
(두 화면이 어긋나지 않도록 단일 소스).

### `core/pipeline.py` — 분석 파이프라인

해시 → 형식 확인 → 사건 폴더 생성 → 원본 복사 → 엔진 추출 → duration/급가속 계산 →
이력 기록. `reopen_case()`는 엔진 재실행 없이 저장된 CSV만 다시 읽는다(빠르고, 원본
영상이 이동식 매체로 사라져도 결과 조회 가능).

### `storage/history_store.py` — 사건 이력

SQLite는 **검색/색인용**이고, 실제 증거(원본 사본·엔진 출력·리포트)는 폴더에 파일로 둔다.
DB가 손상돼도 증거는 살아남고, 다른 포렌식 도구가 이 앱 없이 폴더만으로 접근할 수 있다.

삭제는 Home의 [선택 삭제]/[전체 삭제](Delete 키, 우클릭 메뉴)로 하며 `core/case_deletion.py`가
맡는다. 확인 창에서 "사건 폴더도 함께 삭제"(기본 켜짐)를 끄면 목록에서만 지운다. 안전장치:
cases 루트 **바로 아래**의 `<사건번호>_<id>` 폴더만 지우므로 DB가 엉뚱한 경로를 가리켜도
다른 곳은 건드리지 않는다. 폴더는 **먼저 `<폴더>.deleting`으로 이름을 바꾼 뒤** 지운다 —
Windows는 안에 열린 파일이 있으면 이름 변경부터 실패하므로 "반쯤 지워진 폴더 + 남은
레코드" 상태가 생기지 않고, 실패하면 레코드를 남겨 다시 시도할 수 있다. 원본에서 복사된
읽기 전용 속성은 풀고 다시 지운다. 그래도 남은 `.deleting` 폴더는 다음 삭제 때 다시
시도한다. 사용자가 다른 곳에 저장한 리포트 PDF는 지우지 않는다. 삭제 전에 영상 재생기의
파일 잠금을 항상 푼다(재생기는 마지막으로 Tracker를 켠 사건의 영상을 계속 잡고 있을 수
있다). 분석이 진행 중일 때는 진행 창이 모달이라 삭제 조작이 막히고, 혹시 요청이 들어와도
거절한다(진행 중인 사건의 레코드를 지우면 워커가 외래키 오류로 죽는다).

```
%LOCALAPPDATA%/GPSTracer/
├── history.db
└── cases/<사건번호>_<id>/
    ├── source/           원본 영상 사본 (해시로 무결성 고정)
    ├── engine_output/    엔진 산출물 원본 그대로
    ├── case.json         DB 유실 대비 사람이 읽을 수 있는 사본
    └── report.pdf
```

### `ui/map_server.py` — 지도 리소스 서버

배경지도(PMTiles)는 한 파일에서 필요한 타일만 바이트 범위로 읽는 포맷이라 HTTP
Range(206) 의미론이 필요하다. Qt 커스텀 URL 스킴으로는 206을 제대로 돌려줄 수 없어
표준 HTTP를 쓴다. **`127.0.0.1`에만 바인딩**하고 임의 포트를 쓰며, 앱 자신의 정적
파일과 지도 파일만 서빙한다. 사건 GPS 좌표는 이 서버를 타지 않는다.

### 리포트 PDF 여백

`report/report_builder.py`의 `ReportExporter`는 `printToPdf`에 A4·좌우 20mm·상하 18mm의
`QPageLayout`을 넘긴다. **CSS `@page` 여백은 QtWebEngine이 인자로 받은 레이아웃에 눌려
반영되지 않았고**, 인자를 안 주면 기본 여백이 0이라 내용이 종이 왼쪽 끝에 붙어 나왔다
(실측: 왼쪽 여백 20.1mm로 확인).

### `ui/map_view.py` — 지도 위젯

Python → JS는 `runJavaScript()` 단방향 주입만 쓴다(지도가 되물어볼 일이 없어 채널 유지
불필요, 준비 경쟁조건도 사라짐). 렌더 프로세스가 죽으면 최대 3회까지 자동 복구하고,
복구 후 보던 궤적을 다시 그린다.

### 지도 사용 방식 — 오프라인 / 온라인(카카오맵)

첫 실행 때 `ui/map_mode_dialog.py`가 오프라인·온라인 중 하나를 고르게 하고, 값은
`%LOCALAPPDATA%/GPSTracer/settings.json`(`map_mode`)에 남는다. 나중에 화면 우측 상단
**⚙ 설정 > 지도 사용 방식** 메뉴에서 바꿀 수 있다(메뉴바가 아니라 홈 화면 제목 줄의 버튼이다 —
메뉴바는 눈에 안 띈다는 피드백으로 옮겼다). 이 값과 안내 창의 "다시 표시하지 않음"(레지스트리
`HKCU\Software\GPSTracer`, QSettings)은 **사용자 데이터 영역**이라 `clean_windows.bat`이나
재빌드로는 지워지지 않는다 — 그래서 두 번째 실행부터는 묻지 않는다. 처음처럼 다시 묻게
하려면 **설정 > 지도 설정 초기화** 메뉴를 쓰거나, clean 스크립트 끝의 "Reset app settings"에
Y를 답한다(사건 이력·증거 폴더는 어느 쪽도 건드리지 않는다). 수사 자료를 다루는 도구라 "외부로 나가는가"는
사용자가 알고 고르는 것이지 프로그램이 조용히 정할 일이 아니다.

| | 오프라인 (기본) | 온라인 |
|---|---|---|
| 페이지 | `map.html` (MapLibre + `assets/*.pmtiles`) | `map_kakao.html` (카카오맵 JavaScript SDK) |
| 외부 통신 | 없음 | 지도: 화면 범위가 카카오로 전송. 주소: 좌표가 카카오로 전송 |
| 필요한 것 | 지도 파일 | `assets/online_keys.json` (JavaScript 키 + REST API 키) + 인터넷 |
| 주소 표시 | 안 됨 | Tracker 하단·Location 선택 행에 지번 주소 |

어느 페이지를 띄울지는 `MapServer.map_url()` 하나가 정한다 — 온라인인데 키가 없으면
오프라인 페이지로 간다. 두 페이지는 `renderTrack` / `setPlaybackTime` / `setFollow`
함수 이름이 같아서 `ui/map_view.py`는 어느 쪽인지 모른다.

키가 없는 채로 온라인을 고르면 `ui/online_keys_notice.py`가 발급·도메인 등록·파일 저장
절차를 안내한다(배경지도 안내와 같은 방식, [설정] 메뉴에서 다시 볼 수 있음). 발급 절차
전체는 [`assets/README.md`](assets/README.md)에 있다.

키는 `core/appconfig.online_keys()`가 읽는다. 우선순위는 `settings.json`의
`kakao_js_key` / `kakao_rest_key` > `assets/` 안의 키 파일. 재빌드 없이 키를
바꿀 수 있게 설정 쪽을 위에 뒀다. 키 파일은 이름을 고정하지 않고 폴더 안의 `.json`/`.txt`
후보를 모두 읽어 **영문 소문자·숫자 32자 형식**인 값만 키로 인정한다(예시 문구·BOM·깨진
따옴표 대응). 파일 목록/수정 시각이 바뀌면 2초 안에 다시 읽으므로 재시작이 필요 없고,
안내 창의 [다시 확인]은 즉시 다시 읽어 지도를 온라인으로 바꿔 띄운다. 어떤 파일을 왜 못
썼는지는 `key_status_report()`가 안내 창과 `--diagnose`에 파일별로 적어 준다.
exe는 `_internal/assets/`를 보므로 빌드 뒤에 넣어도 된다(BUILD.md). 세 종류 키 중 **JavaScript 키는 지도, REST API 키는
주소 변환**에 쓰고 네이티브 앱 키는 쓰지 않는다(Android/iOS 전용).

온라인 페이지의 준비/실패는 페이지 안의 `window.__onlineMapState`(`loading` → `ready`
또는 `error:<원인>`)로 알리고, `MapView`가 잠시 폴링해 `online_map_failed` 시그널로
올린다. 원인 종류(`quota`/`domain`/`disabled`/`key`/`network`)는 `core/kakao_api.py`의
`KIND_*`이고, `MainWindow`가 원인별로 한 번씩 안내 창을 띄운다(한도 초과면 "오프라인
지도로 전환" 버튼을 기본으로).

---

## 데이터 흐름

```
영상 업로드
   ↓
SHA-256 계산(HashWorker) → 같은 해시의 이력이 있으면 "이미 분석한 파일입니다" 확인
   (예: 새 사건으로 다시 분석, 기존 이력은 그대로 / 아니요: 홈으로)
   ↓
core.format_sniffer.sniff()          엔진의 detect_container() 재사용
   ↓
core.pipeline.run_analysis_pipeline()   (ui.workers의 백그라운드 스레드에서 실행)
   ├─ core.hashing                   SHA-256
   ├─ 사건 폴더 생성 + 원본 복사
   ├─ engine.engine_adapter.run_full_extraction()
   │     └─ 서브프로세스: app.exe --run-engine blackbox -o <출력> <입력>
   │           └─ engine_entry → integration_blackbox.main()
   │                 ├─ AVI → integration_avi.py
   │                 └─ MP4 → integration_mp4.py
   │        결과: timeline.csv / coordinates.csv / ... 를 TrackPoint로 변환
   ├─ core.duration                  재생시간
   └─ core.acceleration              급가속 구간
   ↓
ui.analysis_view.load_result()
   ├─ Tracker  : 영상(QMediaPlayer) + 지도, 재생 위치 동기화
   ├─ Speed    : 속도 그래프 + 급가속 강조
   └─ Location : 지도 + 좌표 테이블
   ↓
Report 버튼 → report.report_builder → PDF
```

---

## ⚠ 주의할 점

코드를 고치기 전에 반드시 읽을 것. 대부분 실제로 문제가 발생해서 알게 된 것들이다.

### 1. 엔진은 서브프로세스로만 실행할 것

`integration_*.py`의 `main()`과 argparse는 실패 시 `sys.exit()`을 호출한다.
`SystemExit`은 `Exception`이 아니라 **`except Exception`으로 잡히지 않아** GUI 프로세스에서
직접 import해서 부르면 앱 전체가 죽는다. 서브프로세스로 격리하면 이 위험이 사라지고,
사건 이력에 남길 실행 로그(stdout/stderr/종료코드)도 자연스럽게 확보된다.

단, `detect_container()`처럼 `sys.exit()`을 부르지 않는 순수 함수는 직접 import해도 안전하다.

### 2. `engine/vendor/`는 직접 수정하지 말 것

원본 저장소(`../whs-project/`)에서 그대로 가져온 파일이다. 여기만 고치면 원본과
갈라져 다음 갱신 때 충돌한다. **엔진 버그는 원본 저장소에서 고치고 vendor로
복사**해서 두 곳이 항상 바이트 동일하게 유지한다(`diff -q`로 확인). 갱신 절차와 확인 항목은 [`engine/vendor/README_VENDOR.md`](engine/vendor/README_VENDOR.md) 참고.

특히 **엔진 갱신 시 `gpstracer.spec`의 `hiddenimports`를 반드시 다시 확인**해야 한다.
vendor는 데이터 파일로 번들되어 PyInstaller의 정적 분석 대상이 아니라서, 엔진이 새로
쓰기 시작한 표준 라이브러리가 빠지면 **얼린 뒤에야** `ModuleNotFoundError`로 터진다
(실제로 `shlex`에서 겪음).

### 3. 시간축은 엔진이 계산한 `start_time_sec`를 쓸 것

GPS 문장의 UTC로 시간축을 만들면 수신이 튀는 기기에서 축까지 같이 튄다(엔진 README
실측: QXD8000/Mercedes에서 GPS UTC 간격이 0/1/2초로 불규칙한데 구조 기반 축은 완전 균일).
영상 재생과 동기화하는 게 목적이므로 컨테이너 구조에서 계산한 값이 맞다.

경로별 시간축 근거(`time_source`): `tfdt_trun`(fragmented) / `stts`(sample table) /
`gps_utc_elapsed`(udta-mamt) / `avi_video_duration`(AVI).

### 4. 가속도는 "서로 다른 GPS 측정값" 사이에서만 계산할 것

많은 기기가 GPS 갱신 주기보다 훨씬 빠르게 레코드를 쓴다. 그 사이 행들은 **직전 측정값을
그대로 반복한 것**이라 서로 다른 시점으로 취급하면 안 된다.

> **실측 사례**: VUGERA는 초당 31행을 쓰는데 GPS는 1초에 한 번만 갱신된다. 초 경계의
> 속도 변화 3.18 km/h를 행 간격 0.033초로 나누면 **26.8 m/s²** 가 나온다. 실제로는
> 0.88 m/s²인 완만한 가속인데, 38초 영상에서 급가속이 30건 검출됐다.
> FineVu도 연속 행의 94.1%가 완전 중복이었다.

`_distinct_fix_indices()`가 중복을 걷어낸다. UTC가 있으면 UTC로 묶고(정차 중이라 좌표·속도가
같아도 측정은 매초 새로 이뤄지므로 값으로만 묶으면 안 됨), FineVu처럼 UTC가 없는 이진
포맷은 값으로 묶는다.

### 5. "좌표 없음"을 전부 수신 끊김으로 보지 말 것

GPS 1Hz / G센서 10Hz인 기기에서는 좌표 없는 행 대부분이 수신 장애가 아니라 그냥 GPS가
안 실린 샘플이다. 뭉뚱그리면 **정상 주행이 수신 장애로 오독**된다.

> **실측 사례**: INAVI QXD8000은 600행 중 540행이 G센서 전용인데, 이걸 끊김으로 세니
> "GPS 끊김 540개 지점"이라 표시되고 지도의 궤적 전체가 점선(경로 불확실)으로 그려졌다.

판별 기준은 `TrackPoint.is_dropout` — 엔진이 GPS 문장을 파싱했으면 좌표가 비어도
날짜/UTC는 채워주므로 그걸로 구분한다.

### 6. MapLibre `interpolate`는 표현식 최상위에만

줌 기반 `interpolate`를 `match` 안에 중첩하면 MapLibre가 **레이어를 통째로 거부**한다
(콘솔에 `Only one zoom-based "step" or "interpolate" subexpression may be used`).
도로가 통째로 안 그려지는데 예외는 안 나므로 알아채기 어렵다. `interpolate`를 바깥에 두고
각 줌 구간 값 안에서 `match`로 종류별 굵기를 고를 것.

### 7. 지도 초기화는 `load` 이벤트만 믿지 말 것

MapLibre의 `load`는 **렌더 프레임이 최소 한 번 돌아야** 발생한다. GPU 드라이버가
불안정하거나 원격데스크톱/가상머신이면 렌더 루프가 안 돌아 이벤트가 영영 오지 않고
화면이 "지도 준비 중…"에서 멈춘다. `map.html`은 이벤트 + 폴링을 함께 쓰고, 8초 안에
안 되면 사용자에게 사유를 표시한다.

또 Chromium은 **화면에 보이지 않는 페이지의 타이머를 억제**한다. 탭 안의 지도는 그 탭이
열릴 때 `ensure_map_loaded()`로 로드한다(두 탭의 지도를 동시에 띄우면 WebGL 컨텍스트를
두 벌 쓰는 낭비이기도 하다).

### 8. 배경지도 레이어 이름은 Protomaps 스키마

`earth` / `water` / `landuse` / `roads` / `buildings` / `boundaries` 를 쓴다.
OpenMapTiles의 `transportation` / `building` 이 **아니다**.

### 9. macOS 개발 시 venv 위치

`~/Desktop` 하위에 만든 venv는 Qt 플랫폼 플러그인 로드에 실패한다(macOS 폴더 접근
제한으로 추정). 플러그인 파일은 존재하고 직접 dlopen도 되는데 Qt가 못 찾는다.
`~/dev/` 등 Desktop 밖에 만들 것. Windows 배포에는 영향 없는 개발 환경 한정 이슈다.

### 10. 리포트는 전체 좌표를 넣지 않는다

수천~수만 지점이 나오므로 급가속 구간을 우선 포함하고 나머지는 앞부분 일부만 싣는다
(최대 200행). 원본 전체는 `engine_output/`의 CSV에 그대로 보존돼 있다.


### 11. 엔진 실패는 종료 코드로 알 수 없다

`integration_blackbox.main()`은 하위 스크립트의 예외를 잡아 요약만 찍고 `None`을
반환한다. 모든 파일이 SKIP돼도 프로세스 종료 코드는 0이다. 그래서
`engine_adapter._classify_outcome()`은 종료 코드가 아니라 **산출물**로 판정한다:
좌표가 있으면 정상, 좌표는 없지만 다른 산출물이 있으면 이 영상에 GPS가 없는 것,
산출물이 아예 없으면 엔진 실패. `ExtractionResult.status`로 UI에 노출된다.

### 12. 슬랙 카빙 결과는 본 궤적과 절대 합치지 않는다

`--slack`으로 나오는 `slack_coordinates.csv`는 **과거 녹화분**이라 현재 영상의
재생 시각이 없다(sample table 밖 영역이라 절대 offset만 남는다). 지도·타임라인·
재생 동기화에 쓰면 안 되므로 `ExtractionResult.slack_points`에 따로 담는다.

### 13. G센서는 개별 축이 아니라 합력으로 본다

엔진의 자가 보정(`*_g_cal`)은 크기만 맞추고 **장착 각도는 보정하지 않는다**.
어느 축이 진행 방향인지 모르므로 개별 축값은 기기 간 비교 기준이 못 된다.
`TrackPoint.g_magnitude`는 방향과 무관한 합력(√(x²+y²+z²))을 쓴다 —
정상 주행이면 중력 때문에 1g 근처로 나온다(실측 0.69~1.41g).

### 14. PyInstaller windowed 빌드에서는 sys.stdout이 None이다

vendor 엔진 세 파일 모두 `sys.stdout.encoding`을 None 검사 없이 읽는다.
`console=False` 빌드에서 GUI 프로세스가 `format_sniffer` → `integration_blackbox`를
import하는 순간 `AttributeError`로 죽고, 콘솔이 없어 트레이스백도 안 보인다.
vendor는 수정하지 않는 원칙이라 `app.py`의 `_ensure_std_streams()`가 import 전에
더미 스트림을 채워 방어한다. **이 함수를 지우면 exe가 안 켜진다.**

### 15. 카카오 JavaScript 키는 페이지 출처(포트 포함)를 등록해야 응답한다

카카오 SDK는 요청의 `Referer` 출처가 카카오 디벨로퍼스 **Web 플랫폼 사이트 도메인**과
정확히 일치해야 응답한다. 실측: `401 domain mismatched! caller=http://127.0.0.1:48213`.
지도 페이지는 로컬 서버에서 열리므로 포트가 매번 바뀌면 등록이 불가능하다. 그래서
`ui/map_server.py`는 **48213 → 48214 → 48215 순으로 고정 포트**를 시도한다(모두 막혀
있으면 임의 포트, 이때 온라인 지도는 "도메인 미등록" 안내가 뜬다).

카카오 디벨로퍼스에서 할 일(계정 소유자만 가능):
1. 내 애플리케이션 > **카카오맵 > 사용 설정 ON** (꺼져 있으면 `403 App disabled
   OPEN_MAP_AND_LOCAL service`로 지도·주소 둘 다 거절된다)
2. **[앱] > [플랫폼 키] > JavaScript 키 > [JavaScript SDK 도메인]**에 `http://127.0.0.1:48213`,
   `http://127.0.0.1:48214`, `http://127.0.0.1:48215` 등록

두 번째 항목에서 실제로 겪은 함정 두 가지:
- **[제품 링크 관리 > 웹 도메인]은 다른 설정이다.** 카카오톡 공유 링크용이라 여기 넣으면
  계속 `domain mismatched`가 난다(카카오 데브톡에 같은 사례 다수).
- **JS SDK 도메인은 JavaScript 키별로 붙는다.** 앱에 JavaScript 키가 여러 개면 도메인이
  등록된 키를 `online_keys.json`에 넣어야 한다. 현재 앱(IDAS, 1572145)은 대표 키가 아닌
  두 번째 키(`9cfb…1967`)에 등록돼 있어 그 키를 쓴다. 대표 키로 바꾸면 지도가 안 뜬다.
  어느 키에 등록됐는지는 콘솔 플랫폼 키 화면에서 "JS SDK 도메인" 배지로 구분된다.

`GPSTracer.exe --diagnose`가 등록할 주소와 키 유무(마스킹)를 출력한다. 페이지가 SDK
로드에 실패하면 브라우저는 이유를 숨기므로, 로컬 서버의 `/online-map-diagnose`가 같은
조건으로 다시 받아 보고 원인을 돌려준다(성공 경로에서는 부르지 않는다 — SDK 로드
횟수가 한도에 잡힌다).

### 16. API 키 파일은 git에 넣지 말 것

이 저장소는 공개돼 있다. `assets/online_keys.json`은 `.gitignore`에 있고, 빌드하는
PC의 `assets/`에 있을 때만 `gpstracer.spec`이 번들에 넣는다. 배포된 exe 안에는 키가
그대로 들어가므로(데스크톱 앱의 한계) 유출되면 카카오 디벨로퍼스에서 재발급하고,
재빌드 없이 바꾸려면 `settings.json`에 새 키를 넣으면 된다(주의사항 15의 우선순위).

팀에 나눠줄 때는 배경지도와 같은 방식이다 — 키 파일을 외부 공유 링크(구글 드라이브,
뷰어 권한이면 충분)에 올려 두고 `core/appconfig.py`의 `ONLINE_KEYS_DOWNLOAD_URL`에 적으면
안내 창의 [키 파일 내려받기] 버튼이 그 링크와 넣을 폴더를 연다. 링크를 아는 사람만 받을 수
있고 저장소에는 키가 남지 않는다. 키를 소스에 적거나 base64 같은 인코딩으로 감춰 올리는 건
누구나 풀 수 있으므로 하지 말 것.

### 17. 한도 초과(429)는 세션 동안 차단하고 한 번만 안내한다

무료 한도는 앱 단위 일일 집계(지도 SDK 30만 건, 좌표→주소 10만 건, 2026-09 기준).
초과하면 HTTP 429가 온다. `core/geocode.py`는 429나 키 오류를 한 번 보면 세션 동안
`_block`에 담아 더 묻지 않고, `AddressResolver`가 `failed` 시그널을 한 번만 낸다.
지도 사용 방식을 다시 고르면(`reset_block`) 재시도한다.

카카오는 **짧은 시간에 몰린 요청도 429**로 거절하는데 일일 한도 초과와 구분할 수
없다. 그래서 주소 워커는 요청 간격을 0.4초 이상 띄우고 가장 최근 지점 하나만 조회한다.
같은 좌표는 약 11m(소수 4자리) 단위로 캐시해 재생 중 같은 지점을 반복 조회하지 않는다.

### 18. 주소 조회는 GUI 스레드에서 직접 부르지 말 것

`geocode.describe_location()`은 네트워크를 탄다. 화면은 `geocode.cached_address()`로
즉시 확인하고, 없으면 `AddressResolver.request()`에 맡긴 뒤 `resolved` 시그널로 받는다.
재생 중 초당 수십 번 불리는 `_update_info()`에서 동기 호출하면 창이 멈춘다.

### 19. 온라인 페이지는 외부 스크립트를 받는다

`ui/vendor/`의 "CDN 금지" 원칙은 오프라인 페이지 얘기다. `map_kakao.html`은 설계상
`dapi.kakao.com`에서 SDK를 받고 타일도 카카오에서 온다. 오프라인 모드에서는 이 페이지가
아예 열리지 않으므로(`map_url()`) 외부 통신 0이라는 보장은 그대로다.

---

## 검증 현황

### 실제 샘플 (8개 기종 13개 파일) — 전부 통과

| 기종 | 컨테이너 | 지점 | GPS 수신 | 시간축 |
|---|---|---|---|---|
| VUGERA MB-900SB (4파일) | AVI | 1150~2246 | 전부 | `avi_video_duration` |
| INAVI Z300 | MP4 | 200 | 20 | `stts` |
| INAVI QXD8000 | MP4 | 600 | 60 | `tfdt_trun` |
| Land Rover Dashcam (3파일) | MP4 | 60 | 60 / 36 | `gps_utc_elapsed` |
| Mercedes-Benz DriveView | MP4 | 60 | 60 | `tfdt_trun` |
| INAVI FXD900 | AVI | 39 | 39 | `avi_video_duration` |
| FineVu X3000 / X700 | AVI | 1035 / 1060 | 전부 | `avi_video_duration` |

Land Rover `20250901_215728D`의 GPS 수신 36개(끊김 24개)는 엔진 README에 기록된
"60 (fix 없음 24 포함)"과 일치한다.

### 그 외 확인한 것

- 컨테이너 판별이 엔진과 100% 일치 (AVI/MP4/ftyp 없는 변종/RIFF-WAVE/비디오 아님)
- 경계 조건: 지원하지 않는 파일, GPS 없는 영상, dt=0, 시간 역행, 큰 간격, 시간축 없음
- 무결성: 원본 SHA-256 == 사본 == 기록값, `case.json`/`engine_runs` 기록
- 재열람이 엔진 재실행 없이 동일 결과, **원본 파일 삭제 후에도** 조회 가능
- 실제 샘플로 PDF 리포트 생성 (133KB)
- 얼린 exe에서 실제 fragmented MP4 추출

### 온라인 지도(카카오맵) — 2026-09-09

**실제 카카오 지도 위에 궤적이 그려지는 것까지 확인했다.** 카카오맵 사용 설정 ON +
JS SDK 도메인 등록 후, 도봉구 일대 40지점(끊김 2, 급가속 6) 궤적을 앱의 지도 페이지에
넣어 도로·건물·지명 위에 실선/점선/빨간선과 시작·끝·현재위치 마커가 나오는 것을 브라우저로
봤다. 헤드리스 WebEngine에서도 SDK `ready`, 타일 21장, 폴리라인 5개, 주소 변환
(`서울 중구 태평로1가 31`)까지 통과. 단 헤드리스 `grab()`은 빈 이미지라 리포트용 지도
캡처는 실제 화면에서 확인해야 한다(BUILD.md의 기존 주의와 동일).

컨테이너 크기가 작을 때 초기화되면 지도가 구석에 작게 그려지고 범위 맞춤도 그 크기
기준이 되는 것을 실측해서, `resize` 시 `map.relayout()` + 궤적 재맞춤(따라가기가 켜져
있을 때만)을 넣었다.

그 밖에 확인한 것:

- 실제 SDK 호출 실패 경로: 헤드리스 WebEngine에서 `map_kakao.html`을 띄워 SDK 로드 실패
  → 로컬 진단 → `error:domain` 상태 → `MapView.online_map_failed("domain", …)` 시그널까지
  전 구간 동작. 상태 문구 "카카오 디벨로퍼스에 이 프로그램 주소가 등록되지 않음"
- 가짜 SDK를 주입한 렌더링: 좌표 없음/GPS 끊김/급가속이 섞인 12지점 궤적이 실선 2·점선 1·
  빨간선 1로 나뉘고(공유 정점 포함 13개 좌표), 시작/끝/현재위치 오버레이, 화면 맞춤,
  가장자리 도달 시 `panTo`, 따라가기 끄면 이동 없음, 재렌더 시 이전 선 제거
- 지도 서버: 48213 고정 포트, 두 번째 인스턴스는 48214, `/map-config`·`/online-map-diagnose`,
  키 없으면 온라인이어도 오프라인 페이지, 경로 순회 차단 유지
- 실측 응답 분류: 401 도메인 불일치 / 403 사용 설정 꺼짐 / 401 키 없음 / 429 → 한도 초과
- 주소 변환: 캐시(11m), 일시 실패는 차단 안 함, 429·키 오류는 세션 차단 후 호출 0,
  오프라인 모드에서 호출 0. 워커는 연속 요청 중 마지막 것만 조회하고 실패는 한 번만 보고
- 안내 창: 원인별 한 번씩, 한도 초과 창의 "오프라인 지도로 전환" 버튼이 설정을 바꾸고
  지도를 다시 띄움
- 오프라인 페이지(`map.html`)는 온라인 분기를 걷어낸 뒤에도 배경지도·궤적 정상
  (헤드리스 실측에서는 `--disable-gpu`를 붙이면 WebGL이 아예 안 잡혔고
  `--enable-unsafe-swiftshader` 단독이 동작했다 — BUILD.md 참고)
