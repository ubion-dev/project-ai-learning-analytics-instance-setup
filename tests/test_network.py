from pathlib import Path
import json
import ipaddress
import pytest

from ala.network import inspect_network
from ala.system import Command, Paths

KERNEL = '6.12.0-211.68.1.el10_2.x86_64'
UUID = '6046932e-dc29-450e-8ac1-bd00df1b5c85'

class FakeHost:
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.log = 'iwlwifi 0000:04:00.0: loaded firmware version 77.cc\n'
        self.state = '100 (connected)'
        self.managed = 'yes'
        self.auto = 'yes'
        self.address = str(ipaddress.IPv4Address((192 << 24) | (2 << 8) | 42)) + '/24'
        self.dns = '127.0.0.1'
        self.routes = [{'dst': 'default', 'gateway': '127.0.0.1', 'dev': 'wlp4s0'}]
        self.policy = ''
        self.rfkill = {'rfkilldevices': [{'type': 'wlan', 'soft': 'unblocked', 'hard': 'unblocked'}]}
    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        key = tuple(argv)
        if key in self.overrides:
            return self.overrides[key]
        if argv == ['uname', '-r']: return Command(0, KERNEL)
        if argv[:2] == ['modprobe', '--resolve-alias']: return Command(0, 'iwlwifi\n')
        if argv == ['modprobe', '--show-config']: return Command(0, self.policy)
        if argv[0] == 'modinfo': return Command(0, '/lib/modules/' + KERNEL + '/kernel/drivers/net/wireless/iwlwifi.ko.xz')
        if argv[:2] == ['rpm', '-q']: return Command(0, argv[-1])
        if argv[:2] == ['rpm', '-V']: return Command(0)
        if argv[0] == 'journalctl': return Command(0, self.log)
        if argv[0] == 'rfkill': return Command(0, json.dumps(self.rfkill))
        if argv[0] == 'nmcli':
            if argv[-2:] == ['connection', 'show']:
                return Command(0, UUID + ':802-11-wireless\n')
            if argv[1:3] == ['-g', 'connection.autoconnect']: return Command(0, self.auto)
            # Fixed NetworkManager API fields, independent of implementation constants.
            # https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli-examples.html
            documented = {'GENERAL.STATE', 'GENERAL.NM-MANAGED', 'GENERAL.CON-UUID',
                          'IP4.ADDRESS', 'IP4.GATEWAY', 'IP4.DNS',
                          'IP6.ADDRESS', 'IP6.GATEWAY', 'IP6.DNS'}
            if '-f' not in argv or not set(argv[argv.index('-f') + 1].split(',')) <= documented:
                return Command(2, '', 'invalid field')
            return Command(0, '\n'.join(['GENERAL.STATE:' + self.state, 'GENERAL.NM-MANAGED:' + self.managed,
                                         'GENERAL.CON-UUID:' + (UUID if self.state.startswith('100') else '--'),
                                         'IP4.ADDRESS[1]:' + self.address, 'IP4.DNS[1]:' + self.dns]))
        if argv[0] == 'ip': return Command(0, json.dumps(self.routes if '-4' in argv else []))
        if argv[0] == 'curl': return Command(0, '200')
        return Command(127, '', 'unexpected command')
    def execute(self, argv, **kwargs):
        raise AssertionError('diagnostics must never mutate')

@pytest.fixture
def env(tmp_path):
    paths = Paths(tmp_path)
    for path in ('/sys/bus/pci/devices', '/sys/bus/usb/devices', '/sys/class/net'):
        paths.at(path).mkdir(parents=True)
    paths.at('/proc').mkdir()
    paths.at('/proc/cmdline').write_text('quiet')
    return FakeHost(), paths

def device(paths, interface=True, driver=True):
    p = paths.at('/sys/bus/pci/devices/0000:04:00.0')
    p.mkdir()
    for name, value in {'vendor': '0x8086', 'device': '0x2723', 'class': '0x028000',
                        'modalias': 'pci:v00008086d00002723sv00008086sd00000080bc02sc80i00'}.items():
        (p / name).write_text(value)
    if driver:
        (p / 'driver').symlink_to('../../drivers/iwlwifi')
    if interface:
        net = paths.at('/sys/class/net/wlp4s0')
        (net / 'wireless').mkdir(parents=True)
        (net / 'device').symlink_to(p)
    return p

def get(report, suffix):
    return next(r for r in report.results if r.id.endswith(suffix))

def inspect(env, **kwargs):
    return inspect_network(host=env[0], paths=env[1], **kwargs)

def test_no_wifi_is_na_without_commands(env):
    r = inspect(env)
    assert get(r, 'wifi').state == 'not_applicable'
    assert not env[0].calls

def test_missing_inventory_is_unknown(env):
    env[1].at('/sys/bus/pci/devices').rmdir()
    assert get(inspect(env), 'inventory.pci').state == 'unknown'

