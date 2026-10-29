# ala v1 진행 기록

## 현재 상태 — 재개할 때 여기부터

- ALA-00~06: 구현·모의/headless·임시 설치·Python3.12 CI 검증 및 RC 공개 완료. 실기기 수용은 별도다.
- ALA-07: 실기기 수용 미수행. 대상 서버/유지보수 시간/화면 관찰 경로가 미정이다. 정식 v0.1.0으로 표시하지 않는다.
- repo: ubion-dev/project-ai-learning-analytics-setup (private). main은 계획 문서 최초 커밋, 개발은 codex/ala-v1.
- 로컬: 현재 이 파일을 포함한 repo. 모든 명령은 이 프로젝트 root를 workdir로 명시한다.
- 다음 행동: 실제 CPU/GPU 대상·유지보수 시간·화면 확인 경로를 정한 뒤 docs/ACCEPTANCE.md의 수용을 수행한다. 새 기능을 재구현하지 않는다.

## 확정 결정

AGENTS.md와 ala-v1-plan.md를 먼저 읽는다. Linux 기존 계정 재사용, DID는 로그인 없는 URL, 기존 브라우저 프로필은 보관만 하고 새 표시 프로필에 basic 저장 방식을 적용한다. 키링 자동 해제/암호 저장/웹 자동 인증은 만들지 않는다.
UI는 Textual 화살표형, root 시스템 변경과 일반 사용자 Chrome을 분리한다. 실제 서버 설치/재부팅은 아직 승인된 대상/시간이 없어 실행하지 않는다.

## 작업 표

| ID | 구현/자동 검증 | 실기기 수용 |
|---|---|---|
| ALA-00 | cf81793 계획/진행/AGENTS 먼저 커밋, private repo 생성 | 해당 없음 |
| ALA-01 | 9244598 승인 커밋에서 Rocky 폴더만 archive, 원본 85 tests 통과 | 미수행 |
| ALA-02 | efe9bed 공통 Engine, 26 모의 tests | 미수행 |
| ALA-03 | d60d2e7, 4d3a62a: CLI/root job/설치 묶음, installer 13 tests | SSH 종료 지속/실설치 미수행 |
| ALA-04 | 4d3a62a display 25 tests, GNOME/Chrome 복구 | GUI/재부팅/GPU 미수행 |
| ALA-05 | 81a5f34, TUI 15 headless tests | 실제 TTY sudo·SSH 렌더링 미수행 |
| ALA-06 | 81a5f34, Python3.12 CI 205 tests+31 subtests, RC 게시 | 정식 전 수용 필요 |
| ALA-07 | 수용 양식·이전 안내 Draft PR #11·RC 준비 완료 | CPU→GPU 전체 미수행 |

## 검증 증거

