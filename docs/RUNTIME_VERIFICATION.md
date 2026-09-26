# 수정 위치와 직접 실행 재검증

검증 기준: 수정 전 `ba69920`, 제품 수정 후 `76df33b`. 이후 커밋은 시험·검증 문서 보강이다.
실제 사용자 제공 MP4(60.06초)와 합성 H.264/AAC MP4를 사용했다.
환경은 Linux, Python 3.12.14, PySide6 6.11.2, FFmpeg Qt 디코더, offscreen 위젯이다.
지도/주소 네트워크만 대체 객체로 격리했고 엔진 subprocess·SQLite·영상 디코더·Qt 신호는 실제 실행했다.

## 실영상 동일 조건 비교

양쪽 엔진 결과는 모두 `ok`, 600행, GPS 60개, 60.06초, 엔진 종료 코드 0이었다.
실제 좌표와 개인 파일 경로를 제외한 계측값은 `runtime_comparison.json`에 있다.

| 조작/통제 입력 | 수정 전 | 수정 후 | 판정 |
|---|---|---|---|
| 40.7초→1.5초 | 이 샘플에서는 정상 | 정상 | 이 이동 자체가 이번 최신 원본의 실패 사례는 아님 |
| 이후 0초/0.39초 이동 | 첫 GPS 이전인데 과거에 본 좌표·속도 잔류 | 두 시점 모두 위치/속도 `-` | 실영상으로 결함 재현 및 해결 |
| 2초에서 Right 방향키 | 슬라이더 2001ms, 영상 2000ms | 둘 다 3000ms | 실영상으로 해결 |
| 첫 프레임 초기화 중 3.5초 탐색 | 350ms 뒤 0ms | 350ms 뒤 3500ms | 실영상으로 해결 |
| 첫 프레임 초기화 중 재생 클릭 | 400ms 뒤 정지, 0ms | 재생 중, 333ms | 실영상으로 해결 |
| 로드 완료 후 3회 재열기 첫 재생 | 세 번 모두 재생 | 세 번 모두 재생 | 일반 재열기는 이미 정상; 신규 해결로 계산하지 않음 |
| 정상30 + 이상치999 속도(통제 데이터) | 평균514.5 / 최고999.0 | 평균30.0 / 최고30.0 | 실제 SpeedTab 위젯에 입력해 해결 |
| `REC_F_1.avi` 후방 후보 | 후보 없음 | `REC_R_1.avi` 포함 | 함수 직접 실행 |
| 실제 600행의 보고서 HTML | 반복 생략행189개 | 반복 생략행0개 | 보고서 함수 직접 실행 |
| 오디오 출력 객체 | 없음 | QAudioOutput 연결 | 연결 확인; 실제 청감은 미검증 |

수정 후 40.7/1.5/0/0.39/10초 다섯 탐색 모두 올바른 과거 GPS 기준 표시를 확인했다.
각 탐색에서 실제 영상 프레임 콜백을 받았다. 프레임 시작 시각은 각각
40.673967/1.468133/0/0.367033/9.976633초였다. 이는 디코더가 해당 프레임을
전달했다는 증거이며, QMediaPlayer의 프레임 단위 정밀 탐색 보증은 아니다.

## 파일·함수별 수정과 확인

