"""Read-only network diagnosis. Package presence is not a connectivity verdict."""
from __future__ import annotations

import ipaddress
import json
import re

from .config import Config
from .hardware import _devices, _firmware, _KERNEL, _DRIVER
from .model import CheckResult, Report
from .system import Host, Paths

_WIRELESS = ('iwlwifi', 'iwl3945', 'iwl4965', 'ath5k', 'ath9k', 'ath10k', 'ath11k',
             'ath12k', 'brcmfmac', 'brcmsmac', 'rtw', 'rtl8', 'rt2', 'mt76', 'mt79')
_IFNAME = re.compile(r'[A-Za-z0-9_.:-]{1,15}\Z')


def _result(id, state, expected, actual, next_action='', detail=''):
    return CheckResult('network.' + id, state, expected, actual, next_action, detail)


def _run(host, args, timeout=10):
    # Stabilize command field names without changing process-wide environment.
    import os
    return host.run(args, timeout=timeout, env={**os.environ, 'LC_ALL': 'C'})


def _wireless_interfaces(paths):
    interfaces = {}
    root = paths.at('/sys/class/net')
    for entry in root.iterdir():
        if not _IFNAME.fullmatch(entry.name):
            continue
        if (entry / 'wireless').is_dir() or (entry / 'phy80211').exists():
            interfaces[entry.name] = (entry / 'device').resolve()
    return interfaces


def _modprobe_policy(host, paths, modules):
    results = []
    value = _run(host, ['modprobe', '--show-config'])
    if value.returncode:
        results.append(_result('module.policy', 'unknown', '모듈 제외 설정 조회', 'modprobe 조회 실패', 'kmod·접근 권한 확인'))
        return results
    exclusions = []
    for line in value.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {'blacklist', 'install'} and parts[1].replace('-', '_') in modules:
            # Do not return arbitrary install command text to logs/UI.
            exclusions.append({'module': parts[1], 'rule': parts[0]})
    try:
        cmdline = paths.at('/proc/cmdline').read_text()
        for field in cmdline.split():
            if field.startswith(('modprobe.blacklist=', 'rd.driver.blacklist=', 'module_blacklist=')):
                for module in field.split('=', 1)[1].split(','):
                    if module.replace('-', '_') in modules:
                        exclusions.append({'module': module, 'rule': field.split('=', 1)[0]})
    except OSError:
        results.append(_result('module.cmdline', 'unknown', '부팅 모듈 제외 설정 조회', '커널 명령행 읽기 실패'))
    results.append(_result('module.policy', 'blocked' if exclusions else 'ok', '대상 모듈 제외 없음', exclusions,
                           '모듈 제외 설정을 직접 검토하세요. 자동 변경하지 않습니다.' if exclusions else ''))
    return results


