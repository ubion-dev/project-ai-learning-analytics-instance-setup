"""Expected-state verification must keep checking and must not repair the host."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.record = self.root / 'queries'
        self.script = self.root / 'verify.sh'
        self.installed = self.root / 'usr/local/bin/chrome-did'
        self.installed.parent.mkdir(parents=True)
        shutil.copy2(SCRIPTS / 'bin/chrome-did', self.installed)
        self.installed.chmod(0o755)
        shutil.copytree(SCRIPTS / 'bin', self.root / 'bin')
        shutil.copytree(SCRIPTS / 'lib', self.root / 'lib')
        self.policy = self.root / 'policies/managed/did-display.json'
        self.policy.parent.mkdir(parents=True)
        self.policy.parent.chmod(0o755)
        self.policy.parent.parent.chmod(0o755)
        self.policy.write_text(json.dumps(dict.fromkeys(
            ('TranslateEnabled', 'DefaultBrowserSettingEnabled', 'PromotionsEnabled'), False)))
        self.policy.chmod(0o644)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        source = (SCRIPTS / 'verify.sh').read_text().replace('/usr/local/bin/chrome-did', str(self.installed))
        source = source.replace(' --check\n', ' --check --directory "$TEST_POLICY_DIR"\n')
        source = source.replace('/etc/system-update', str(self.root / 'etc-system-update'))
        source = source.replace('/system-update', str(self.root / 'scheduled-update'))
        self.script.write_text(source)
        (self.root / 'common.sh').write_text((SCRIPTS / 'common.sh').read_text() + '\nrequire_rocky() { :; }\n')
        fake = self.root / 'fake-bin'
        fake.mkdir()
        dispatcher = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TEST_RECORD'], 'a') as record: record.write(json.dumps([name, *args]) + '\\n')
if name == 'systemctl':
    if args[0] == 'is-active':
        if args[-1] == os.environ.get('TEST_ACTIVE_AUTO'): sys.exit(0)
        if args[-1].startswith(('dnf-', 'packagekit')): sys.exit(3)
        sys.exit(3 if args[-1] in ('packagekit.service', os.environ.get('TEST_INACTIVE')) else 0)
    if args[0] == 'is-enabled':
        unit = args[-1]
        if unit.startswith(('dnf-', 'packagekit')) or unit.endswith('.target'): print('masked')
        else: print('enabled-runtime' if unit == os.environ.get('TEST_RUNTIME_ONLY') else 'enabled')
        sys.exit(0)
    raise AssertionError(('unexpected systemctl mutation', args))
if name == 'id': print('1000'); sys.exit(0)
if name == 'loginctl': print(os.environ['TEST_RUNTIME']); sys.exit(0)
if name == 'runuser': sys.exit(int(os.environ.get('TEST_CHROME_STATUS', '0')))
if name == 'google-chrome-stable':
    assert args == ['--version'], args
    print('Google Chrome test-version'); sys.exit(0)
if name == 'rpm':
    assert args[0] == '-q', args
    sys.exit(0)
if name == 'firewall-cmd': sys.exit(1)
if name == 'timedatectl': sys.exit(1 if os.environ.get('TEST_INFO_FAILURE') else 0)
if name in ('docker', 'ss', 'df'): sys.exit(0)
raise AssertionError(('unexpected host mutation', name, args))
'''
        for name in ('systemctl', 'id', 'loginctl', 'runuser', 'google-chrome-stable',
                     'rpm', 'firewall-cmd', 'timedatectl', 'docker', 'ss', 'df', 'dnf', 'systemd-run'):
            path = fake / name
            path.write_text(dispatcher)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ['PATH'],
                        TEST_RECORD=str(self.record), TEST_POLICY_DIR=str(self.policy.parent),
                        TEST_RUNTIME=str(self.runtime), SUDO_USER='tester')

    def verify(self, *args, **env):
        result = subprocess.run(['bash', str(self.script), *args], env=dict(self.env, **env),
                                capture_output=True, text=True)
        calls = [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []
        return result, calls

    def test_installation_state_passes_without_requiring_a_running_browser(self):
        result, calls = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('실패 0 / 대기 0', result.stdout)
        self.assertFalse(any(call[0] in ('runuser', 'dnf', 'systemd-run') for call in calls))

    def test_inactive_service_does_not_skip_later_checks_and_summary(self):
        result, calls = self.verify(TEST_INACTIVE='docker')
        self.assertEqual(result.returncode, 1)
        self.assertIn('[FAIL] docker 실행', result.stderr)
        self.assertTrue(any(call[0] == 'df' for call in calls))
        self.assertIn('검사 결과:', result.stdout)

    def test_runtime_only_enablement_does_not_pass_as_persistent(self):
        result, _ = self.verify(TEST_RUNTIME_ONLY='docker')
        self.assertEqual(result.returncode, 1)
        self.assertIn('기대: enabled / 현재: enabled-runtime', result.stdout)

    def test_url_check_uses_target_user_and_discovered_runtime_directory(self):
        url = 'https://example.com/did?a=1&b=2'
        result, calls = self.verify('--chrome-url', url, '--user', 'tester')
        self.assertEqual(result.returncode, 0, result.stderr)
        command = next(call for call in calls if call[0] == 'runuser')
        self.assertEqual(command[:5], ['runuser', '-u', 'tester', '--', 'env'])
        self.assertIn('XDG_RUNTIME_DIR=' + str(self.runtime), command)
        self.assertEqual(command[-2:], ['check', url])
        self.assertFalse(any(call[0] in ('dnf', 'systemd-run') for call in calls))

    def test_pending_browser_has_a_distinct_exit_code(self):
        result, _ = self.verify('--chrome', TEST_CHROME_STATUS='2')
        self.assertEqual(result.returncode, 2)
        self.assertIn('실패 0 / 대기 1', result.stdout)

    def test_chrome_failure_is_included_in_overall_result(self):
        result, _ = self.verify('--chrome-url', 'https://example.com', TEST_CHROME_STATUS='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('[FAIL] 실행 중 DID Chrome의 기대 상태', result.stderr)
        self.assertIn('실패 1 / 대기 0', result.stdout)

    def test_info_command_failure_does_not_abort_the_remaining_checks(self):
        result, calls = self.verify(TEST_INFO_FAILURE='yes')
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(call[0] == 'df' for call in calls))
        self.assertIn('검사 결과:', result.stdout)

    def test_changed_installed_launcher_is_detected_and_not_executed(self):
        self.installed.write_text('#!/bin/sh\nexit 0\n')
        result, calls = self.verify('--chrome')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(any(call[0] == 'runuser' for call in calls))

    def test_masked_but_running_automatic_update_is_not_reported_ready(self):
        result, _ = self.verify('--update-policy', TEST_ACTIVE_AUTO='dnf-automatic-install.service')
        self.assertEqual(result.returncode, 1)
        self.assertIn('[FAIL] dnf-automatic-install.service 비활성', result.stderr)
