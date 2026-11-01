from dataclasses import replace
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ala.config import Config
from ala.engine import UPDATE_UNITS
from ala.prepare import Prepare, MARKER, RPM_QUERY, DNF_OPTIONS
from ala.system import Command, Paths, Host

OLD = '6.12.0-211.16.1.el10_2.x86_64'
NEW = '6.12.0-211.68.1.el10_2.x86_64'
BOOT = '9a692999-86db-4415-82d6-8e176e20b1b7'
NEXT = '4c7f3da1-bf14-4a28-ad47-b933a26821a4'


def kernel_row(kernel):
    version, rest = kernel.split('-', 1)
    release, arch = rest.rsplit('.', 1)
    return '\t'.join(('kernel-core', '0', version, release, arch))


class FakeHost:
    def __init__(self):
        self.rows = [kernel_row(OLD), 'bash\t0\t5.2\t1.el10\tx86_64']
        self.kernel = OLD
        self.default = OLD
        self.calls, self.executed = [], []
        self.replies = {}
        self.updates = True
        self.upgrade_code = 0
        self.policy_code = 0
        self.policy_ready = True
        self.change = True
        self.crash = False
        self.partial = False

    def run(self, argv, **kwargs):
        argv = tuple(argv)
        self.calls.append((argv, kwargs))
        if argv in self.replies:
            return self.replies[argv]
        if argv == RPM_QUERY:
            return Command(0, '\n'.join(self.rows) + '\n')
        if argv[:2] == ('rpm', '-qf'):
            return Command(0, 'rocky-gpg-keys\n' if '/rpm-gpg/' in argv[-1] else 'rocky-repos\n')
        if argv[:2] == ('rpm', '-Vf'):
            return Command(0)
        if argv == ('uname', '-r'):
            return Command(0, self.kernel + '\n')
        if argv == ('grubby', '--default-kernel'):
            return Command(0, '/boot/vmlinuz-' + self.default + '\n')
        if argv[:2] == ('pgrep', '-x'):
            return Command(1)
        if argv[:2] == ('systemctl', 'is-enabled'):
            return Command(1, 'masked\n') if self.policy_ready else Command(0, 'enabled\n')
        if argv[:2] == ('systemctl', 'is-active'):
            return Command(3, 'inactive\n')
        if argv[0] == 'dnf' and 'check-update' in argv:
            return Command(100 if self.updates else 0, 'kernel-core.x86_64 6.12.0-211.68.1.el10_2 baseos\n' if self.updates else '')
        if argv[0] == 'dnf' and 'history' in argv:
            return Command(0, 'Transaction ID : 12\nReturn-Code    : Success\n')
        return Command(127, '', 'missing command: ' + ' '.join(argv))

    def execute(self, argv, **kwargs):
        self.executed.append(tuple(argv))
        if argv[0] == 'bash':
            if not self.policy_code:
                self.policy_ready = True
            return self.policy_code
        if self.change:
            self.rows.append(kernel_row(NEW))
            self.default = NEW
        if self.crash:
            raise KeyboardInterrupt('simulated power interruption')
        return self.upgrade_code


@pytest.fixture
def setup(tmp_path):
    paths, host = Paths(tmp_path), FakeHost()
    for directory in ('etc/yum.repos.d', 'var', 'boot', 'sys/module'):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / 'etc/os-release').write_text('ID="rocky"\nVERSION_ID="10.2"\n')
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text(BOOT)
    paths.at('/etc/yum.repos.d/rocky.repo').write_text('''[baseos]
name=Rocky Linux 10 BaseOS
mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=$basearch&repo=BaseOS-$releasever
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-10
[appstream]
mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=$basearch&repo=AppStream-$releasever
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-10
[crb]
mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=$basearch&repo=CRB-$releasever
enabled=0
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-10
[google-chrome]
baseurl=https://dl.google.com/linux/chrome/rpm/stable/x86_64
enabled=1
''')
    config = Config(user='user', display=False)
    with patch('ala.prepare.platform_error', return_value=None), patch('ala.prepare.shutil.disk_usage', return_value=type('Usage', (), {'free': 20 * 1024**3})()):
        yield Prepare(config, host, paths), host, paths


