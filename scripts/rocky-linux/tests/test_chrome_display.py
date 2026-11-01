"""Chrome policy and SSH launcher checks; no real Chrome/systemd is started."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]
POLICIES = dict.fromkeys(('TranslateEnabled', 'DefaultBrowserSettingEnabled', 'PromotionsEnabled'), False)


class ChromePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / 'policies/managed'
        self.backups = self.root / 'state/backups'
        self.target = self.directory / 'did-display.json'

    def run_policy(self, action):
        return subprocess.run([sys.executable, str(SCRIPTS / 'lib/chrome_policy.py'),
                               '--' + action, '--directory', str(self.directory),
                               '--backups', str(self.backups)], capture_output=True, text=True)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o755)
        path.parent.parent.chmod(0o755)
        path.write_text(json.dumps(data))
        path.chmod(0o644)

    def test_missing_policy_is_created_and_repeat_has_no_rewrite_or_backup(self):
        result = self.run_policy('apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.target.read_text()), POLICIES)
        before = (self.target.read_bytes(), self.target.stat().st_mtime_ns)
        result = self.run_policy('apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target.read_bytes(), self.target.stat().st_mtime_ns), before)
        self.assertFalse(self.backups.exists())

    def test_existing_unrelated_values_are_preserved_and_backup_is_outside_policy_directory(self):
        original = dict(POLICIES, TranslateEnabled=True, CustomPolicy={'URL': 'https://example.com'})
        self.write(self.target, original)
        result = self.run_policy('apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.target.read_text()), dict(original, **POLICIES))
        self.assertEqual([path.name for path in self.directory.iterdir()], ['did-display.json'])
        backups = list(self.backups.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), original)
        self.assertEqual(self.run_policy('apply').returncode, 0)
        self.assertEqual(list(self.backups.iterdir()), backups)
        self.assertEqual(list(self.directory.parent.glob('.did-display.*')), [])

    def test_equivalent_json_comments_and_formatting_are_kept(self):
        self.write(self.target, POLICIES)
        original = '''{
          // Chrome accepts comments and trailing commas.
          "TranslateEnabled": false,
          "DefaultBrowserSettingEnabled": false,
          "PromotionsEnabled": false,
          "CustomPolicy": ["https://example.com/a,}/*literal*/",],
        }'''
        self.target.write_text(original)
        result = self.run_policy('apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_text(), original)
        self.assertFalse(self.backups.exists())

    def test_numeric_zero_is_not_accepted_as_boolean_false(self):
        self.write(self.target, dict(POLICIES, TranslateEnabled=0))
        self.assertNotEqual(self.run_policy('check').returncode, 0)
        result = self.run_policy('apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(self.target.read_text())['TranslateEnabled'], False)

    def test_read_permissions_are_repaired_once(self):
        self.write(self.target, POLICIES)
        self.target.chmod(0o600)
        self.assertNotEqual(self.run_policy('check').returncode, 0)
        result = self.run_policy('apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(len(list(self.backups.iterdir())), 1)
        self.assertEqual(self.run_policy('apply').returncode, 0)
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_other_file_with_matching_policy_is_kept_without_duplicate_keys(self):
        external = self.directory / 'existing-policy'  # Chrome does not filter extensions.
        self.write(external, POLICIES)
        original = external.read_bytes()
        result = self.run_policy('apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(external.read_bytes(), original)
        self.assertEqual(json.loads(self.target.read_text()), {})
        self.assertEqual(self.run_policy('check').returncode, 0)

    def test_external_conflicting_policy_is_not_overwritten(self):
        self.write(self.target, dict(POLICIES, CustomPolicy=True))
        self.write(self.directory / 'other.json', {'TranslateEnabled': True})
        original = self.target.read_bytes()
        result = self.run_policy('apply')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('other.json', result.stderr)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertFalse(self.backups.exists())

    def test_invalid_existing_json_is_not_overwritten(self):
        self.write(self.target, POLICIES)
        self.target.write_text('{invalid')
        self.assertNotEqual(self.run_policy('apply').returncode, 0)
        self.assertEqual(self.target.read_text(), '{invalid')
        self.assertFalse(self.backups.exists())

    def test_hidden_backup_in_managed_is_checked_as_a_policy_file(self):
        self.write(self.target, POLICIES)
        self.write(self.directory / '.old-policy.backup', {'TranslateEnabled': True})
        result = self.run_policy('apply')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('.old-policy.backup', result.stderr)
        self.assertFalse(self.backups.exists())

    def test_check_does_not_create_missing_files(self):
        self.assertNotEqual(self.run_policy('check').returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])


class ChromeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.log = self.root / 'commands'
        self.script = self.root / 'chrome-did'
        source = (SCRIPTS / 'bin/chrome-did').read_text()
        self.script.write_text(source.replace("Path('/proc')", f'Path({str(self.root / "proc")!r})'))
        binary = self.root / 'bin'
        binary.mkdir()
        dispatcher = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'id': print(os.environ.get('TEST_UID', '1000')); sys.exit(0)
if name == 'google-chrome-stable': raise AssertionError('Chrome must run through systemd-run')
if name == 'systemctl':
    assert args[0] == '--user', args
    if args[1] == 'is-active': sys.exit(0 if os.environ.get('TEST_ACTIVE') else 3)
    if args[1] == 'show-environment':
        print(os.environ.get('TEST_MANAGER_ENV', 'DISPLAY=:1\\nWAYLAND_DISPLAY=wayland-0')); sys.exit(0)
    if args[1] == 'show': print('4321'); sys.exit(0)
with open(os.environ['TEST_LOG'], 'a') as record: record.write(json.dumps([name, *args]) + '\\n')
'''
        for name in ('id', 'google-chrome-stable', 'systemctl', 'systemd-run', 'journalctl'):
            path = binary / name
            path.write_text(dispatcher)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                        TEST_LOG=str(self.log), XDG_CONFIG_HOME=str(self.root / 'user config'))

    def run_launcher(self, *args, **env):
        result = subprocess.run(['bash', str(self.script), *args],
                                env=dict(self.env, **env), capture_output=True, text=True)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_ssh_start_includes_fullscreen_popup_options_and_preserves_url_as_one_argument(self):
        url = 'https://example.com/did?screen=1&label=a%20b&value=$literal&token=${literal}'
        result, calls = self.run_launcher('start', url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call[:4], ['systemd-run', '--user', '--collect', '--unit=did-chrome.service'])
        self.assertEqual(call[-1], url)
        self.assertIn('--expand-environment=no', call)
        for option in ('--start-fullscreen', '--no-first-run', '--no-default-browser-check', '--hide-crash-restore-bubble'):
            self.assertIn(option, call)
        self.assertIn('--user-data-dir=' + str(self.root / 'user config/chrome-did'), call)
        self.assertNotIn('--no-sandbox', call)
        self.assertNotIn('--ozone-platform=wayland', call)

    def test_wayland_only_environment_uses_native_wayland(self):
        result, calls = self.run_launcher('start', 'https://example.com', TEST_MANAGER_ENV='WAYLAND_DISPLAY=wayland-0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--ozone-platform=wayland', calls[0])

    def test_missing_desktop_environment_stops_before_launch(self):
        result, calls = self.run_launcher('start', 'https://example.com', TEST_MANAGER_ENV='LANG=ko_KR.UTF-8')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('로그인된 화면 환경', result.stderr)
        self.assertEqual(calls, [])

    def test_active_service_is_kept_without_relaunch(self):
        result, calls = self.run_launcher('start', 'https://example.com', TEST_ACTIVE='yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[유지]', result.stdout)
        self.assertEqual(calls, [])

    def test_stop_targets_only_the_did_service(self):
        result, calls = self.run_launcher('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [['systemctl', '--user', 'stop', 'did-chrome.service']])

    def test_root_and_invalid_urls_are_rejected_before_launch(self):
        for args, env in ((('start', 'https://example.com'), {'TEST_UID': '0'}),
                          (('start', 'file:///etc/passwd'), {}),
                          (('start', '--no-sandbox'), {}),
                          (('start', 'https://example.com\nother'), {})):
            with self.subTest(args=args):
                result, calls = self.run_launcher(*args, **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_help_and_shell_syntax(self):
        subprocess.run(['bash', '-n', str(SCRIPTS / 'bin/chrome-did')], check=True)
        result, calls = self.run_launcher('--help', TEST_UID='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_status_and_logs_are_scoped_to_the_did_service(self):
        result, calls = self.run_launcher('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-1], ['systemctl', '--user', 'status', 'did-chrome.service', '--no-pager', '--full'])
        result, calls = self.run_launcher('logs')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-1], ['journalctl', '--user', '--boot', '-u', 'did-chrome.service', '-n', '50', '--no-pager'])

    def write_process(self, missing=(), url='https://example.com', uid=None):
        process = self.root / 'proc/4321'
        process.mkdir(parents=True)
        arguments = ['/opt/google/chrome/chrome', '--start-fullscreen', '--no-first-run',
                     '--no-default-browser-check', '--hide-crash-restore-bubble',
                     '--user-data-dir=' + str(self.root / 'user config/chrome-did'), url]
        (process / 'cmdline').write_bytes(('\0'.join(arg for arg in arguments if arg not in missing) + '\0').encode())
        (process / 'status').write_text(f'Uid:\t{os.getuid() if uid is None else uid}\t1000\t1000\t1000\n')

    def test_check_inspects_running_options_and_expected_url_without_mutations(self):
        self.write_process()
        result, calls = self.run_launcher('check', 'https://example.com', TEST_ACTIVE='yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[OK] 기대한 사이트의 시작 주소', result.stdout)
        self.assertEqual(calls, [])

    def test_check_finds_missing_restore_option_without_restarting(self):
        self.write_process(missing=('--hide-crash-restore-bubble',))
        result, calls = self.run_launcher('check', TEST_ACTIVE='yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn('[FAIL] 탭 복원 안내 차단', result.stdout)
        self.assertEqual(calls, [])

    def test_check_reports_url_difference(self):
        self.write_process()
        result, calls = self.run_launcher('check', 'https://other.example.com', TEST_ACTIVE='yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn('[FAIL] 기대한 사이트의 시작 주소', result.stdout)
        self.assertEqual(calls, [])

    def test_stopped_browser_is_pending_without_url_and_failure_with_url(self):
        result, calls = self.run_launcher('check')
        self.assertEqual(result.returncode, 2)
        self.assertIn('[대기]', result.stdout)
        self.assertEqual(calls, [])
        result, calls = self.run_launcher('check', 'https://example.com')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, [])
