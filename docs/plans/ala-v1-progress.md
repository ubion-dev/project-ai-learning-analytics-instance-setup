# ala v1 진행 기록

## 현재 상태
- 단계: ALA-02 진행 중. ALA-00/01 완료. 공통 계약 저장, engine 구현 위임 중.
- 최신 사용자 승인: ala v1 전체 계획 구현 (2026-10-29).
- 자동 검증/실기기 수용/정식 배포는 아직 미완료.
- 다음: 문서 첫 커밋 → 새 비공개 원격 → 원본 출처 검사 및 Rocky 스크립트 분리.

## 재개 시 필수
AGENTS.md → ala-v1-plan.md → 이 문서 → git status/diff/log 순으로 확인한다.
완료된 단계를 재구현하지 말고 현재 ID와 남은 완료 기준부터 보고한다.

## 조사 메모
- 원본 로컬 작업 브랜치: codex/configure-chrome-display-policy, head 9f41e1c.
- 계획 첫 커밋: cf81793. 분리·공통 계약 커밋: 9244598.
- GitHub 비공개 레포지토리 생성 및 main 문서 커밋 업로드 완료.
- 원본 untracked .DS_Store/PDF 두 개는 범위 밖이며 건드리지 않는다.
- 초기 기준 커밋 조회 실패는 로컬 fetch 미완료 때문이었다. git fetch origin main 후 4a7128c가 PR #10 merge임을 확인하고 git archive로 scripts/rocky-linux만 추출했다. 계획 기준 변경 없음.
- 시스템 python3는 3.9.6이므로 개발/검증에는 별도 Python 3.12+ 런타임을 찾는다.

## 작업 표
| ID | 상태 | 증거 |
|---|---|---|
| ALA-00 | 완료 | cf81793; 비공개 GitHub 생성 |
| ALA-01 | 완료 | 기준 커밋 archive; 85 tests 통과 |
| ALA-02 | 진행 | config/model/system 계약; ala_engine 위임 |
| ALA-03 | 미착수 | |
| ALA-04 | 미착수 | |
| ALA-05 | 미착수 | |
| ALA-06 | 미착수 | |
| ALA-07 | 미착수 | 실기기 대상/유지보수 시간/화면 경로 미정 |

## 실행 계약 (모든 작업자 필독)
- Config는 ala/config.py, CheckResult/Report는 ala/model.py, Host/Command/Paths는 ala/system.py를 사용한다.
- Engine(config, host=None, paths=None).inspect()/plan()/apply(on_stage=None) -> Report.
- Chrome 작업은 ala/display.py: inspect_display(config,host,paths)->list[CheckResult], apply_display(config,host,paths)->list[CheckResult], chrome_action(action,config,host=None,paths=None)->dict 계약.
- TUI는 service 객체의 비동기 인터페이스를 쓰며 root CLI가 곧 계약을 추가한다.
- 개발 Python: /tmp/ala-dev/bin/python (3.14); 실제 지원/CI는 Python3.12. Textual8.2.8.

## 진행 체크포인트
- 분리 기준선85 tests 통과(72.021s). Config/model/path22 + root job/CLI11 =33 tests 통과(1.65s).
- ALA-02 engine 실구현 및 모의18검증이 도착하여 root 통합검토 중.
- ALA-03의 job/CLI 고정 인터페이스를 사전 구현·검증했고 패키징 작업은 ala_packaging에 독립 위임했다. 이 단계의 완료 선언은 engine/패키징 통합 검증 이후 한다.
- 실행 잠금은 Linux /run/lock 자체 권한 차이를 피하기 위해 /run/lock/ala/operation.lock로 구체화했다.
- 다음: 엔진 검토/테스트 통과 후 Chrome/TUI 병렬 위임.