def states(report):
    return {r.id: r.state for r in report.results}


def marker(paths):
    return json.loads(paths.at(MARKER).read_text())


def snapshot(paths):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in paths.root.rglob('*') if p.is_file()}


def upgrade_calls(host):
    return [argv for argv in host.executed if argv[0] == 'dnf']


def reboot(host, paths, kernel=NEW):
    paths.at('/proc/sys/kernel/random/boot_id').write_text(NEXT)
    host.kernel = kernel


def test_optional_initial_state_has_no_commands_or_writes(setup):
    operation, host, paths = setup
    before = snapshot(paths)
    assert operation.inspect().exit_code == 0
    assert states(operation.inspect()) == {'prepare.state': 'not_applicable'}
    assert before == snapshot(paths)
    assert host.calls == []


def test_plan_uses_only_temporary_metadata_and_no_mutation(setup):
    operation, host, paths = setup
    before = snapshot(paths)
    assert states(operation.plan())['prepare.updates'] == 'change_required'
    assert before == snapshot(paths)
    assert host.executed == []
    argv, kw = next((a, k) for a, k in host.calls if 'check-update' in a)
    for option in DNF_OPTIONS:
        assert option in argv
    for key in ('cachedir', 'persistdir', 'logdir'):
        value = next(v.split('=', 2)[2] for v in argv if v.startswith('--setopt=' + key + '='))
        assert 'ala-prepare-plan-' in value
        assert not Path(value).exists()
    assert '--enablerepo=baseos' in argv
    assert '--enablerepo=appstream' in argv
    assert '--enablerepo=crb' not in argv
    assert '--enablerepo=google-chrome' not in argv
    assert kw['env']['LC_ALL'] == 'C'


def test_success_waits_then_verifies_reboot_without_second_upgrade(setup):
    operation, host, paths = setup
    assert operation.apply().exit_code == 2
    value = marker(paths)
    assert value['state'] == 'reboot_required'
    assert value['expected_kernel'] == NEW
    assert value['transaction_id'] == 12
    assert value['changed_packages'] == [kernel_row(NEW)]
    assert paths.at(MARKER).stat().st_mode & 0o777 == 0o600
    before = snapshot(paths)
    assert operation.apply().exit_code == 2
    assert before == snapshot(paths)
    assert len(upgrade_calls(host)) == 1
    reboot(host, paths)
    assert states(operation.inspect()) == {'prepare.verification': 'pending'}
    assert marker(paths)['state'] == 'reboot_required'
    assert operation.apply().exit_code == 0
    assert marker(paths)['verified_boot_id'] == NEXT
    assert len(upgrade_calls(host)) == 1
    before = snapshot(paths)
    assert operation.apply().exit_code == 0
    assert operation.plan().exit_code == 0
    assert before == snapshot(paths)


def test_previous_kernel_after_reboot_is_blocked(setup):
    operation, host, paths = setup
    operation.apply()
    reboot(host, paths, OLD)
    assert states(operation.apply())['prepare.boot_kernel'] == 'blocked'
    assert marker(paths)['state'] == 'reboot_required'
    assert len(upgrade_calls(host)) == 1


def test_no_updates_and_correct_kernel_completes_without_reboot(setup):
    operation, host, paths = setup
    host.updates, host.change = False, False
    assert states(operation.plan())['prepare.updates'] == 'ok'
    assert operation.apply().exit_code == 0
    assert marker(paths)['state'] == 'complete'
    assert marker(paths)['transaction_id'] is None
    assert not any('history' in a for a, _ in host.calls)


