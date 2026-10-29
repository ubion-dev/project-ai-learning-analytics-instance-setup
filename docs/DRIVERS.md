# 드라이버 자동 준비와 업데이트 정책

`ala setup`은 장치를 먼저 조사하고 필요한 공식 지원 패키지의 누락만 보완합니다.
이미 설치된 정상 드라이버를 교체하거나 관리 대상 패키지를 최신 버전으로 다시 설치하지 않습니다.

```bash
ala setup --plan
ala setup --apply
ala check
```

별도 드라이버 선택 옵션은 없습니다. 계획·검사는 읽기 전용이며 실제 설치는 `--apply`에서 수행합니다.

## 자동 준비 범위

Linux의 많은 장치 드라이버는 커널 모듈로 제공되고, 일부 장치는 별도 펌웨어 파일도 필요합니다.
Rocky의 커널 패키지는 기본·일반·추가 모듈로 나뉩니다. [RHEL 10 커널 패키지 설명](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html-single/managing_monitoring_and_updating_the_kernel/index)

| 조사 결과 | 처리 |
|---|---|
| PCI/USB 장치에 기존 드라이버 연결 | 연결을 유지하고 장치 ID·드라이버 정보를 보고 |
| 확인된 장치 유형의 공식 펌웨어 RPM 누락 | 해당 패키지만 Rocky 공식 저장소에서 설치 |
| 네트워크·그래픽 또는 명확한 USB 네트워크 장치의 드라이버 연결이 없고 모듈 RPM 누락 | 실행 커널과 정확히 같은 버전의 공식 모듈 보완 |
| NVIDIA GPU | 기존 NVIDIA 자동 감지 경로 사용, 정상 `nvidia-smi` 유지 |
| 미지원·모호한 장치, 필요한 저장소/권한 부재 | 차단 또는 확인 불가와 다음 조치 표시 |

PCI의 네트워크·저장장치·그래픽·오디오와 USB 인터페이스 정보를 조사합니다.
저장·오디오 및 일반 USB 복합 인터페이스는 연결이 없어도 용도가 확정되지 않았으므로 정보만 보고합니다.
USB 제조사 전용 클래스만 보고 Wi-Fi 장치라고 추정하지 않습니다.
NVIDIA가 없는 장비는 해당 없음으로 처리하며 GPU 패키지를 설치하지 않습니다.

펌웨어는 장치 유형과 제조사/드라이버가 확인된 매핑만 사용합니다.
예를 들어 Intel 무선, Realtek, Atheros, MediaTek용 펌웨어는 Rocky에서 별도 패키지로 제공합니다.
`linux-firmware` 하나만 설치했다고 모든 무선 장치를 지원하는 것은 아닙니다.
[Intel 무선 패키지](https://dl.rockylinux.org/pub/rocky/10/BaseOS/x86_64/os/Packages/i/), [Realtek 패키지](https://dl.rockylinux.org/pub/rocky/10/BaseOS/x86_64/os/Packages/r/), [Atheros 패키지](https://dl.rockylinux.org/pub/rocky/10/BaseOS/x86_64/os/Packages/a/), [MediaTek 패키지](https://dl.rockylinux.org/pub/rocky/10/BaseOS/x86_64/os/Packages/m/)

NVIDIA는 공식 지원 여부와 Secure Boot·기존 설치 상태를 확인합니다.
기존 고장 드라이버를 자동 제거하거나 서명 검사를 우회하지 않습니다. [NVIDIA Rocky Linux 설치 안내](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/rocky-linux.html)

## 연결 유지와 재부팅 대기

설치 도중 NetworkManager를 재시작하거나 Wi-Fi를 끄지 않습니다.
사용 중인 드라이버를 언로드·재바인딩하지 않으며 자동 재부팅하지 않습니다.
펌웨어·모듈을 새로 준비하면 현재 부팅 ID를 기록하고 재부팅 대기로 표시합니다.
같은 부팅에서 다시 실행해도 설치를 반복하지 않습니다. 재부팅 후에는 현재 장치 연결을 다시 조사합니다.
읽기 전용 검사는 기록을 삭제하지 않습니다. 이전 부팅의 대기 기록이 파일에 남아 있어도 현재 상태를 기준으로 판정합니다.

드라이버 연결이 있다는 결과는 장치가 실제로 통신·출력한다는 수용 증거가 아닙니다.
연결이 없다는 결과도 곧바로 하드웨어 고장을 뜻하지 않습니다. 모듈 제외 설정, 장치 할당, 펌웨어, 커널 지원 범위를 확인해야 합니다.
필요한 모듈 RPM이 현재 저장소에 없으면 커널을 임의 갱신하지 않고 중단합니다.
기기 ID와 로그를 바탕으로 유지보수 작업을 정합니다.
장치 지원을 확인하지 못해도 독립적인 셸·Docker·화면 정책 적용은 계속하고 최종 검사에 해당 결과를 남깁니다.
실제 RPM 설치 실패가 발생하면 이후 적용은 중단합니다.

## OS 업그레이드와의 관계

기본 구성은 DNF Automatic·PackageKit·GNOME의 자동 업데이트를 차단합니다.
`ala setup`은 전체 `dnf upgrade`를 실행하지 않습니다.
패키지 설치에는 새 부팅 커널과 Rocky release/repository/key 패키지를 제외하는 옵션을 적용합니다.
의존성 해결에 이 패키지들의 변경이 필요하면 우회 설치하지 않고 실패를 보고합니다.
정확한 실행 커널용 modules/devel RPM은 허용하며, 일반 라이브러리 등 필요한 의존성은 변경될 수 있습니다.
[DNF 패키지 제외 옵션](https://dnf.readthedocs.io/en/stable/command_ref.html)

이 보호는 ala가 수행하는 설치에 적용됩니다. 관리자 수동 DNF 명령, 별도 cron·관리 에이전트의 작업까지 막는 전역 동결은 아닙니다.
분리 전 Bash 스크립트의 `--upgrade`는 명시적 전체 업데이트 요청이며 기본값은 꺼짐입니다.
운영 중 자동 업데이트를 막아도 정기 점검에서 보안 패치·호환성 확인·재부팅 검증은 필요합니다.

## 자동 해결하지 않는 것

- 공식 커널이 지원하지 않는 장치용 임의 GitHub/DKMS 드라이버 검색 설치
- BIOS·장치 내장 펌웨어 플래시와 OS/커널 버전 자동 교체
- Wi-Fi SSID·암호 입력, 자동 연결 프로필·DNS·라우팅의 임의 변경
- 인터넷이 없는 서버의 패키지 다운로드 경로 자동 마련

장치용 펌웨어 파일을 설치하는 것과 BIOS/장치에 펌웨어를 플래시하는 것은 다른 작업입니다.
Wi-Fi가 연결됐지만 인터넷만 안 되는 경우에는 드라이버 교체 전에 네트워크 설정과 통신 경로를 확인합니다.
실제 CPU/GPU 서버의 드라이버·재부팅 검증 결과는 [실기기 수용 기록](ACCEPTANCE.md)에 별도로 남깁니다.