def test_normal_wifi_separates_layers(env):
    device(env[1])
    r = inspect(env)
    assert r.exit_code == 0
    for suffix in ('.initialization', '.firmware', '.manager', '.connection', '.address', '.dns', '.routes', '.autoconnect'):
        assert get(r, suffix).state == 'ok'
    assert not any(call[0][0] == 'curl' for call in env[0].calls)

def test_timeout_existing_packages_is_not_install_request(env):
    device(env[1], interface=False)
    env[0].log = 'iwlwifi 0000:04:00.0: probe failed with error -110'
    r = inspect(env)
    result = get(r, '.initialization')
    assert result.state == 'blocked'
    assert '가능한 원인' in result.next_action
    assert not any(item.state == 'change_required' and '.package.' in item.id for item in r.results)

def test_firmware_fallback_and_recovered_init_not_failure(env):
    device(env[1])
    env[0].log = ('iwlwifi 0000:04:00.0: Direct firmware load failed -2\n'
                  'iwlwifi 0000:04:00.0: probe failed with error -110\n'
                  'iwlwifi 0000:04:00.0: loaded firmware version 77.cc')
    r = inspect(env)
    assert get(r, '.initialization').state == 'ok'
    assert get(r, '.firmware').state == 'ok'

def test_optional_firmware_error_with_interface_not_blocked(env):
    device(env[1])
    env[0].log = 'iwlwifi 0000:04:00.0: Direct firmware load failed -2'
    assert get(inspect(env), '.firmware').state == 'not_applicable'

def test_real_missing_firmware_and_no_interface_blocked(env):
    device(env[1], interface=False)
    env[0].log = 'iwlwifi 0000:04:00.0: no suitable firmware found'
    assert get(inspect(env), '.firmware').state == 'blocked'

def test_firmware_package_missing_plan_points_to_setup(env):
    device(env[1])
    env[0].overrides[('rpm', '-q', 'iwlwifi-mvm-firmware')] = Command(1)
    r = get(inspect(env), '.package.iwlwifi-mvm-firmware')
    assert r.state == 'change_required' and 'ala setup' in r.next_action

def test_command_permission_errors_unknown(env):
    device(env[1])
    env[0].overrides[('rpm', '-q', 'iwlwifi-mvm-firmware')] = Command(1, '', 'rpmdb permission denied')
    assert get(inspect(env), '.package.iwlwifi-mvm-firmware').state == 'unknown'

def test_kernel_log_missing_unknown(env):
    device(env[1])
    env[0].log = ''
    assert get(inspect(env), 'kernel_log').state == 'unknown'

def test_rfkill_block_does_not_unblock(env):
    device(env[1])
    env[0].rfkill['rfkilldevices'][0]['hard'] = 'blocked'
    assert get(inspect(env), 'rfkill').state == 'blocked'
    assert not any('unblock' in call[0] for call in env[0].calls)

def test_blacklist_and_override_are_preserved(env):
    p = device(env[1])
    (p / 'driver_override').write_text('vfio-pci')
    env[0].policy = 'blacklist iwlwifi\ninstall unrelated /sensitive/password\n'
    r = inspect(env)
    assert get(r, '.override').state == 'blocked'
    assert get(r, 'module.policy').state == 'blocked'
    assert '/sensitive/' not in json.dumps(r.to_dict())

def test_kernel_cmdline_blacklist(env):
    device(env[1])
    env[1].at('/proc/cmdline').write_text('rd.driver.blacklist=iwlwifi')
    assert get(inspect(env), 'module.policy').state == 'blocked'

def test_disconnected_profile_is_diagnosed_without_selection(env):
    device(env[1])
    env[0].state, env[0].auto, env[0].address, env[0].dns, env[0].routes = '30 (disconnected)', 'no', '', '', []
    r = inspect(env)
    assert get(r, '.connection').state == 'not_applicable'
    assert get(r, 'profile.' + UUID + '.autoconnect').state == 'not_applicable'
    assert get(r, '.initialization').state == 'ok'
    assert get(r, '.address').state == 'not_applicable'

def test_unmanaged_network(env):
    device(env[1])
    env[0].managed = 'no'
    assert get(inspect(env), '.manager').state == 'blocked'

def test_no_address_and_dns_are_distinct(env):
    device(env[1])
    env[0].address, env[0].dns = str(ipaddress.IPv6Address((0xfe80 << 112) | 0x1234)) + '/64', ''
    r = inspect(env)
    assert get(r, '.address').state == 'change_required'
    assert get(r, '.dns').state == 'change_required'
    assert get(r, '.initialization').state == 'ok'

def test_probe_success_is_host_only_even_when_wifi_failed(env):
    device(env[1], interface=False)
    env[0].log = 'iwlwifi 0000:04:00.0: probe failed with error -110'
    r = inspect(env, probe_url='https://example.com')
    probe = get(r, 'host_probe')
    assert probe.state == 'ok' and probe.actual['scope'] == 'host'
    assert r.exit_code == 1
    args, kwargs = next(call for call in env[0].calls if call[0][0] == 'curl')
    assert '--max-time' in args and '--disable' in args
    assert '--insecure' not in args and '--location' not in args
    assert kwargs['timeout'] == 7

