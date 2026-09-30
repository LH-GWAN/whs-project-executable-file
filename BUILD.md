# IDAS 빌드 안내

## Windows exe 만들기

**PyInstaller는 크로스 컴파일이 안 된다.** Windows용 exe는 반드시 Windows PC에서
빌드해야 한다(macOS/Linux에서 빌드하면 그 OS용 실행파일이 나온다).

### 준비물
- Windows 10/11 64bit
- Python 3.11 또는 3.12 (64bit) — 설치 시 **"Add python.exe to PATH"** 체크
- **IDAS가 실행 중이면 먼저 끌 것.** 실행 중에 빌드하면 exe가 잡고 있는 `dist\` 파일을
  PyInstaller가 바꾸지 못해 깨진다. `build_windows.bat`과 `clean_windows.bat`은 실행 중인
  `IDAS.exe`가 있으면 시작하지 않고 안내 후 멈춘다.

### 빌드
프로젝트 폴더에서 `build_windows.bat`을 더블클릭하거나:

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pyinstaller idas.spec --noconfirm
```

결과물:
- **`IDAS.lnk`** — 프로젝트 폴더에 바로 생기는 실행 바로가기. **이걸 더블클릭**하면 실행된다.
- `dist\IDAS\IDAS.exe` — 실제 실행파일

### exe를 폴더 밖으로 옮길 수 없는 이유

