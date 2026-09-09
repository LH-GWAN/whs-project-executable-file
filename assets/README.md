# 배경지도(오프라인 벡터 타일)

`korea.pmtiles`가 이 폴더에 있으면 지도에 실제 도로/건물/수계가 표시된다.
**파일이 없어도 프로그램은 정상 동작한다** — 그 경우 배경 없이 주행 궤적만 그려진다.

배경지도를 앱에 내장하는 이유는 비용이 아니라 **기밀 유지**다. 구글/네이버 지도 API를
쓰면 분석할 때마다 사건 GPS 좌표가 외부 서버로 전송되는데, 수사 자료로는 부적절하다.
로컬 파일을 쓰면 망분리 PC에서도 동작하고 좌표가 밖으로 나가지 않는다.

## 온라인 지도(카카오맵) 키 — `online_keys.json`

오프라인 지도 대신 카카오맵을 쓰는 온라인 모드용 키 파일. 배경지도 파일과 마찬가지로
**git으로는 오지 않는다**(`.gitignore`, 저장소가 공개라 키를 올리면 안 된다). 프로그램은
온라인을 골랐는데 이 파일이 없으면 **안내 창을 띄우고**, [설정 > 온라인 지도 키 설정
안내]에서 언제든 다시 볼 수 있다(`ui/online_keys_notice.py`).

### 발급받는 방법 (처음 한 번, 카카오 계정 필요)

1. https://developers.kakao.com/console/app 에 로그인해 **[애플리케이션 추가]**로 앱을
   만든다(이름은 아무거나). 이미 만든 앱이 있으면 그 앱을 쓴다.
2. 왼쪽 메뉴 **[카카오맵] > 사용 설정**을 **ON**으로 켠다. 꺼져 있으면 지도·주소 둘 다
   `App disabled OPEN_MAP_AND_LOCAL service`로 거절된다.
   - 무료 한도(일 지도 30만 건·좌표→주소 10만 건)는 **개발자 계정에서 처음 켠 앱 하나**에만
     붙는다. 두 번째 앱부터는 유료 API 설정이 필요하니 앱을 여러 개 만들지 말 것.
3. **[앱] > [플랫폼 키]** 화면에서 두 값을 복사한다.
   - **JavaScript 키** → `kakao_js_key` (지도 표시)
   - **REST API 키** → `kakao_rest_key` (좌표→주소 변환)
   - 네이티브 앱 키는 Android/iOS용이라 쓰지 않는다.
4. 같은 화면에서 **JavaScript 키 카드 > [JavaScript SDK 도메인]**에 아래 3개를 등록한다.
   ```
   http://127.0.0.1:48213
   http://127.0.0.1:48214
   http://127.0.0.1:48215
   ```
   프로그램이 이 포트에서 지도 페이지를 열고, 카카오는 요청 출처를 포트까지 비교한다.
   - **[제품 링크 관리] > 웹 도메인은 다른 설정이다**(카카오톡 공유용). 거기 넣으면 계속
     `domain mismatched`가 난다.
   - JavaScript 키가 여러 개면 **도메인을 등록한 키**를 3번의 `kakao_js_key`로 써야 한다.
     플랫폼 키 화면에서 "JS SDK 도메인" 배지가 붙은 카드가 그 키다.

### 넣는 방법

프로그램 안내 창의 [키 파일 만들기]를 누르면 빈 틀이 생기고 편집기로 열린다. 직접 만들
때는 `online_keys.example.json`을 이 폴더에 `online_keys.json`으로 복사하고 값을 채운다.

```json
{
  "provider": "kakao",
  "kakao_js_key": "JavaScript 키",
  "kakao_rest_key": "REST API 키"
}
```

