# 적용 및 PR 준비

이 수정의 기준은 원본 저장소 main `ba699200282eb7fbbd2180fb1abbeaee4555f6a9`이다.
브랜치는 `fix/review-integrity-playback`, PR 본문은 `PR_DESCRIPTION.md`에 있다.

## 본인 Fork에서 패치 적용

커밋하지 않은 변경이 없는 본인 Fork 로컬 저장소에서 진행한다.
`origin`이 본인 Fork인지 `git remote -v`로 확인한다.

```bash
git fetch origin
git switch -c fix/review-integrity-playback origin/main
git am /path/to/IDAS_review_fixes.patch
python -m pytest -q tests
git push -u origin fix/review-integrity-playback
```

`/path/to/IDAS_review_fixes.patch`는 다운로드한 실제 경로로 바꾼다.
기준 main과 달라 충돌하면 무시하거나 강제 적용하지 말고 충돌 파일을 확인한다.
원본 저장소의 변경을 이미 별도 PR로 수정 중이면 이 패치와 중복되는지 먼저 확인한다.

이후 GitHub 비교 화면에서 base `LH-GWAN/whs-project-executable-file:main`,
head 본인 Fork의 `fix/review-integrity-playback`을 선택한다.
변경사항과 `PR_DESCRIPTION.md`를 검토하면 PR 제출을 준비할 수 있다.

이번 작업에서는 로컬 커밋까지만 수행했으며 원격 push/PR 생성은 하지 않았다.
전체 소스 ZIP은 실행/검토용이고, 기존 개발 폴더를 무조건 덮어쓰는 용도가 아니다.