one-dir 방식이라 **exe 옆에 `_internal\` 폴더가 반드시 같이 있어야** 한다
(Qt 라이브러리·분석 엔진·오프라인 지도 약 896MB). exe만 떼면 실행되지 않는다.
그래서 exe는 `dist\IDAS\`에 두고, 폴더 루트에는 바로가기만 만든다.

**바로가기는 이 PC에서만 동작한다** — 절대 경로를 가리키므로 다른 PC로 복사해도
소용없다. 배포할 때는 `dist\IDAS\` 폴더 전체를 옮기고, 받는 쪽에서
`IDAS.exe`를 직접 실행하거나 바탕화면 바로가기를 새로 만들면 된다.

### ⚠ 배포 시 주의
**exe 파일 하나만 떼어내면 실행되지 않는다.** `dist\IDAS\` **폴더 전체**를
옮겨야 한다. exe 옆 `_internal\` 안에 Qt 라이브러리와 분석 엔진(`engine\vendor\*.py`)이
들어 있다.

one-file(단일 exe) 대신 one-dir(폴더)로 만든 이유는 `idas.spec` 상단 주석 참고 —
요약하면 구동 속도, QtWebEngine 경로 문제, 백신 오탐 세 가지 때문이다.

---

### clean 해도 남는 것

`clean_windows.bat`은 `.venv\`, `dist\`, `build\`, 바로가기, `__pycache__\`만 지운다.
지도 온라인/오프라인 선택(`%LOCALAPPDATA%\IDAS\settings.json`)과 안내 창의 "다시
표시하지 않음"(레지스트리 `HKCU\Software\IDAS`)은 사용자 데이터라 남고, 그래서
재빌드 후에도 처음처럼 묻지 않는다. 스크립트 끝에서 "Reset app settings"에 Y를 답하거나
앱의 **설정 > 지도 설정 초기화**를 쓰면 다시 묻는다. 사건 이력(`history.db`)과 증거
폴더(`cases\`)는 어느 쪽도 지우지 않는다.

---

## 확인된 사항 (macOS에서 검증)

exe 자체는 Windows에서 만들어야 하지만, 빌드 설정(spec)과 패키징 로직은
플랫폼과 무관하다. macOS에서 실제로 얼려서 아래를 확인했다:

- 번들 크기 약 520MB (Qt/WebEngine 포함), 빌드 약 40초
- `engine/vendor/*.py`가 바이트코드가 아닌 **실제 .py 파일**로 번들에 포함됨
  (vendor 스크립트끼리 평평한 이름으로 import하는 구조라 이게 필수)
- 얼린 상태에서 `--run-engine` 자기 재호출이 동작하고, 합성 AVI로 실제 GPS 추출
  (`timeline.csv` + `coordinates.csv` + raw chunk) 성공
- GUI 정상 기동, Qt 플랫폼 플러그인 및 QtWebEngineProcess/icudtl.dat 번들 확인

### 빌드 중 실제로 잡은 문제
`integration_blackbox.py`가 쓰는 `shlex`가 번들에서 빠져 얼린 뒤에만
`ModuleNotFoundError`로 터졌다. vendor 스크립트를 **데이터 파일**로 넣기 때문에
PyInstaller의 정적 분석 대상이 아니어서 생긴 문제다. `idas.spec`의
`hiddenimports`에 vendor가 쓰는 표준 라이브러리를 명시해서 해결했다.

**→ vendor 엔진을 새 버전으로 갱신하면 이 목록을 반드시 다시 확인할 것:**

```bash
python3 - <<'PY'
import ast, pathlib
mods=set()
for f in ["integration_blackbox.py","integration_avi.py","integration_mp4.py"]:
    for n in ast.walk(ast.parse(pathlib.Path("engine/vendor/"+f).read_text())):
        if isinstance(n, ast.Import):
            for a in n.names: mods.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            if n.module and n.level==0: mods.add(n.module.split(".")[0])
print(sorted(mods - {"integration_avi","integration_mp4","integration_blackbox"}))
PY
```

---


## 배경지도 넣는 시점 (둘 다 됨)

**방법 A — 빌드 전** (권장): 지도 파일을 `assets\`에 먼저 넣고 빌드하면
번들에 포함돼서, `dist\IDAS\` 폴더만 옮겨도 지도가 따라간다. 배포용.

**방법 B — 빌드 후**: 이미 빌드한 상태라면 재빌드 없이
`dist\IDAS\_internal\assets\` 에 파일을 넣기만 하면 된다.
(폴더가 없으면 만들면 된다.) 확인:

```cmd
dist\IDAS\IDAS.exe --diagnose
```

`[OK] 배경지도 ...` 로 나오면 인식된 것이다. 371MB 전국판을 넣겠다고
20분짜리 재빌드를 다시 돌릴 필요는 없다.

---

## ⚠ 지도는 실제 화면이 있는 PC에서 반드시 확인할 것

지도(MapLibre)는 WebGL을 쓰고, Chromium은 **화면에 보이지 않는 페이지의 타이머와
렌더링을 억제**한다. 개발 환경의 헤드리스(offscreen) 테스트에서는 탭 위젯 안에
중첩된 WebEngine 뷰가 `visibilityState: "hidden"`으로 잡혀서 지도 초기화가 끝나지
않는 것을 확인했다(단독 위젯으로 띄우면 `visible`이 되고 100% 정상 동작).

- **단독 검증**: 궤적/위험운전 선·이름표/GPS 끊김 점선/시작·종료 마커/재생위치 마커/
  자동 화면맞춤 전부 정상 (반복 실행 12/12 통과)
- **앱 창 안에서의 검증**: 헤드리스로는 불가 — 실제 디스플레이가 있는 Windows에서
  Tracker/Location 탭을 열어 눈으로 확인해야 한다.

혹시 실제 PC에서도 "지도 준비 중…"에서 멈추면 그래픽 가속 문제이므로 위
"예상되는 문제"의 소프트웨어 렌더링 플래그를 적용한다.

---

## 온라인 지도 키 넣기 (선택)

온라인 모드(카카오맵)를 쓰려면 빌드 전에 `assets/online_keys.json`을 만든다.
발급 절차(앱 생성 → 카카오맵 사용 설정 → 키 복사 → JS SDK 도메인 등록)는
[`assets/README.md`](assets/README.md)에 있고, 프로그램 안에서도 온라인을 고르면
안내 창이 뜬다. 파일이 있으면 `idas.spec`이 자동으로 번들에 넣고, 없으면 온라인을
골라도 오프라인 지도로 표시된다. 빌드 후에 넣으려면 배경지도처럼
`dist\IDAS\_internal\assets\`에 두면 된다 — **재빌드도, 재시작도 필요 없다.**
프로그램 안내 창의 [다시 확인]을 누르면 바로 인식한다. 넣었는데도 "키 없음"이면 안내 창에
파일별 사유가 적혀 있고, `assets/README.md`의 "넣었는데 키 없음이 뜰 때"를 참고한다.

이 파일은 `.gitignore`에 있어 **git으로는 절대 전달되지 않는다.** 저장소가 공개라
키를 커밋하면 안 된다.

빌드 후 확인:

```cmd
dist\IDAS\IDAS.exe --diagnose
```

`온라인 지도 (카카오맵)` 항목에 키 유무(앞뒤 4자리만)와 카카오 디벨로퍼스에 등록할
도메인(`http://127.0.0.1:48213` 등 3개)이 나온다. 도메인은 **[앱] > [플랫폼 키] >
JavaScript 키 > [JavaScript SDK 도메인]**에 등록하며(제품 링크 관리의 웹 도메인이 아니다),
JavaScript 키가 여러 개면 도메인을 등록한 키를 `online_keys.json`에 넣어야 한다.
등록과 "카카오맵 사용 설정 ON"은 카카오 계정에서 해야 하며, 안 돼 있으면 프로그램이
원인별 안내 창을 띄운다(README 주의사항 15 참고).

---

## 배경지도(선택)

`assets/korea.pmtiles`를 넣으면 지도에 실제 도로/건물이 표시되고, 없으면 주행 궤적만
그려진다(프로그램은 양쪽 다 정상 동작). 만드는 방법은 `assets/README.md` 참고.

파일이 있으면 `idas.spec`이 빌드 시 자동으로 번들에 포함한다.

---

## Windows에서 빌드 후 반드시 확인할 것

개발 PC가 아닌 **깨끗한 Windows PC**(Python 미설치)에서 확인해야 의미가 있다.
개발 PC에는 필요한 DLL이 이미 깔려 있어 문제가 가려진다.

1. `IDAS.exe` 실행 → 창이 뜨는지
   (안 뜨면 Qt 플랫폼 플러그인 누락 — `_internal\PySide6\Qt\plugins\platforms\qwindows.dll` 확인)
2. 블랙박스 영상 업로드 → 분석 완료까지
3. Tracker 탭에서 **영상이 재생되는지** (H.264 디코딩 — Windows Media Foundation 사용)
4. Tracker/Location 탭에서 **지도에 궤적이 그려지는지**, 영상 재생 시 위치 마커가
   따라 움직이는지 (지도는 WebGL을 쓰므로 GPU 드라이버 영향을 받는다)
5. Report 버튼 → PDF 생성 (QtWebEngine 동작 확인)
6. 네트워크를 끊은 상태에서 위를 반복 — 외부 통신 없이 동작해야 함

### 예상되는 문제
- **백신/EDR 오탐**: 서명되지 않은 exe라 SmartScreen 경고가 뜰 수 있다. 배포 대상이
  수사기관이면 조직 인증서로 코드 서명하는 것을 권장.
- **자식 프로세스 차단**: 그룹 정책으로 자식 프로세스 생성이 막힌 PC에서는
  분석 엔진(서브프로세스)과 QtWebEngine이 실패할 수 있다. 이 경우 예외 등록 필요.
- **지도가 빈 화면**: 지도는 WebGL이 필요해서 GPU 드라이버가 오래됐거나 원격데스크톱
  세션이면 렌더 프로세스가 죽을 수 있다. 앱이 자동으로 3회까지 다시 띄우지만
  그래도 안 되면 아래 플래그로 소프트웨어 렌더링을 강제한다:
  ```cmd
  set QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu --enable-unsafe-swiftshader
  IDAS.exe
  ```
  (개발 환경의 헤드리스 테스트에서 5회 중 1회꼴로 이 현상이 재현됐다 — 실제 GPU가
  있는 PC에서는 훨씬 안정적이지만, 가상머신/원격 접속 환경이면 확인이 필요하다.)
  단, macOS 헤드리스 실측(Qt 6.11)에서는 `--disable-gpu`를 붙이면 WebGL이 아예
  안 잡혀 지도가 안 떴고 `--enable-unsafe-swiftshader` 단독이 동작했다. 한 조합이
  안 되면 `--disable-gpu`를 빼고 다시 시도할 것.
- **온라인 지도가 안 뜸**: 인터넷, 카카오 디벨로퍼스의 카카오맵 사용 설정, Web 사이트
  도메인(`http://127.0.0.1:48213`~`48215`) 등록, 키 파일 순으로 확인. 프로그램이
  원인별 안내 창을 띄우며 `--diagnose`로도 확인할 수 있다. 일일 한도(지도 30만·주소
  10만 건)를 넘으면 그날은 오프라인 지도로 전환해 쓰면 된다.

---

## 개발 중 실행 (빌드 없이)

```bash
python -m venv .venv        # ※ macOS에서는 ~/Desktop 밖에 만들 것 (Qt 플러그인 로드 실패)
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Linux 개발 환경 (Ubuntu 24.04)

Python 패키지 외에 Qt WebEngine 시스템 라이브러리가 필요하다.
`libXtst.so.6` / `libxkbfile.so.1` 오류가 나면 아래 의존성을 확인한다.

```bash
sudo apt-get update
sudo apt-get install python3-venv libxtst6 libxkbfile1 libnss3 libxss1 libasound2t64 libxcb-cursor0
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt   # pytest 포함
python app.py
```

회귀 검증은 `python -m pytest -q tests`로 실행한다. 합성 영상 디코더 시험은
FFmpeg, 지도 초기화 예외의 JavaScript 시험은 Node.js가 있으면 실행하고 없으면 skip한다.
Linux CI에서 위젯 시험은 `QT_QPA_PLATFORM=offscreen`으로 실행할 수 있다.
이 시험의 지도 위젯은 대체 객체로 격리하며, 실제 WebGL 지도·카카오맵·Windows EXE
검증을 대신하지 않는다. 배포 설정에서 Chromium sandbox를 끄지 않는다.


---

## 이미지 캡처 V5: 리뷰 전 검증 및 Windows 실제 실행 안내

V5는 최신 main을 기준으로 충격값 급증 구간의 **프레임 시각(Presentation
Timestamp, PTS)** 을 검증한다. GPS 이상치 규칙이나 Tracker, vendor 엔진을
변경하지 않는다. 보고서 기본 탐지 기준은 **직전 2초 평균 대비 절대 편차 1.00g 이상**이다.
현재 샘플은 평균에서 제외하며 최소 1초 관측과 이전 샘플 2개가 필요하다.
G센서 편차는 충돌 확정 또는 물리적 충격량(N·s)이 아니다.

### 1. GitHub Actions 전체 회귀 테스트 (main 합친 *뒤* 실행)

- V5 브랜치의 Actions → **IDAS regression (V5)** 에서 Linux 및 Windows
  작업의 최종 결론과 python -m pytest -q tests 로그를 확인한다.
- pytest가 실행 전 실패하거나 skipped가 나오면 전체 통과로 기록하지
  않는다. -rs로 skip 원인을 확인한다. Merge 전 원본 최신 main으로
  업데이트했다면 **업데이트 후** 테스트를 반드시 다시 실행한다.
- 수동 실행: Windows CMD에서 py -3.12 -m venv .venv,
  .venv\Scripts\activate.bat, python -m pip install -r requirements.txt,
  python -m pytest -q tests. FFmpeg의 ffmpeg -version과
  ffprobe -version도 함께 확인한다. 별도의 Git Bash는 필요 없다.
- GitHub CI는 Linux offscreen에서 실제 PySide6 위젯 테스트를 포함하지만,
  Windows 실사용 PC의 GPU, 코덱, 사용자 권한, QtWebEngine PDF 동작을
  완전히 대신할 수 없다.

### 2. 실제 Windows 10/11 64비트 검증 (개인정보 없는 시험 영상 사용)

1. Python 3.11/3.12 x64를 설치하고, 프로젝트를 짧은 일반 사용자 경로
   (예: C:\IDAS-v5)에 압축 해제한다. python -m venv .venv,
   .venv\Scripts\activate.bat, python -m pip install -r requirements.txt,
   python app.py 순서로 실행한다. 지도는 우선 오프라인으로 선택하고,
   테스트 기간에 온라인 API 키나 실제 사고 증거를 공개 저장소에 넣지 않는다.
2. 전방 또는 전후방 MP4/AVI를 분석한다. 원본 SHA-256과 분석 사본 해시
   일치, 출력 CSV 및 기록, no_gps와 engine_failed 메시지 구분을 확인한다.
   원본을 손상시키지 않는지 먼저 확인한다.
3. Tracker에서 재생/일시정지/앞뒤 탐색을 반복하고, **미래 GPS 좌표나 속도가
   뒤로 탐색한 화면에 남지 않는지** 확인한다. GPS (0,0), NaN, Inf 및
   체크섬 실패 행이 정상 경로/속도 통계에 들어가지 않는지 확인한다.
   주소 조회 동안 이전 주소가 깜빡이지 않는지도 확인한다.
4. 최소 1초의 기준 데이터 이후 평균 대비 1g 이상 벗어나는 시험 데이터를 사용해
   Report 버튼을 누른다. PDF의 감지 시각과 프레임 실제 PTS 및 편차를
   확인한다. 0.15g→0.45g처럼 배수가 커도 편차가 1g 미만이면
   캡처하지 않는다. 영상 경계를 넘어 평균을 계산하지 않는다. 원본 영상에서
   프레임을 직접 비교하고 타임라인의 기록과 일치하는지 확인한다.
   **원본 영상의 센서-비디오 내부 동기화는 PTS 검증만으로 증명되지 않는다.**
5. 1080p 이상 큰 영상, GPS 없는 영상, G센서 없는 영상, 손상된 영상,
   코덱 미지원 영상, 후방 전용 및 2영상 트랙을 차례로 확인한다.
   잘못되거나 PTS를 확인할 수 없는 프레임은 성공한 캡처처럼 PDF에 들어가면
   안 되고, 실패 사유를 보여주면서 일반 PDF 보고서는 만들어져야 한다.
6. PDF를 다시 열어 한국어 글꼴, 이미지, 실제 프레임 시각/편차, A4 여백을
   눈으로 확인한다. 큰 이미지에서도 PDF 저장 성공 여부를 확인한다.
   임시 경로의 idas-report-* 폴더는 성공/실패 후 정리되어야 한다.
   PDF에 원본 영상 파일 자체가 들어가는 기능은 아니다.
7. 마지막으로 build_windows.bat로 Windows exe를 빌드한다.
   dist\IDAS\ 폴더 **전체**를 깨끗한 Windows PC로 옮겨 실행해
   GUI·코덱·보고서·오프라인 경로를 반복한다. exe 단독 복사는 지원하지 않는다.
   CI는 이 실제 배포 테스트를 대체하지 않는다.

### 3. 시험 실패 시 리뷰어에게 남길 정보

python -m pytest -q -rs tests 마지막 요약과 실패한 테스트 이름,
실행 OS/Python/FFmpeg 버전, 사용한 익명 시험 영상의 컨테이너·영상 길이·
트랙 수, 기대 감지 시각과 실제 프레임 PTS, PDF 오류 문구를 남긴다.
사건 원본, 얼굴, 차량번호, 위치좌표, API 키는 공개 PR에 첨부하지 않는다.

### 4. 의도적으로 별도 범위로 남긴 기존 메모

engine_adapter.py의 기존 vendor 한글 문구 기반 no_gps 분류와
core/pipeline.py의 분석 사본 SHA-256 재검증의 단계별 진행률 UI는
이번 이미지 캡처 PR의 변경 대상이 아니다. 기존 코드의 상태 판정/무결성
규칙을 추측으로 바꾸지 않고 별도 이슈에서 벤더 API/출력 계약을 확인한 뒤
수정해야 한다. GUI Report 캡처는 현재 동기 방식이므로 긴 영상의 FFmpeg
디코딩에 시간이 걸리면 일시적으로 응답성이 낮아질 수 있다. 45초 추출
상한 후 실패 사유 또는 Qt 대체 경로를 사용한다.
