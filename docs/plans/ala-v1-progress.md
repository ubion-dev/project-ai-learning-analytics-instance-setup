# ala v1 진행 기록

## 현재 상태
- 단계: ALA-00 진행 중. 계획/진행/AGENTS 먼저 저장함. 기능 코드는 아직 없음.
- 최신 사용자 승인: ala v1 전체 계획 구현 (2026-10-29).
- 자동 검증/실기기 수용/정식 배포는 아직 미완료.
- 다음: 문서 첫 커밋 → 새 비공개 원격 → 원본 출처 검사 및 Rocky 스크립트 분리.

## 재개 시 필수
AGENTS.md → ala-v1-plan.md → 이 문서 → git status/diff/log 순으로 확인한다.
완료된 단계를 재구현하지 말고 현재 ID와 남은 완료 기준부터 보고한다.

## 조사 메모
- 원본 로컬 작업 브랜치: codex/configure-chrome-display-policy, head 9f41e1c.
- 원본 untracked .DS_Store/PDF 두 개는 범위 밖이며 건드리지 않는다.
- 계획의 기준 main 4a7128c에는 scripts/rocky-linux/setup.sh 경로가 없다는 git show 결과를 확인함.
  ALA-01 전 해당 커밋과 마지막 구현 커밋의 차이를 확인하고 출처를 명시할 것.
- 시스템 python3는 3.9.6이므로 개발/검증에는 별도 Python 3.12+ 런타임을 찾는다.

## 작업 표
| ID | 상태 | 증거 |
|---|---|---|
| ALA-00 | 진행 | 계획/진행/AGENTS 저장 |
| ALA-01 | 미착수 | |
| ALA-02 | 미착수 | |
| ALA-03 | 미착수 | |
| ALA-04 | 미착수 | |
| ALA-05 | 미착수 | |
| ALA-06 | 미착수 | |
| ALA-07 | 미착수 | 실기기 대상/유지보수 시간/화면 경로 미정 |