def test_no_update_but_different_default_kernel_requires_reboot(setup):
    operation, host, paths = setup
    host.rows.append(kernel_row(NEW))
    host.default = NEW
    host.change = False
    assert operation.apply().exit_code == 2
    assert marker(paths)['expected_kernel'] == NEW


def test_package_only_update_still_requires_reboot(setup):
    operation, host, paths = setup
    def update(argv, **kwargs):
        host.executed.append(tuple(argv))
        host.rows[1] = 'bash\t0\t5.2\t2.el10\tx86_64'
        return 0
    host.execute = update
    assert operation.apply().exit_code == 2
    assert marker(paths)['expected_kernel'] == OLD
    reboot(host, paths, OLD)
    assert operation.apply().exit_code == 0


def test_failed_partial_transaction_preserves_baseline_and_retry_reboot(setup):
    operation, host, paths = setup
    original = list(host.rows)
    host.upgrade_code = 1
    assert states(operation.apply())['prepare.upgrade'] == 'error'
    assert marker(paths)['state'] == 'failed'
    assert marker(paths)['before'] == sorted(original)
    assert kernel_row(NEW) in marker(paths)['after_failure']
    host.upgrade_code, host.change = 0, False
    assert operation.apply().exit_code == 2
    assert marker(paths)['attempts'] == 2
    assert kernel_row(NEW) in marker(paths)['changed_packages']


def test_power_cut_is_not_automatically_resumed_and_explicit_retry_rechecks(setup):
    operation, host, paths = setup
    host.crash = True
    with pytest.raises(KeyboardInterrupt):
        operation.apply()
    assert marker(paths)['state'] == 'upgrading'
    before = snapshot(paths)
    assert states(operation.inspect()) == {'prepare.recovery': 'blocked'}
    assert snapshot(paths) == before
    host.crash, host.change = False, False
    reboot(host, paths)
    assert operation.apply().exit_code == 2
    assert marker(paths)['boot_id'] == NEXT
    assert len(upgrade_calls(host)) == 2


@pytest.mark.parametrize('name', ['dkms', 'akmods', 'akmod-wireless', 'kmod-nvidia', 'nvidia-open', 'nvidia-driver', 'cuda-drivers'])
def test_external_modules_prevent_update(setup, name):
    operation, host, paths = setup
    host.rows.append(name + '\t0\t1\t1.el10\tx86_64')
    assert states(operation.apply())['prepare.external_modules'] == 'blocked'
    assert host.executed == []
    assert not paths.at(MARKER).exists()


def test_loaded_out_of_tree_module_blocks_even_without_package(setup):
    operation, host, paths = setup
    taint = paths.at('/sys/module/vendor_wifi/taint')
    taint.parent.mkdir()
    taint.write_text('OE')
    assert states(operation.plan())['prepare.external_modules'] == 'blocked'
    assert not any(a[0] == 'dnf' for a, _ in host.calls)


def test_dkms_registered_module_blocks(setup):
    operation, host, paths = setup
    paths.at('/var/lib/dkms/vendor/1.0').mkdir(parents=True)
    assert states(operation.apply())['prepare.external_modules'] == 'blocked'


def test_kmod_libs_is_not_external_driver(setup):
    operation, host, paths = setup
    host.rows.append('kmod-libs\t0\t1\t1.el10\tx86_64')
    assert states(operation.plan())['prepare.external_modules'] == 'ok'


@pytest.mark.parametrize('failure', [Command(127, '', 'missing'), Command(124, '', 'timeout')])
def test_missing_inventory_is_unknown(setup, failure):
    operation, host, paths = setup
    host.replies[RPM_QUERY] = failure
    assert states(operation.apply())['prepare.inventory'] == 'unknown'
    assert host.executed == []


def test_active_package_process_blocks(setup):
    operation, host, paths = setup
    host.replies[('pgrep', '-x', 'dnf|dnf5|yum|rpm|packagekitd')] = Command(0, '123\n')
    assert states(operation.apply())['prepare.transaction'] == 'blocked'
    assert host.executed == []