@pytest.mark.parametrize('url', ['https://user:secret@example.com', 'file:///etc/passwd', 'https://example.com/?token=secret', 'https://example.com/\n--insecure'])
def test_probe_rejects_credentials_before_commands(env, url):
    with pytest.raises(ValueError):
        inspect(env, probe_url=url)
    assert env[0].calls == []

def test_readonly_keeps_files_and_mtimes(env):
    device(env[1])
    def snapshot():
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in env[1].root.rglob('*') if p.is_file()}
    before = snapshot()
    inspect(env)
    inspect(env)
    assert snapshot() == before
    assert all(call[0][0] not in {'dnf', 'systemctl'} for call in env[0].calls)
    assert all('--show-secrets' not in call[0] for call in env[0].calls)

def test_unsupported_usb_vendor_class_not_guessed(env):
    base = env[1].at('/sys/bus/usb/devices')
    (base / '1-2').mkdir()
    (base / '1-2/idVendor').write_text('0bda')
    (base / '1-2/idProduct').write_text('1234')
    interface = base / '1-2:1.0'
    interface.mkdir()
    for name, value in {'bInterfaceClass': 'ff', 'bInterfaceSubClass': '00', 'bInterfaceProtocol': '00'}.items():
        (interface / name).write_text(value)
    assert get(inspect(env), 'wifi').state == 'not_applicable'

def test_module_query_unavailable_is_unknown(env):
    p = device(env[1])
    alias = (p / 'modalias').read_text()
    env[0].overrides[('modprobe', '--resolve-alias', alias)] = Command(127)
    assert get(inspect(env), '.alias').state == 'unknown'

def test_unbound_and_module_absent_reports_blocked(env):
    p = device(env[1], interface=False, driver=False)
    env[0].overrides[('modprobe', '--resolve-alias', (p / 'modalias').read_text())] = Command(1)
    assert get(inspect(env), '.alias').state == 'blocked'


def test_firmware_file_corruption_is_blocked_not_reinstall(env):
    device(env[1])
    env[0].overrides[('rpm', '-V', 'iwlwifi-mvm-firmware')] = Command(1, 'S.5....T. /usr/lib/firmware/iwlwifi-cc-a0-77.ucode')
    r = inspect(env)
    assert get(r, '.integrity.iwlwifi-mvm-firmware').state == 'blocked'
    assert get(r, '.package.iwlwifi-mvm-firmware').state == 'ok'
    assert not any('reinstall' in call[0] for call in env[0].calls)


def test_firmware_integrity_permission_failure_unknown(env):
    device(env[1])
    env[0].overrides[('rpm', '-V', 'iwlwifi-mvm-firmware')] = Command(1, '', 'permission denied')
    assert get(inspect(env), '.integrity.iwlwifi-mvm-firmware').state == 'unknown'


def test_error_on_other_adapter_not_misattributed(env):
    device(env[1], interface=False)
    env[0].log = 'iwlwifi 0000:05:00.0: probe failed with error -110'
    result = get(inspect(env), '.initialization')
    assert result.state == 'unknown' and not result.actual['initialization_timeout']


def test_route_query_failure_unknown(env):
    device(env[1])
    env[0].overrides[('ip', '-j', '-4', 'route', 'show', 'dev', 'wlp4s0')] = Command(127)
    assert get(inspect(env), '.routes').state == 'unknown'


def test_active_profile_autoconnect_off_is_actionable(env):
    device(env[1])
    env[0].auto = 'no'
    assert get(inspect(env), 'interface.wlp4s0.autoconnect').state == 'change_required'


def test_modinfo_permission_is_unknown(env):
    device(env[1])
    env[0].overrides[('modinfo', '-k', KERNEL, '-F', 'filename', 'iwlwifi')] = Command(1, '', 'Permission denied')
    assert get(inspect(env), '.module.iwlwifi').state == 'unknown'


def test_nmcli_uses_documented_nm_managed_field(env):
    # The official nmcli device example names this GENERAL.NM-MANAGED, not MANAGED.
    device(env[1])
    report = inspect(env)
    argv = next(args for args, _ in env[0].calls if args[0] == 'nmcli' and args[-3:] == ['device', 'show', 'wlp4s0'])
    fields = argv[argv.index('-f') + 1].split(',')
    assert 'GENERAL.NM-MANAGED' in fields
    assert 'GENERAL.MANAGED' not in fields
    assert get(report, '.manager').state == 'ok'


def test_nmcli_invalid_fields_are_rejected_by_fixture(env):
    # Ensure a future misspelling cannot silently pass through the mock.
    result = env[0].run(['nmcli', '-t', '-f', 'GENERAL.MANAGED', 'device', 'show', 'wlp4s0'])
    assert result.returncode == 2
