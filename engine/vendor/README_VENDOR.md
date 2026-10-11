# vendor 폴더에 대하여

여기 있는 파이썬 파일은 **이 프로젝트에서 수정하지 않는다.**
원본: https://github.com/LH-GWAN/whs-project (`ENGINE_COMMIT.txt`의 커밋)

2026-10: 원본 레포 작업 사본(`Desktop/화햇/project/whs-project`, `ENGINE_COMMIT.txt` 커밋 이후의
미커밋 수정 포함)에서 다시 복사했다. 이때 들어간 변경 - 슬랙 처리 방향 전환:
- `integration_avi.py`: 옛 녹화 잔재가 있어도 기본은 잘라내지 않는다(`--repair-slack`일 때만
  `_wo_slack.avi`). `--slack`으로 슬랙 영역(embedded/trailing)의 NMEA GPS를 `slack/`에 카빙
  (`run_slack_carve_avi`). `detect_slack_info()`. 첫 RIFF 뒤에 이어붙은 RIFF(`appended_riff`)도 슬랙.
- `integration_mp4.py`: `detect_slack_info()`. 두 번째 `ftyp`부터 끝까지 슬랙(`appended_file`).
- `integration_blackbox.py`: `--detect-slack`(추출 없이 파일마다 `SLACK_JSON …` 한 줄),
  `--slack`을 AVI에도 전달.

- `integration_blackbox.py` — 앱이 부르는 유일한 진입점(시그니처로 AVI/MP4 판별)
- `integration_avi.py` / `integration_mp4.py` — 위 파일이 import하는 하위 통합 스크립트

원본 레포의 `fragmentation/` 폴더(단독 스크립트 7종)는 같은 기능의 분해판이라
여기 두지 않았다 — 포렌식 도구에서 "실제로 어느 코드가 돌았는지"가 모호해지는 것을
피하기 위함. 위 3개는 표준 라이브러리만 쓰는 자립 스크립트라 그것만으로 동작한다.

## 엔진을 새 버전으로 갱신하려면

1. 위 3개 파일을 원본 레포에서 다시 복사
2. `ENGINE_COMMIT.txt`를 새 커밋 해시로 갱신
3. `ENGINE_README.md` / `ENGINE_ARCHITECT.md`도 같이 갱신(출력 형식 변경 여부 확인용)
4. `idas.spec`의 `hiddenimports` 재확인 (아래 명령으로 목록 추출 — vendor는
   데이터 파일로 번들되어 PyInstaller 정적 분석 대상이 아니라, 빠지면 얼린 뒤에만
   ModuleNotFoundError로 터진다):
   ```bash
   python3 - <<'EOF'
   import ast, pathlib
   mods=set()
   for f in ["integration_blackbox.py","integration_avi.py","integration_mp4.py"]:
       for n in ast.walk(ast.parse(pathlib.Path("engine/vendor/"+f).read_text())):
           if isinstance(n, ast.Import):
               for a in n.names: mods.add(a.name.split(".")[0])
           elif isinstance(n, ast.ImportFrom):
               if n.module and n.level==0: mods.add(n.module.split(".")[0])
   print(sorted(mods - {"integration_avi","integration_mp4","integration_blackbox"}))
   EOF
   ```
5. 앱 쪽에서 확인할 것:
   - `engine/registry.py` — 진입 모듈 이름이 그대로인지
   - `engine/engine_adapter.py` — CLI 형태(`-o <출력> <입력>`), `--slack`/`--mp4-opt`
     옵션, `timeline.csv` 컬럼(`start_time_sec`/`time_source`/`latitude`/`speed_kmh`/
     `x_g_cal` 등)이 그대로인지
   - `core/duration.py` — 재사용 중인 `find_top_level_sections`/`parse_hdrl`/
     `build_stream_table`/`compute_video_duration`/`scan_top_level`/`iter_boxes`/
     `find_box` 시그니처가 그대로인지
   - `core/format_sniffer.py` — `detect_container`/`check_extension_mismatch` 가 그대로인지
   - `track_table.csv`의 `is_text_track` 의미 — fragmented 경로는 "이번에 처리한 Track만
     True"라서 나머지를 추가 실행해야 하고, sample table 경로는 "모든 text Track이 True"라
     추가 실행이 필요 없다. 이 규칙이 바뀌면 `engine_adapter._pending_track_ids`도 고칠 것.

## 2026-10-09 코드 리뷰 반영 (whs-project와 동일 내용)

- `integration_avi.py`: `write_avi_timeline(..., decode_summary=)`가 판정(text/record72)된 스트림만 쓰고
  `gps_trusted` 열을 더한다. 센서는 가장 가까운 시각(1초 이내, `bisect`)으로 붙인다. OpenDML은
  `first_riff_video_duration`으로 첫 RIFF 길이. `find_embedded_nmea_text`는 RMC 우선·checksum 우선.
  `parse_finevu_record`는 길이 69~80·전부 0 거부. `resolve_targets`는 hdrl 없을 때 dc/db/wb/pc 청크 제외.
  출력 폴더는 `unique_out_dir`(끝 공백·점 제거, 같은 stem은 `_2`). CSV는 `_GuardDictWriter`/`_GuardListWriter`
  (수식 주입 방지).
- `integration_mp4.py`: `decode_sample_text`가 길이 프리픽스 뒤 패딩·Box를 허용. route가 None이어도 `--slack`이면
  `run_slack_carve`. 슬랙 카빙은 mmap(`_carve_one_region`/`_finish_slack_carve`). `unique_out_dir`, `_GuardDictWriter`.
- 앱이 읽는 timeline 열: `gps_status`, `gps_elapsed_sec`, `gps_trusted`, `abs_time`가 추가됐다(engine_adapter.load_timeline).