def _interface(host, name):
    prefix = 'interface.' + name
    fields = 'GENERAL.STATE,GENERAL.NM-MANAGED,GENERAL.CON-UUID,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.GATEWAY,IP6.DNS'
    value = _run(host, ['nmcli', '-t', '--escape', 'no', '-f', fields, 'device', 'show', name])
    if value.returncode:
        return [_result(prefix + '.manager', 'unknown', 'NetworkManager 장치 상태', '조회 실패', 'NetworkManager·nmcli·권한 확인')]
    data = {}
    for line in value.stdout.splitlines():
        if ':' in line:
            key, item = line.split(':', 1)
            data.setdefault(re.sub(r'\[\d+\]$', '', key), []).append(item)
    managed = data.get('GENERAL.NM-MANAGED', [''])[0]
    raw_state = data.get('GENERAL.STATE', [''])[0]
    match = re.match(r'(\d+)', raw_state)
    connected = bool(match and match[1] == '100')
    results = [_result(prefix + '.manager', 'ok' if managed == 'yes' else 'blocked' if managed == 'no' else 'unknown',
                       'NetworkManager 관리', managed or '조회 값 없음', '관리 제외 설정 확인' if managed == 'no' else '')]
    results.append(_result(prefix + '.connection', 'ok' if connected else 'not_applicable' if match and match[1] in {'20', '30'} else 'unknown' if not match else 'pending',
                           '현재 연결 상태', raw_state or '조회 값 없음',
                           '' if connected else '미연결은 드라이버 고장 판정이 아닙니다. 사용할 연결 프로필을 확인하세요.'))
    uuid = data.get('GENERAL.CON-UUID', [''])[0]
    if re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', uuid):
        auto = _run(host, ['nmcli', '-g', 'connection.autoconnect', 'connection', 'show', 'uuid', uuid])
        actual = auto.stdout.strip()
        results.append(_result(prefix + '.autoconnect', 'unknown' if auto.returncode or actual not in {'yes', 'no'} else 'ok' if actual == 'yes' else 'change_required',
                               '활성 프로필 자동 연결', actual if not auto.returncode else '조회 실패',
                               '기존 프로필의 자동 연결 설정을 검토하세요. 진단은 변경하지 않습니다.' if actual != 'yes' else ''))
    else:
        results.append(_result(prefix + '.autoconnect', 'not_applicable' if not connected else 'unknown',
                               '활성 프로필 자동 연결', '활성 프로필 UUID 없음', '프로필을 임의 선택하지 않습니다.'))
    addresses = [v for key in ('IP4.ADDRESS', 'IP6.ADDRESS') for v in data.get(key, []) if v]
    usable = []
    for address in addresses:
        try:
            parsed = ipaddress.ip_interface(address).ip
            if not parsed.is_link_local and not parsed.is_loopback and not parsed.is_unspecified:
                usable.append(address)
        except ValueError:
            pass
    state = 'ok' if usable else 'change_required' if connected else 'not_applicable'
    results.append(_result(prefix + '.address', state, '연결 시 사용 가능한 IP', addresses,
                           'DHCP·주소 할당 확인' if connected and not usable else ''))
    dns = [v for key in ('IP4.DNS', 'IP6.DNS') for v in data.get(key, []) if v]
    results.append(_result(prefix + '.dns', 'ok' if dns else 'change_required' if connected else 'not_applicable',
                           '연결 시 DNS 서버 설정', dns, 'DNS 설정 확인' if connected and not dns else '',
                           '설정 조회입니다. DNS 질의 성공을 의미하지 않습니다.'))
    routes = []
    route_unknown = False
    for family in ('-4', '-6'):
        route = _run(host, ['ip', '-j', family, 'route', 'show', 'dev', name])
        try:
            parsed = json.loads(route.stdout) if route.returncode == 0 else None
            if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
                raise ValueError('route')
            # Return only useful route fields, no unrelated daemon output.
            routes.extend({k: item[k] for k in ('dst', 'gateway', 'dev', 'metric') if k in item} for item in parsed)
        except (ValueError, TypeError):
            route_unknown = True
    results.append(_result(prefix + '.routes', 'unknown' if route_unknown else 'ok' if routes else 'change_required' if connected else 'not_applicable',
                           '인터페이스 경로 조회', routes, 'ip route·정책 라우팅 확인' if route_unknown or (connected and not routes) else '',
                           '기본 경로 부재만으로 로컬 네트워크 장애를 단정하지 않습니다.'))
    return results


def _profiles(host):
    """Read saved Wi-Fi profiles by UUID, including disconnected profiles; never ask secrets."""
    value = _run(host, ['nmcli', '-t', '--escape', 'no', '-f', 'UUID,TYPE', 'connection', 'show'])
    if value.returncode:
        return [_result('profiles', 'unknown', '기존 Wi-Fi 프로필 조사', 'nmcli 조회 실패')]
    results = []
    for line in value.stdout.splitlines():
        uuid, sep, kind = line.partition(':')
        if kind not in {'802-11-wireless', 'wifi'}:
            continue
        if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', uuid):
            results.append(_result('profiles', 'unknown', '유효한 프로필 UUID', '잘못된 응답'))
            continue
        auto = _run(host, ['nmcli', '-g', 'connection.autoconnect', 'connection', 'show', 'uuid', uuid])
        actual = auto.stdout.strip()
        state = 'unknown' if auto.returncode or actual not in {'yes', 'no'} else 'ok' if actual == 'yes' else 'not_applicable'
        results.append(_result('profile.' + uuid + '.autoconnect', state, '기존 프로필 자동 연결', actual if not auto.returncode else '조회 실패',
                               '사용할 프로필인지 확인한 뒤 자동 연결 설정을 검토하세요. 임의로 활성화하지 않습니다.' if state != 'ok' else '',
                               '저장된 프로필 목록입니다. 사용하지 않는 프로필의 자동 연결 꺼짐은 장애가 아닙니다. 활성 프로필은 인터페이스 검사에서 따로 판정합니다. 이름/SSID/비밀번호는 수집하지 않습니다.'))
    return results or [_result('profiles', 'not_applicable', '기존 Wi-Fi 프로필', '저장된 무선 프로필 없음')]