- **개발 실행**: 이 폴더(`assets/`)에 넣고 프로그램을 다시 시작하면 인식된다.
- **exe 빌드**: 빌드 전에 넣으면 `gpstracer.spec`이 번들에 넣는다. 이미 빌드했으면
  `dist\GPSTracer\_internal\assets\`에 넣어도 된다(재빌드 불필요).
- **재빌드 없이 키 교체**: `%LOCALAPPDATA%\GPSTracer\settings.json`에
  `"kakao_js_key"`, `"kakao_rest_key"`를 넣으면 파일보다 우선한다(유출로 재발급했을 때).

확인:

```cmd
GPSTracer.exe --diagnose
```

`온라인 지도 (카카오맵)` 항목에 키가 앞뒤 4자리로 표시되면 인식된 것이다.

### 팀에 나눠줄 때

배경지도와 같다 — 키 파일은 git이 아니라 USB/드라이브로 옮겨 `assets/`에 넣는다. exe로
배포할 때는 빌드하는 PC에만 있으면 되고, 받는 쪽은 exe 안에 든 키를 그대로 쓴다.
같은 앱의 키를 여러 PC가 써도 되지만 한도는 앱 단위로 합산된다.

### 안 될 때

프로그램이 원인별로 안내 창을 띄운다.

| 안내 | 원인 | 조치 |
|---|---|---|
| 도메인 미등록 | 4번을 안 했거나 다른 키/다른 항목에 함 | JavaScript SDK 도메인에 3개 등록, 등록한 키 사용 |
| 카카오맵 사용 설정 꺼짐 | 2번을 안 함 | 사용 설정 ON |
| 키 오류 | 값이 틀리거나 비어 있음 | 플랫폼 키 화면 값과 대조 |
| 사용 한도 초과 | 그날 무료 한도 소진 | 오프라인 지도로 전환(안내 창 버튼), 다음 날 재시도 |
| 연결 실패 | 인터넷 없음 | 망분리 PC면 오프라인 지도 사용 |

온라인 모드에서는 지도 화면 범위와 주소를 표시할 좌표가 카카오 서버로 전송된다.
기밀이 중요한 사건은 오프라인을 쓸 것.

## 현재 들어있는 파일

| 항목 | 값 |
|---|---|
| 범위 | 대한민국 전역 (제주·울릉도·독도 포함, bbox `124.5,33.0,132.0,38.7`) |
| 최대 줌 | 14 (도로 수준. 벡터라 더 확대해도 자연스럽게 늘어남) |
| 크기 | 약 371 MB |
| 원본 데이터 | OpenStreetMap 2026-08-25 스냅샷 |
| 스키마 | Protomaps Basemap (`earth`/`water`/`landuse`/`roads`/`buildings`/`boundaries`) |
| 라이선스 | ODbL (무료 사용·배포 가능, 출처 표시 필요 — 지도 우측 하단에 자동 표시) |


## 배포 방법 (팀에 나눠줄 때)

지도 파일은 **git으로 배포할 수 없다.** GitHub 웹 업로드는 25MB에서 막히고,
저장소 일반 파일도 100MB 제한이라 `.gitignore`로 제외돼 있다.
`git pull`로는 절대 받아지지 않는다.

**외부 공유 링크로 배포한다.** 현재 배포 위치:

    https://drive.google.com/file/d/1WEV52yIpNVDtSla2f5ZLGmWk_wE0SCic/view?usp=drive_link

프로그램은 시작할 때 `assets/`에 지도 파일이 없으면 **안내 창을 한 번 띄우고**,
[지도 내려받기] 버튼으로 위 주소를 바로 연다(`ui/basemap_notice.py`).
파일을 다른 곳으로 옮기면 그 파일의 `BASEMAP_DOWNLOAD_URL` 상수만 고치면 된다.

받는 사람이 할 일:
1. 링크에서 파일을 받는다
2. `assets/` 폴더에 넣는다
3. 프로그램을 다시 시작한다 (exe로 쓴다면 다시 빌드)

**압축을 풀 필요는 없다.** `.zip`을 그대로 넣어두면 프로그램이 시작할 때
자동으로 풀어서 쓴다(`extract_zipped_basemap`). PMTiles는 내부가 이미 gzip이라
zip으로 감싸도 용량은 그대로다(실측 100%) - 압축이 아니라 확장자 포장 용도다.

**파일명은 자유다.** `korea.pmtiles`든 `sudogwon.pmtiles`든 `assets/` 안의
`.pmtiles`를 자동으로 찾는다(여러 개면 가장 큰 것을 쓴다).

---

## 다시 만들거나 갱신하는 방법

OSM 원본(1GB)을 받아 Java/planetiler로 수십 분 변환할 필요가 **없다.** Protomaps가
공개한 planet 아카이브에서 필요한 지역만 HTTP Range로 잘라오면 몇 분이면 끝난다.

```bash
# 1) pmtiles CLI 받기 (OS/아키텍처에 맞는 것으로)
#    https://github.com/protomaps/go-pmtiles/releases
#    예: go-pmtiles-<버전>_Darwin_arm64.zip / _Windows_x86_64.zip

# 2) 최신 planet 빌드 날짜 확인 (일별 빌드, 없는 날짜도 있음)
curl -sI https://build.protomaps.com/20260825.pmtiles | head -1

# 3) 한국 영역만 추출 (약 4분, 400MB 전송)
./pmtiles extract https://build.protomaps.com/20260825.pmtiles korea.pmtiles \
  --bbox=124.5,33.0,132.0,38.7 --maxzoom=14 --download-threads=8
```

만들어진 `korea.pmtiles`를 이 폴더에 넣으면 끝이다.
- **개발 실행 시**: 바로 인식된다.
- **exe 빌드 시**: `gpstracer.spec`이 파일이 있으면 자동으로 번들에 포함한다.

### 줌 레벨별 크기 (실측)

| 범위 | maxzoom | 크기 | 비고 |
|---|---|---|---|
| 전국 | 12 | 99 MB | 도시 단위 파악 |
| 전국 | 13 | 205 MB | 주요 도로 |
| 전국 | **14** | **371 MB** | 도로 수준 |
| **수도권** | **14** | **38 MB** | 옮기기 쉬움 (`--bbox=126.6,37.2,127.5,37.8`) |
| 수도권+충청 | 14 | 94 MB | `--bbox=126.4,36.2,127.9,37.9` |
| 서울시 | 15 | 39 MB | 건물 단위 (`--bbox=126.76,37.42,127.19,37.70`) |

관할 구역이 정해져 있으면 `--bbox`를 좁혀 훨씬 작게 만들 수 있다
(예: 수도권만 `--bbox=126.6,37.2,127.3,37.8`).

지도를 지역별로 나눠 만들 필요는 **없다.** 앱이 분석된 GPS 궤적 범위에 맞춰 화면을
자동으로 맞추므로(`fitBounds`), 전국 파일 하나로 서울 사건은 서울이, 경기 사건은
경기가 알아서 보인다.

## 스타일을 손볼 때 주의

`ui/web/map.html`의 배경지도 레이어는 위 표의 Protomaps 스키마 이름을 쓴다
(OpenMapTiles의 `transportation`/`building`이 아니라 `roads`/`buildings`).

또 **줌 기반 `interpolate`는 표현식 최상위에 한 번만** 올 수 있다. 도로 굵기를
`["match", ..., interpolate, interpolate]`처럼 감싸면 MapLibre가 레이어를 통째로
거부하고(콘솔에 `Only one zoom-based "step" or "interpolate" subexpression...`)
도로가 통째로 안 그려진다. `interpolate`를 바깥에 두고 각 줌 구간 값 안에서
`match`로 종류별 굵기를 고를 것.