- 분리 원본: main 4a7128c6e7bd188e523faaf316a28eac0557008f (PR #10 merge), git archive scripts/rocky-linux.
- 초기 git show 실패는 원본 local fetch가 되지 않은 탓이었다. fetch 후 기준 커밋/경로 정상 확인. 기준 변경 없음.
- 분리 직후 unittest: 85 passed, 72.021s.
- 최종 전체 pytest(모의·headless·임시 설치): 204 passed + 31 subtests, 133.56s. 이후 RPC 연결 테스트 1개를 추가해 대상 테스트만 실행함.
- TUI headless 15개: 키보드 전체 설치 동선, 단일 확인, stale/미저장 구성, 권한 인증과 draft 보존, 느린 조회/종료, resize, job/log 경량 poll.
- Installer 13개: 실제 임시 venv+오프라인 wheel, 동일 버전 멱등성, 실패 시 current 유지, 변조/링크/lock/활성 job 거부, 원본 폴더 삭제 후 실행.
- Bash fence 46개 문법과 신규 문서 local link 8개 정상. git diff --check 통과.
- dist/ala-0.1.0rc1.tar.gz: SHA256 manifest·allow-list·압축 해제 후 --version/--help·examples/instance.json 확인.
- 실제 Rocky 설치/화면/팝업/재부팅/호스트 및 컨테이너 GPU는 검증하지 않았다.

## 구현 계약

- Config/config.py; CheckResult·Report/model.py; Host·Command·Paths/system.py.
- Engine(config,host,paths).inspect()/plan()/apply(on_stage) → Report; 콜백(stage_id,state).
- display.inspect_display/apply_display → list[CheckResult], chrome_action → dict.
- private entry: _session→session_start, _chrome-run→run_chrome, _chrome-user ACTION→user_action JSON+exit_code.
- Service.request 동기 / call 비동기 / authenticate sudo-v. async는 취소 가능한 subprocess RPC이며 systemd 작업은 분리됨.
- submit expected_config는 잠금 안에서 현재 저장 구성과 비교한다. 구성 파일 없는 apply는 사전 거부한다.
- 공용 잠금: /run/lock/ala/operation.lock. /run/lock의 sticky 권한과 ala 소유 디렉터리를 구분한다.
- systemctl 조회 오류는 interrupted로 단정하지 않고 query_warning, 확인된 not-found/inactive/부팅 변경만 중단 판정.
- 보고서의 프록시 인증정보는 redaction, URL credential query는 Config에서 거부.

## 개발 환경/분담

- Python: /tmp/ala-dev/bin/python (3.14). 운영/CI는 Python3.12. Textual8.2.8.
- wheelhouse: /tmp/ala-wheelhouse. 빌드: 해당 Python scripts/build-release.py --wheelhouse 해당경로.
- 모든 worker 변경 종료: ala_engine(engine+tests), ala_packaging(installer/builder/locks/CI/INSTALL+tests), ala_display(display+tests), ala_tui(tui+tests).
- root가 공통 계약·문서·통합·git/PR·릴리스를 관리한다. 기존 PTZ repo의 untracked .DS_Store/PDF 두 개는 건드리지 않는다.
- 테스트 코드가 변하지 않으면 통과 결과를 반복하지 말고 변경 범위만 추가 검증한다. 배포는 최신 runtime 코드로 다시 빌드한다.

## 압축·재개 절차

AGENTS.md → 계획 → 이 문서 → git status/diff/log를 읽고 현재 단계/남은 통과 조건/다음 행동을 보고한다.
완료 단계를 재구현하지 않는다. 최신 사용자 결정/코드와 다른 기록은 이유를 남겨 고친다. 구현/자동 검증/실기기 수용 완료를 구분한다.

## 원격 인계 결과

- 구현 Draft PR: https://github.com/ubion-dev/project-ai-learning-analytics-setup/pull/1
- 코드 기준: 81a5f347ba0732b1393e865afcf0a0ec7fa648bc (이후 문서 기록 갱신만).
- Python3.12 CI: https://github.com/ubion-dev/project-ai-learning-analytics-setup/actions/runs/40825661860
  205 passed + 31 subtests, 64.22s; Bash/lock 설치/후보 빌드/smoke도 성공.
- PR 이벤트 CI 40825729241 역시 성공. private visibility 확인 완료.
- RC: https://github.com/ubion-dev/project-ai-learning-analytics-setup/releases/tag/v0.1.0-rc.1
- 첨부: ala-0.1.0rc1.tar.gz + .sha256. 실제 CI가 만든 묶음을 사용했고 manifest/현재 runtime 파일과 대조했다.
- SHA256: 42a55614daa579f0b7c4dc464fb4980cda32f2e936b708f8367ba8f7b4b62396
- 기존 PTZ 이전 안내 Draft PR: [비공개 원본 이전 안내]
  codex/ala-migration, 8f3462e, 문서 두 개만 변경. 원본 실행 스크립트/사용자 untracked PDF는 보존.
- PR 자동 merge 및 정식 v0.1.0 발행은 하지 않았다. 실제 서버에 접속/설치/재부팅을 수행하지 않았다.
- 계획 압축/재개 파일은 AGENTS.md, docs/plans/ala-v1-plan.md, 이 문서다. 다음 세션은 이 세 파일과 git 상태를 읽고 ALA-07의 실기기 수용만 이어간다.

## 운영 기록 비공개 처리

개별 장비의 접속·식별·장애 조사 기록은 공개 이력에서 제외했다.