def _probe(host, url):
    # Same credential/query validation as DID URLs. No shell, no curlrc or redirects.
    Config(url=url)
    value = _run(host, ['curl', '--disable', '--silent', '--output', '/dev/null', '--write-out', '%{http_code}',
                        '--max-time', '5', '--connect-timeout', '5', '--proto', '=http,https', '--url', url], timeout=7)
    status = value.stdout.strip()
    ok = value.returncode == 0 and re.fullmatch(r'[1-5][0-9]{2}', status)
    return _result('host_probe', 'ok' if ok else 'unknown' if value.returncode in {124, 127} else 'error',
                   '호스트의 HTTP 응답 수신', {'http_status': status if re.fullmatch(r'\d{3}', status) else None,
                                                'returncode': value.returncode, 'scope': 'host'},
                   '' if ok else 'DNS·라우팅·TLS·대상 서비스 확인',
                   '호스트 기본 경로/프록시의 요청입니다. Wi-Fi 통신 성공이나 페이지 내용 정상 여부를 증명하지 않습니다.')


def inspect_network(host=None, paths=None, probe_url=None) -> Report:
    host, paths = host or Host(), paths or Paths()
    if probe_url is not None:
        Config(url=probe_url)  # Reject before any commands, including malformed URL/credentials.
    devices, inventory = _devices(paths)
    results = [_result(r.id.removeprefix('hardware.'), r.state, r.expected, r.actual, r.next_action) for r in inventory]
    try:
        interfaces = _wireless_interfaces(paths)
    except OSError:
        interfaces = {}
        results.append(_result('interfaces', 'unknown', '무선 인터페이스 조사', 'sysfs 조회 실패'))
    candidates = []
    for device in devices:
        path = paths.at(f'/sys/bus/{device.bus}/devices/{device.name}')
        names = [name for name, target in interfaces.items() if target == path.resolve()]
        if names or (device.driver and device.driver.startswith(_WIRELESS)) or (device.bus == 'pci' and device.kind.startswith('0280')):
            candidates.append((device, path, names))
    if not candidates and not interfaces:
        if not results:
            results.append(_result('wifi', 'not_applicable', '무선 장치', '확인된 무선 장치 없음',
                                   detail='미지원 제조사 전용 USB 인터페이스를 Wi-Fi로 추정하지 않습니다.'))
        if probe_url is not None:
            results.append(_probe(host, probe_url))
        return Report(results)

    kernel = _run(host, ['uname', '-r'])
    release = kernel.stdout.strip()
    valid_kernel = kernel.returncode == 0 and bool(_KERNEL.fullmatch(release))
    results.append(_result('kernel', 'ok' if valid_kernel else 'unknown', '실행 커널', release if valid_kernel else '조회 실패'))
    journal = _run(host, ['journalctl', '-k', '-b', '--no-pager', '-o', 'cat', '-n', '2000'])
    log_ok = journal.returncode == 0 and bool(journal.stdout.strip()) and journal.stdout.strip() != '-- No entries --'
    if not log_ok:
        results.append(_result('kernel_log', 'unknown', '현재 부팅 커널 로그', '조회 실패/로그 없음', 'journal 권한·보관 상태 확인'))
    modules = set()
    for device, path, names in candidates:
        prefix = 'device.' + device.bus + '.' + device.name
        results.append(_result(prefix, 'ok', '장치 식별', device.value,
                               detail='장치 또는 driver binding 확인은 무선 통신 정상 판정이 아닙니다.'))
        if device.override or device.driver in {'vfio-pci', 'vfio_pci', 'pci-stub', 'pci_stub'}:
            results.append(_result(prefix + '.override', 'blocked', '무선 드라이버 할당', device.override or device.driver,
                                   '장치 할당 의도를 확인하세요. 자동 재바인딩하지 않습니다.'))
        found = set()
        try:
            alias = (path / 'modalias').read_text().strip()
            if not re.fullmatch(r'[A-Za-z0-9_:*?.+-]{1,1024}', alias):
                raise ValueError('modalias')
            resolved = _run(host, ['modprobe', '--resolve-alias', alias])
            found = {m.replace('-', '_') for m in resolved.stdout.splitlines() if _DRIVER.fullmatch(m)} if resolved.returncode == 0 else set()
            state = 'ok' if found else 'unknown' if resolved.returncode in {124, 127} or re.search(r'permission|denied|not permitted', resolved.stderr, re.I) else 'blocked'
            results.append(_result(prefix + '.alias', state, '현재 커널 모듈 매핑', sorted(found),
                                   '실행 커널 모듈 패키지·지원 여부 확인' if not found else ''))
        except (OSError, ValueError):
            results.append(_result(prefix + '.alias', 'unknown', 'modalias 조회', '읽기 실패/잘못된 형식'))
        if device.driver and _DRIVER.fullmatch(device.driver):
            found.add(device.driver.replace('-', '_'))
        modules.update(found)
        for module in sorted(found):
            if valid_kernel:
                info = _run(host, ['modinfo', '-k', release, '-F', 'filename', module])
                state = 'ok' if info.returncode == 0 and info.stdout.strip() else 'unknown' if info.returncode in {124, 127} or re.search(r'permission|denied|not permitted', info.stderr, re.I) else 'blocked'
                results.append(_result(prefix + '.module.' + module, state, '실행 커널의 모듈 파일',
                                       info.stdout.strip() if state == 'ok' else '모듈 조회 실패', '실행 커널용 modules 패키지 확인' if state != 'ok' else ''))
        firmware = _firmware(device)
        for package in firmware:
            rpm = _run(host, ['rpm', '-q', package])
            state = 'ok' if rpm.returncode == 0 else 'change_required' if rpm.returncode == 1 and not rpm.stderr.strip() else 'unknown'
            results.append(_result(prefix + '.package.' + package, state, '펌웨어 RPM 설치', package if state == 'ok' else '누락' if state == 'change_required' else '조회 실패',
                                   'ala setup --plan으로 공식 누락 패키지 확인' if state == 'change_required' else '',
                                   '패키지 존재는 실제 펌웨어 로딩 성공을 증명하지 않습니다.'))
            if state == 'ok':
                integrity = _run(host, ['rpm', '-V', package])
                verification = 'ok' if integrity.returncode == 0 and not integrity.stdout.strip() else 'blocked' if integrity.returncode == 1 and integrity.stdout.strip() and not integrity.stderr.strip() else 'unknown'
                results.append(_result(prefix + '.integrity.' + package, verification, '설치된 펌웨어 RPM 파일 무결성',
                                       '일치' if verification == 'ok' else '불일치' if verification == 'blocked' else '확인 불가',
                                       '패키지 파일 손상·수정 여부를 검토하세요. 자동 재설치하지 않습니다.' if verification != 'ok' else ''))
        # Correlate with the device address, not all adapters sharing one module.
        lines = [line.lower() for line in journal.stdout.splitlines() if device.name.lower() in line.lower()] if log_ok else []
        timeout = any('failed' in line and '-110' in line for line in lines)
        fw_success = any('loaded firmware' in line or 'firmware version' in line for line in lines)
        fw_error = any(('firmware' in line and any(word in line for word in ('failed', 'not found', 'no suitable'))) for line in lines)
        initialized = bool(names)
        results.append(_result(prefix + '.initialization', 'ok' if initialized else 'blocked' if timeout else 'unknown',
                               '무선 인터페이스 생성', {'interfaces': names, 'initialization_timeout': timeout},
                               '' if initialized else '초기화 timeout(-110): 전원·펌웨어·커널 로그를 확인하세요. Windows 빠른 시작은 가능한 원인 중 하나입니다.' if timeout else '현재 부팅 로그와 장치 지원 상태 확인',
                               '이전 오류가 있어도 현재 인터페이스가 생성되면 초기화 복구로 판단합니다.'))
        results.append(_result(prefix + '.firmware', 'ok' if fw_success else 'blocked' if fw_error and not initialized else 'unknown' if not initialized else 'not_applicable',
                               '실제 펌웨어 로딩 증거', {'load_evidence': fw_success, 'failure_evidence': fw_error},
                               '설치된 드라이버를 반복 재설치하지 말고 현재 부팅 로그를 확인하세요.' if fw_error and not initialized else '',
                               '선택적 펌웨어 탐색 실패만으로 장애를 확정하지 않습니다. 현재 인터페이스/성공 로그를 함께 확인합니다.'))
    if modules:
        results.extend(_modprobe_policy(host, paths, modules))
    radio = _run(host, ['rfkill', '--json', '--output', 'TYPE,SOFT,HARD'])
    try:
        value = json.loads(radio.stdout) if radio.returncode == 0 else None
        if not isinstance(value, dict) or not isinstance(value.get('rfkilldevices'), list):
            raise ValueError('rfkill')
        wifi = [r for r in value['rfkilldevices'] if isinstance(r, dict) and r.get('type') in {'wlan', 'wifi'}]
        known = all(r.get('soft') in {'blocked', 'unblocked'} and r.get('hard') in {'blocked', 'unblocked'} for r in wifi)
        blocked = any(r.get('soft') == 'blocked' or r.get('hard') == 'blocked' for r in wifi)
        results.append(_result('rfkill', 'unknown' if not known else 'blocked' if blocked else 'ok' if wifi else 'not_applicable',
                               '무선 차단 없음', wifi, '물리 스위치/무선 차단 설정 확인. 자동 해제하지 않습니다.' if blocked else ''))
    except (ValueError, TypeError):
        results.append(_result('rfkill', 'unknown', '무선 차단 상태 조회', 'rfkill 조회 실패/잘못된 응답'))
    results.extend(_profiles(host))
    for name in sorted(interfaces):
        results.extend(_interface(host, name))
    if probe_url is not None:
        results.append(_probe(host, probe_url))
    return Report(results)