def test_process_permission_failure_is_unknown(setup):
    operation, host, paths = setup
    host.replies[('pgrep', '-x', 'dnf|dnf5|yum|rpm|packagekitd')] = Command(127)
    assert states(operation.apply())['prepare.transaction'] == 'unknown'
    assert host.executed == []


def test_reserved_offline_update_blocks_without_delete(setup):
    operation, host, paths = setup
    paths.at('/system-update').symlink_to('/var/lib/offline')
    assert any(r.state == 'blocked' and r.id.startswith('update.offline') for r in operation.apply().results)
    assert paths.at('/system-update').is_symlink()
    assert host.executed == []


def test_low_disk_blocks(setup):
    operation, host, paths = setup
    with patch('ala.prepare.shutil.disk_usage', return_value=type('Usage', (), {'free': 1})()):
        assert states(operation.apply())['prepare.space.var'] == 'blocked'
    assert host.executed == []


@pytest.mark.parametrize('old,new', [('mirrors.rockylinux.org', 'evil.example'), ('gpgcheck=1', 'gpgcheck=0'), ('[appstream]', '[missing]')])
def test_untrusted_or_missing_official_repo_blocks(setup, old, new):
    operation, host, paths = setup
    file = paths.at('/etc/yum.repos.d/rocky.repo')
    file.write_text(file.read_text().replace(old, new))
    assert states(operation.apply())['prepare.repositories'] == 'blocked'
    assert host.executed == []


def test_update_policy_before_upgrade(setup):
    operation, host, paths = setup
    host.policy_ready = False
    assert operation.apply().exit_code == 2
    assert host.executed[0][0] == 'bash'
    assert host.executed[1][0] == 'dnf'
    assert all('reboot' not in argv and 'restart' not in argv for argv in host.executed)


def test_failed_update_policy_does_not_upgrade(setup):
    operation, host, paths = setup
    host.policy_ready = False
    host.policy_code = 1
    assert states(operation.apply())['prepare.update_policy'] == 'error'
    assert not upgrade_calls(host)


def test_validation_failure_after_upgrade_does_not_repeat_transaction(setup):
    operation, host, paths = setup
    host.replies[('grubby', '--default-kernel')] = Command(127)
    assert operation.apply().exit_code == 1
    assert marker(paths)['state'] == 'verifying'
    del host.replies[('grubby', '--default-kernel')]
    assert operation.apply().exit_code == 2
    assert len(upgrade_calls(host)) == 1


def test_new_kernel_installed_but_grub_default_old_never_completes(setup):
    operation, host, paths = setup
    host.replies[('grubby', '--default-kernel')] = Command(0, '/boot/vmlinuz-' + OLD)
    assert operation.apply().exit_code == 1
    assert marker(paths)['state'] == 'verifying'


def test_untrusted_marker_blocks(setup):
    operation, host, paths = setup
    p = paths.at(MARKER)
    p.parent.mkdir(parents=True)
    p.write_text('{}')
    p.chmod(0o600)
    assert operation.apply().exit_code == 1
    assert host.executed == []
    p.unlink()
    p.symlink_to('/tmp/unrelated')
    assert operation.apply().exit_code == 1
    assert host.executed == []


def test_real_host_rejected_in_alternative_root(setup):
    operation, host, paths = setup
    assert states(Prepare(Config(), Host(), paths).apply()) == {'prepare.execution': 'blocked'}


def test_marker_reads_do_not_run_repo_or_update_checks(setup):
    operation, host, paths = setup
    operation.apply()
    host.calls.clear()
    operation.inspect()
    assert [a for a, _ in host.calls] == [('uname', '-r')]


def test_unloaded_extra_kernel_module_is_blocked(setup):
    operation, host, paths = setup
    module = paths.at('/usr/lib/modules/' + OLD + '/extra/vendor.ko.xz')
    module.parent.mkdir(parents=True)
    module.write_bytes(b'not loaded')
    assert states(operation.apply())['prepare.external_modules'] == 'blocked'
    assert host.executed == []


