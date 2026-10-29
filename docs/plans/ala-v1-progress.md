# ala v1 진행 기록

## 현재 상태 — 재개할 때 여기부터

- ALA-09~12 구현·최종 Python3.12 CI·RC3 게시 완료. 기준 main 773ebb5(PR #2 병합), 브랜치 codex/ala-initial-prepare.
- doctor network 읽기 전용 계층 진단, prepare 최초 OS 업데이트/수동 재부팅 검증, 기존 GDM 자동 로그인·실제 세션 구분을 추가했다.
- 기존 setup은 전체 OS 업데이트를 하지 않는다. prepare 완료후 upgrade 반복 없음. 모든 재부팅은 명시적 사용자 작업.
- ALA-00~08/RC2 기존 구현·검증은 재사용한다. RC2를 덮어쓰지 않는다.
- 실기기 수용 ALA-07 및 RC3 추가 수용 미수행. 접속 장비 식별/호스트키 확인과 실제 화면 관찰이 남아 있다. 유지보수 시간 허락을 다시 묻지 않는다.
- 다음: Draft PR #3 검토 및 장비 식별/호스트키·화면 확인 경로 확보 후 CPU→GPU 실기기 수용. 실제 장비 수용 전 정식 v0.1.0으로 표시하지 않는다.

## 확정 결정

AGENTS.md와 ala-v1-plan.md를 먼저 읽는다. Linux 기존 계정 재사용, DID는 로그인 없는 URL, 기존 브라우저 프로필은 보관만 하고 새 표시 프로필에 basic 저장 방식을 적용한다. 키링 자동 해제/암호 저장/웹 자동 인증은 만들지 않는다.
UI는 Textual 화살표형, root 시스템 변경과 일반 사용자 Chrome을 분리한다. 실제 서버 설치/재부팅은 사용자 승인 범위 안에 있으며, 현재는 SSH 기기 키 확인을 기다리고 있다.

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
| ALA-08 | f9a8546, Python3.12 253 tests+40 subtests, RC2 게시, Draft PR #2 | 실제 DNF solver/장치·재부팅 미수행 |

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

- 구현 Draft PR: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/1
- 코드 기준: 81a5f347ba0732b1393e865afcf0a0ec7fa648bc (이후 문서 기록 갱신만).
- Python3.12 CI: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/actions/runs/40825661860
  205 passed + 31 subtests, 64.22s; Bash/lock 설치/후보 빌드/smoke도 성공.
- PR 이벤트 CI 40825729241 역시 성공. private visibility 확인 완료.
- RC: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.1
- 첨부: ala-0.1.0rc1.tar.gz + .sha256. 실제 CI가 만든 묶음을 사용했고 manifest/현재 runtime 파일과 대조했다.
- SHA256: 42a55614daa579f0b7c4dc464fb4980cda32f2e936b708f8367ba8f7b4b62396
- 기존 PTZ 이전 안내 Draft PR: [비공개 원본 이전 안내]
  codex/ala-migration, 8f3462e, 문서 두 개만 변경. 원본 실행 스크립트/사용자 untracked PDF는 보존.
- PR 자동 merge 및 정식 v0.1.0 발행은 하지 않았다. 실제 서버에 접속/설치/재부팅을 수행하지 않았다.
- 계획 압축/재개 파일은 AGENTS.md, docs/plans/ala-v1-plan.md, 이 문서다. 다음 세션은 이 세 파일과 git 상태를 읽고 ALA-07의 실기기 수용만 이어간다.

## 운영 기록 비공개 처리

개별 장비의 접속·식별·장애 조사 기록은 공개 이력에서 제외했다.

## 운영 기록 비공개 처리

개별 장비의 접속·식별·장애 조사 기록은 공개 이력에서 제외했다.

## 사용자 최신 우선순위: setup.sh 코드 감사

- 사용자 요청에 따라 원격 진단보다 setup.sh와 호출 하위 스크립트 코드 확인을 우선했다. 추가 SSH 시도는 하지 않는다.
- setup.sh 전체·common·base·ssh·docker·display·update·GPU 모듈 확인. Wi-Fi off/rfkill/NetworkManager stop·disable·mask/connection down·modify/autoconnect 해제/직접 DNS·gateway 변경 명령 없음.
- nmcli는 setup-ssh.sh 39/41행의 현재 연결/zone 조회 두 곳뿐이다. ip route 명령도 조회만 한다.
- 실제 네트워크 관련 변경은 firewalld 시작·SSH 포트 허용, Docker 설치·서비스 기동이다. Docker 네트워크/방화벽 간접 영향은 실제서버조회전 배제할 수 없다. --upgrade 명시시만 전체 dnf upgrade.
- Wi-Fi 부팅 자동 연결을 켜거나 검증하는 로직도 현재는 없다. 새기능을 추측 추가하지 않고 이 결과부터 사용자에게 전달한다.
- fixture 수정 후 GitHub Python3.12 CI는 207 passed+34 subtests(64.19s)로 두 실행 모두 통과했다. docs-only 감사 기록에는 테스트를 재실행할 필요가 없다.

## 최신 작업: 레포지토리 이름 변경

- 사용자 승인으로 GitHub 정식 이름을 `ubion-dev/project-ai-learning-analytics-instance-setup`으로 변경했다.
- 레포 ID `R_kgDOVbW9rw`, 비공개 설정, PR #1, RC v0.1.0-rc.1 및 첨부 해시가 유지됨을 확인했다.
- 로컬 디렉터리는 현재 workspace의 `project-ai-learning-analytics-instance-setup`, origin도 새 URL로 갱신했다.
- CLI `ala`, Python 배포 식별자, 설치·서비스·프로필 경로와 런타임 코드는 변경하지 않는다.
- 설치 안내·계획·진행·기존 PTZ 이전 안내 및 PR/RC 설명의 정식 URL을 갱신한다. 이미 게시된 RC 묶음은 재작성하지 않는다.
- 이름/문서 변경이므로 기존 통과 테스트를 재실행하지 않고 diff와 링크·메타데이터만 검증한다.
- 네트워크 문제에 대해서는 원격 변경을 수행하지 않았다. 직전 사용자의 코드 감사 요청 결과와 접속 제한은 위 기록을 따른다.

## ALA-08 시작 — 공식 드라이버 자동 준비

- 사용자 요청: 완료 상태, OS 업그레이드 차단 여부 확인 및 필요한 드라이버의 초기 자동 설치.
- 현재는 DNF Automatic/PackageKit/GNOME 자동 업데이트 차단, NVIDIA 자동 감지가 구현되어 있다. 일반 dnf install 의존성에 의한 커널/release 변경은 별도 보호가 없어 보완한다.
- 범위: PCI/USB 조사와 공식 펌웨어 누락 보완, 실행 커널 버전의 공식 모듈만 준비. 기존 정상 드라이버와 연결을 유지하고 자동 재부팅/모듈 언로드/임의 드라이버 다운로드는 하지 않는다.
- 계획 변경 이유와 완료 기준은 ala-v1-plan.md의 ALA-08에 기록했다. 코드는 RC2로 올리고 RC1 자산은 보존한다.
- 실기기 설치/재부팅은 여전히 미수행. SSH 대상의 호스트키 확인 문제가 해결되지 않았으므로 원격 변경 없이 모의 검증부터 진행한다.
- 코드 검토 보완: 사용하지 않는 오디오/보조 저장장치나 USB 복합 인터페이스의 binding 부재를 필수 실패로 만들면 정상 초기 설치가 막힌다. 따라서 이들은 정보 조사만 하고 generic 모듈 보완은 네트워크·그래픽/명확한 USB 네트워크로 제한한다. 장치 지원 진단은 최종 결과에 남기되 독립 설정은 계속하고, 실제 패키지 설치 실패는 중단한다.
- GitHub 조회 중 기존 PR #1의 MERGED 상태를 확인했다. origin/main 659d97e는 기존 개발 HEAD와 동일한 코드 트리여서 fast-forward로 반영하고 codex/ala-hardware-preparation으로 새 브랜치를 만들었다. 이번 변경은 별도 PR로 제출한다.
- 구현: ala/hardware.py 공통 조사/적용, Engine 기본 hardware 단계, 공식 Rocky 펌웨어 매핑, 실행 커널 정확 일치 모듈, 재부팅 기록과 readonly/재실행 유지. USB CDC 직렬·Bluetooth·허브/오디오/보조 저장장치 오탐을 제외했다.
- DNF 보호: 공통 Bash helper, Chrome RPM 직접 설치, installer Python/pip bootstrap에 새 부팅 kernel/kernel-core/debug/UKI 및 Rocky release/repo/key 제외·weak deps off를 적용했다. 기존 명시 --upgrade는 기본 off로 유지한다. kmod 기본 설치/검사를 맞췄다.
- 로컬 Python3.14 전체 pytest: 251 passed + 40 subtests, 115.04s. 이후 리뷰에서 설치 중 새 USB 진단이 생겨도 독립 설정을 계속하는 회귀 1개를 추가하고 Engine+hardware 65 passed + 6 subtests를 확인했다. 현재 전체 수집 항목은 252개다.
- 추가 검증: common/Chrome/NVIDIA/installer 45개, common/verify/legacy 47개+30 subtests, 하드웨어 모듈 33개+3 subtests, 수정 Shell 5개 bash -n 통과. git diff --check, local 문서 링크, RC2 묶음 manifest/모듈·DRIVERS문서 포함/--version·--help smoke 통과.
- RC2 로컬 후보는 dist/ala-0.1.0rc2.tar.gz다. GitHub Python3.12 CI와 PR 제출이 다음 행동이다. 기존 RC1을 덮어쓰지 않는다.
- 실제 Rocky DNF solver에서 기존 동일 버전 커널 의존성 충족, 물리 장치 설치·통신·GPU·전체화면·재부팅 수용은 미검증이다. 테스트 통과를 실장비 보장으로 해석하지 않는다.
- 최종 리뷰에서 ECM+ACM 복합 USB의 모든 data class를 네트워크로 추정하는 오탐을 재현했다. USB union/IAD를 해석하지 않는 v1에서는 제어 인터페이스나 실제 네트워크 driver 증거만 사용하도록 수정했다. 하드웨어 34개+3 subtests, 최종 Engine+hardware 66개+6 subtests 통과. 현재 전체 수집은 253개다.
- 최종 기능 커밋: f9a85466c022a11e38848e50a1b50167dc15c527. Draft PR #2: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/2 (자동 병합하지 않음).
- Python3.12 CI: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/actions/runs/40842796588 — 253 passed + 40 subtests, 48.78s. Bash 문법·고정 wheel·후보 빌드·CLI smoke 성공.
- CI 산출물의 전체 manifest/현재 runtime 소스 일치를 확인하고 별도 RC2에 게시했다: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.2
- RC2 SHA256: 27c1c5083f37f7188164d1f5266e9b3d475a2c010f81a72b6c126661c2fcd3ab. 기존 RC1 자산은 변경하지 않았다.
- 남은 조건은 ALA-07 실기기 수용과 이번 장치/커널 보존 수용이다. 실제 서버에 설정·설치·재부팅을 수행하지 않았다.


## ALA-09~12 시작 (2026-10-29)
사용자 승인 계획을 먼저 저장했다. main 773ebb5, PR #2 병합 확인. 브랜치 codex/ala-initial-prepare.
현재 구현 전. network/prepare/display를 분리하고 공통 인터페이스는 계획의 추가 계약을 따른다.
기존 RC2 검증은 재사용. 새 변경은 RC3로 검증. 실기기 접속 키/화면 관찰 미확인으로 실기기 수용 미수행.


## ALA-09~12 구현·로컬 검증

- 계획 먼저 저장/커밋 9031031. network/prepare/display 영역별 분담, root가 jobs/service/CLI/TUI/engine/문서 통합.
- network: 현재 장치·modalias·모듈·펌웨어 RPM 무결성·현재 부팅 초기화·rfkill·NM 프로필/IP/route/DNS를 분리. 외부 probe 명시 옵션만 사용하고 host scope로 표시. inactive 프로필 autoconnect=no는 정보 상태.
- prepare: operation='prepare' 고정 worker, 기존 single lock/record 재사용. 별도 root0600 prepare.json에 RPM 전후·트랜잭션/boot/예상 커널 기록. 공식 repo HTTPS/서명키 패키지, 외부 모듈/공간/실행작업 검사. 임시 plan metadata. 완료·같은boot·verifying 재시도에서 전체 업데이트 반복하지 않음.
- display: configuration/session/lock/GDM 진단/visual acceptance 분리. URL 없거나 autostart off에서도 자동 로그인 준비. 실제 화면 수용은 자동 검사 범위 밖.
- review 수정: 업데이트후 정책 drift를 완료로 숨기는 checkpoint 순서 수정, verifying의 TUI 재시도 허용, 공식 nmcli 필드 GENERAL.NM-MANAGED 검증 및 mock allowlist 강화.
- 공식 Rocky10.2 rocky-repos/rocky-gpg-keys RPM에서 repo HTTPS/gpgcheck/키 경로/패키지 소유를 확인. 실제 DNF transaction 또는 실제 장비 검증을 수행한 것은 아님.
- 로컬 전체 335 passed+40 subtests(123.11s) 이후 리뷰 회귀13개 추가. 최종 수집348개.
- 변경범위 최종 prepare/network/TUI: 96 passed(10.96s). 앞선 engine/jobs/display/network:118 passed+3subtests. prepare44/network33/display35 개별통과.
- 빌드: Python3.14 고정 wheelhouse로 dist/ala-0.1.0rc3.tar.gz 생성. diff --check/entry help/installer bash 문법 정상. 최종 Python3.12 CI 및 후보 manifest 확인이 남음.
- 최종 수용은 docs/ACCEPTANCE.md의 RC3 항목을 CPU→GPU 순서로 수행. 새 원격 로그인·설치·재부팅은 이 작업에서 수행하지 않았다.


## RC3 최종 검증 및 인계

- 기능 commit: 4bc033b762e061555abad30b9e17ef0c0744e475. PR #3: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/pull/3 (Draft, 자동 병합 안 함).
- Python3.12 CI: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/actions/runs/40890011688 — 348 passed + 40 subtests, 62.98s. Bash 문법/고정 wheel 후보 빌드/CLI smoke 성공.
- 로컬 변경범위 최종96 tests 통과. 배포의51개 manifest hash 및 각 source 일치, CI archive와 local archive byte 일치, 문서 local links 정상.
- RC3: https://github.com/ubion-dev/project-ai-learning-analytics-instance-setup/releases/tag/v0.1.0-rc.3
- SHA256: 66e3c00e4e468a39a08b384ca96669774b80f081f4f3229bbf1eaa31d4d94935. CI 산출물 사용. RC1/RC2 변경 없음.
- GitHub connector는 이 private repo에서404여서 기존 인증 gh로 PR/CI/후보를 처리했다.
- 실제 Rocky의 DNF transaction·CPU/GPU 드라이버·자동 로그인·재부팅·화면 수용 미수행. 서버 키 검증 우회나 원격 설정 변경 없이 구현/자동검증 단계만 완료했다.