| 수정 파일 | 주요 함수/위치 | 바꾼 오류와 검증 |
|---|---|---|
| `ui/tracker_tab.py` | `_slot_point`, `_update_info`, `_show_address` | 미래 행/재생이력 캐시 대신 현재 이하 기록 조회. 실영상 0초·0.39초 정상. 1.42초에 미래1.45초 선택 방지 회귀 |
| 같은 파일 | `load_video`, `_on_media_status`, `_cancel_priming`, `_finish_prime`, `stop` | 취소 가능한 타이머, source 전환 시 초기화. 실영상 프라이밍 중 탐색·재생 보존 |
| 같은 파일 | `__init__`, `_on_slider_moved`, `_on_position_changed` | valueChanged·QSignalBlocker·1초 키보드·드래그 release. 실영상 방향키 위치 일치 |
| 같은 파일 | `_on_media_error`, `_sync_rear`, QAudioOutput 연결 | 누락 파일 오류/조작 차단, 후방 오류 독립, 짧은 후방 범위 복귀. 합성 디코더·실제 위젯 시험 |
| `ui/speed_tab.py` | `load` | 평균·최고의 이상치 유입 제거. 위젯 입력30/999 결과30/30 |
| `core/acceleration.py` | `_distinct_fix_indices` | 그래프/통계/급가감속 유효 측정 기준 공유. 실패체크섬·trusted=False·Inf 제외와 반복 기록 시험 |
| `core/outliers.py` | `_coords_valid`, `_groups`, `mark_outliers`, `haversine_m` | NaN/Inf·범위 밖 차단, 정상(0,0) 허용, 거리 수치 범위 보호. 입력 경계값 실행 |
| `engine/engine_adapter.py` | `TrackPoint.has_fix`, `_f`, `load_timeline`, `_restore_timeline_trust`, `load_coordinates_as_points` | 검사 실패 좌표 사용 방지, 좌표CSV 신뢰도 복원. 실제 CSV 입출력 시험 |
| 같은 파일 | `_count_fixes`, `run_full_extraction`, `_classify_outcome` | 검증된 좌표 수로 대표CSV 순위, 출력 재사용 거부, no_gps/engine_failed 구분. 합성 MP4 실제 엔진·History 왕복 |
| `core/pipeline.py` | `run_analysis_pipeline`, `_safe_case_folder_name`, `reopen_case`, `_write_case_json` | 분석 전 해시, 실패 정리, 이름 길이 제한, 상태 복원. 실제 파일·DB + 실패 주입 회귀; 실영상 분석 성공 |
| `core/video_pairs.py` | `_PAIR_RULES`, `rear_candidates` | 번호 접미사를 보존하는 F_1→R_1 후보. 기존 F/R도 통과 |
| `ui/map_view.py` | `compute_headings`, `_run_js`, `set_track` | 미래 방향 참조 제거, 로딩 중 최신 상태만 보관. 정차 방향 통제 입력 시험; 실제 지도 렌더는 미검증 |
| `ui/web/map.html` | `boot()` 호출부 try/catch | WebGL 생성자 예외에서 무한 대기 대신 안내. 실제 HTML 스크립트 Node VM 시험 |
| `ui/analysis_view.py` | `load_result` | 새 사건에 영상 없을 때 이전 source 제거, 상태·경고 표시. 실제 AnalysisView 위젯의 사건A→B 시험(지도 격리) |
| `ui/location_tab.py` | `__init__`, `load` | GPS 검증 열과 실패값 표시. 실제 표에서 정상/실패/미제공, 실패 행 외부지도 링크 제거 확인 |
| `ui/case_info_dialog.py` | `_on_start` | 분석 체크박스 전부 해제 시 진행 차단. 실제 대화상자 로직 시험(경고 메시지 창만 대체) |
| `report/report_builder.py` | `_select_row_indices`, `render_report_html` | 전체 구간 표본, 반복 생략행 제거, 경고·검증·복구·통계 표기. 실제600행 HTML·이스케이프 시험 |

## 검증 보강 및 한계

이번 재검증에서 회귀 시험도 보강했다. GPS 공백 입력에 정상 주기 기록을 추가해 실제
공백으로 판정되도록 했고, 매 키보드 탐색 직전에 프레임 목록을 비워 과거 프레임으로
통과하지 않게 했다. 초기화 중 재생 유지, 사건 영상 부재, Location 검증 열 시험을 추가했다.
최종 결과: **34 passed**. 제품 코드에 추가 수정은 필요하지 않았다.

실제 지도 화면(WebGL/카카오)·주소 서비스, Windows EXE, AVI/JDR 실기종,
스피커 청감, PDF 인쇄 레이아웃은 아직 확인하지 않았다. 지도는 시험에서 대체했으므로
'지도 화면까지 통합 검증 완료'라고 보지 않는다. 지도 클릭→영상은 여전히 미구현이다.
개인 원본 영상·실제 위치·사건 DB는 PR/소스 ZIP에 포함하지 않았다.