def test_runfile_driver_is_blocked_without_package_or_loaded_module(setup):
    operation, host, paths = setup
    binary = paths.at('/usr/bin/nvidia-uninstall')
    binary.parent.mkdir(parents=True)
    binary.write_text('')
    assert states(operation.apply())['prepare.external_modules'] == 'blocked'


def test_post_transaction_policy_drift_is_repaired_before_completion(setup):
    operation, host, paths = setup
    host.change = False
    execute = host.execute
    def drift(argv, **kwargs):
        result = execute(argv, **kwargs)
        if argv[0] == 'dnf':
            host.policy_ready = False
        return result
    host.execute = drift
    assert operation.apply().exit_code == 0
    assert [a[0] for a in host.executed] == ['dnf', 'bash']
    assert marker(paths)['state'] == 'complete'
    assert host.policy_ready


def test_failed_post_transaction_policy_never_marks_complete(setup):
    operation, host, paths = setup
    host.change = False
    execute = host.execute
    def drift(argv, **kwargs):
        result = execute(argv, **kwargs)
        if argv[0] == 'dnf':
            host.policy_ready = False
            host.policy_code = 1
        return result
    host.execute = drift
    assert operation.apply().exit_code == 1
    assert marker(paths)['state'] == 'verifying'
    assert states(operation.plan())['prepare.recovery'] == 'change_required'
    host.policy_code = 0
    assert operation.apply().exit_code == 0
    assert len(upgrade_calls(host)) == 1
    assert marker(paths)['state'] == 'complete'


def test_reboot_verification_repairs_policy_before_complete(setup):
    operation, host, paths = setup
    operation.apply()
    reboot(host, paths)
    host.policy_ready = False
    host.policy_code = 1
    assert operation.apply().exit_code == 1
    assert marker(paths)['state'] == 'reboot_required'
    host.policy_code = 0
    assert operation.apply().exit_code == 0
    assert marker(paths)['state'] == 'complete'
    assert len(upgrade_calls(host)) == 1


def test_unexpected_removed_package_not_hidden_as_success(setup):
    operation, host, paths = setup
    execute = host.execute
    def remove(argv, **kwargs):
        result = execute(argv, **kwargs)
        if argv[0] == 'dnf':
            host.rows = [row for row in host.rows if not row.startswith('bash\t')]
        return result
    host.execute = remove
    assert states(operation.apply())['prepare.removed_packages'] == 'blocked'
    assert marker(paths)['state'] == 'verifying'


def test_untrusted_signing_key_blocks_update(setup):
    operation, host, paths = setup
    key = str(paths.at('/etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-10'))
    host.replies[('rpm', '-Vf', key)] = Command(1, 'S.5....T. key changed')
    assert states(operation.apply())['prepare.repositories'] == 'blocked'
    assert host.executed == []


def test_metadata_failure_is_unknown_and_readonly(setup):
    operation, host, paths = setup
    run = host.run
    def fail(argv, **kwargs):
        if 'check-update' in argv:
            return Command(1, '', 'DNS lookup failed')
        return run(argv, **kwargs)
    host.run = fail
    before = snapshot(paths)
    assert states(operation.plan())['prepare.updates'] == 'unknown'
    assert host.executed == []
    assert before == snapshot(paths)


def test_unsuccessful_history_after_noop_retry_is_not_success(setup):
    operation, host, paths = setup
    run = host.run
    def failed_history(argv, **kwargs):
        if 'history' in argv:
            return Command(0, 'Transaction ID : 12\nReturn-Code    : Failure: 1\n')
        return run(argv, **kwargs)
    host.run = failed_history
    assert operation.apply().exit_code == 1
    assert marker(paths)['state'] == 'verifying'
    assert operation.apply().exit_code == 1
    assert len(upgrade_calls(host)) == 1
