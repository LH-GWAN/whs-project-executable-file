# 배경지도(오프라인 벡터 타일)

`korea.pmtiles`가 이 폴더에 있으면 지도에 실제 도로/건물/수계가 표시된다.
**파일이 없어도 프로그램은 정상 동작한다** — 그 경우 배경 없이 주행 궤적만 그려진다.

배경지도를 앱에 내장하는 이유는 비용이 아니라 **기밀 유지**다. 구글/네이버 지도 API를
쓰면 분석할 때마다 사건 GPS 좌표가 외부 서버로 전송되는데, 수사 자료로는 부적절하다.
로컬 파일을 쓰면 망분리 PC에서도 동작하고 좌표가 밖으로 나가지 않는다.

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