## 2026-10-11 손상 파일 허용 (whs-project와 동일 내용)

- `integration_blackbox.detect_container`: 앞 8바이트가 깨져도 `sniff_damaged_container`가 내용(hdrl/movi/idx1 또는
  Box 사슬, 앞·뒤 8MB → 전체)으로 판별한다. note에 "시그니처 손상"이 들어간다.
- `integration_avi.py`: RIFF가 아니어도 종료하지 않는다. `find_top_level_sections_damaged`(hdrl/movi/idx1 바이트 스캔,
  idx1 엔트리 역산으로 movi 기준점 복원), `find_idx1_fallback`은 `_idx1_entries_plausible`로 가짜 idx1을 거부,
  `walk_movi_chunks`(재동기화, 비패딩 청크 지원), `synthesize_idx1`(idx1 없을 때 색인 재구성, 손상 구간 빈 자리,
  `recording_cut_at`으로 옛 녹화 잔재 제외), `detect_base_offset` 후보 D(엔트리 역산), timeline에 `source_offset`
  (청크 위치), 재생 시각 없는 좌표 행도 timeline에 남김(헤더 소실 - 앱이 UTC로 추정), stdout `DAMAGE_JSON` 줄.
- `integration_mp4.py`: size 0 + 타입 0x00000000 Box를 손상으로 봄, `scan_top_level_bounded`가 `resync_top_level`로
  다음 정상 Box에서 이어 읽고 `DAMAGE_GAPS`에 구간을 남김, moof 없는 mdat(`UNINDEXED_MEDIA`)와 손상 구간의 RMC를
  `carve_gap_gps`가 UTC로 시간축에 되돌려 넣음(`time_source=gap_carved_utc`), timeline에 `source_offset`.
- 앱이 읽는 열: `source_offset`(바이트 위치), `time_source`가 `gap_carved_utc`면 `recovery_state="gap_carved"`.

## 2026-10-11 코드 리뷰 2차 반영 (whs-project와 동일 내용)

- `integration_avi.py`: `_idx1_entries_plausible`은 색인 전체 표본(0으로 덮인 자리는 분모 제외)이 과반 일치하고 현재
  movi의 첫 청크 사슬(`first_chunk_chain`)과 맞물릴 때만 받으며, 첫 청크 사슬에서 역산한 base 후보도 본다.
  `find_idx1_fallback`은 검증을 통과한 후보 가운데 자기 영역 뒤 가장 앞의 idx1을 고른다(뒤에 붙은 옛 idx1 제외).
  `find_top_level_sections_damaged`는 첫 청크 사슬 → movi → hdrl(movi보다 앞) 순. `recording_cut_at(mm, walked, gaps,
  info=)`은 체크섬 맞고 날짜 있는 A 상태 RMC만 근거로 하고 근거가 없으면 `boundary_uncertain`(뒤는 `after_cut_uncertain`
  슬랙). `walk_movi_chunks(..., tail_info=)`는 `LIST rec ` 안으로 들어가고 0 꼬리·잘린 꼬리를 gap에 넣지 않는다.
  `synthesize_idx1` 빈 자리 수는 앞뒤 앵커 시각 차이 기준, placeholder는 index.csv에 `DAMAGED_PLACEHOLDER`.
  `measured_video_duration`·`supplement_truncated_index`, `_count_avix_after`. `classify_payload`가 전부 0을 `empty`로.
  `find_embedded_nmea_text`는 체크섬 맞는 RMC → GGA → 체크섬 없는 RMC → GGA 순. stdout `DAMAGE_JSON`에
  `idx1_truncated`, `supplemented_entries`, `placeholders`, `cut_at`, `cut_reason`, `boundary_uncertain`, `zero_tail`,
  `truncated_tail`, `last_chunk_end`, `measured_duration_sec`, `header_duration_sec`. decode_detection.csv에 `empty` 열.
- `integration_mp4.py`: `scan_top_level_bounded(f, filesize, cancel_check=None)`가 moof마다 mfhd 순번·tfdt·GPS 날짜+UTC
  (`_moof_probe`)를 보고 이어질 때만 받으며, 어긋나면 boundary_kind `mfhd`/`tfdt`/`gps`/`moov`/`moof`로 슬랙 경계. 이어지지
  않는 moof 하나는 뒤 조각 2개를 미리 읽어 손상으로만 처리(`_lookahead_moofs`). 내부가 손상된 moof도 DAMAGE_GAPS.
  끝까지 0인 꼬리는 `padding`. size 0은 최상위 mdat만, truncated는 알려진 Box 타입만. 카빙(`_carve_gap_gps`)은 EOF 꼬리
  제외·날짜 일치·앞뒤 앵커 UTC 사이·영상 길이 안일 때만 받고 `CARVED_GPS_OFFSETS`로 슬랙 중복을 막는다. largesize mdat
  잘림은 truncated. 혼합 fMP4의 moov 샘플 표 구간은 `_hybrid_moov_samples`(time_source `stts`). `assign_utc_elapsed_times`는
  체크섬 실패 행을 UTC 없는 행으로. `_gps_rank`로 대표 문장 선택. 스캔 결과 LRU 캐시.
- `integration_blackbox.py`: `sniff_damaged_container`가 앞 창에 흔적이 없으면 뒤 8MB도 보고, AVI 근거가 있으면 전체 MP4
  탐색을 생략한다. RIFF formType이 깨진 파일은 알려진 다른 RIFF 형식만 거부하고 내용으로 판별한다. 배치 `main`은
  `_SharedReservations`로 두 모듈의 출력 폴더 예약을 공유한다.
- 앱은 `DAMAGE_JSON`을 `ExtractionResult.engine_damage`로 보관하고 `measured_duration_sec`로 AVI 길이를 보정한다.
