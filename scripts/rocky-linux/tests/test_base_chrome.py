"""Chrome is part of setup-base and is installed only when missing."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
RPM_URL = 'https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm'
KEY_URL = 'https://dl.google.com/linux/linux_signing_key.pub'


class BaseChromeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'setup-base.sh'
        source = (SCRIPTS / 'setup-base.sh').read_text()
        source = source.replace(' --apply\n', ' --apply --directory "$TEST_POLICY_DIR" --backups "$TEST_POLICY_BACKUPS"\n')
        source = source.replace('/usr/local/bin', str(self.root / 'usr/local/bin'))
        self.script.write_text(source)
        shutil.copytree(SCRIPTS / 'lib', self.root / 'lib')
        shutil.copytree(SCRIPTS / 'bin', self.root / 'bin-source')
        source = self.script.read_text().replace('$ROCKY_SCRIPT_DIR/bin/chrome-did', '$ROCKY_SCRIPT_DIR/bin-source/chrome-did')
        self.script.write_text(source)
        self.policy_dir = self.root / 'etc/opt/chrome/policies/managed'
        self.backups = self.root / 'var/lib/rocky-setup/chrome-policy-backups'
        self.launcher = self.root / 'usr/local/bin/chrome-did'
        common = (SCRIPTS / 'common.sh').read_text() + '\nrequire_rocky() { :; }\nreport_reboot() { :; }\n'
        (self.root / 'common.sh').write_text(common)
        self.packages = self.root / 'packages.json'
        self.packages.write_text(json.dumps(['dnf-plugins-core', 'git', 'curl', 'ca-certificates',
                                           'bash-completion', 'firewalld', 'chrony', 'python3', 'iproute']))
        self.log = self.root / 'mutations'
        binary = self.root / 'bin'
        binary.mkdir()
        dispatcher = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
packages = pathlib.Path(os.environ['TEST_PACKAGES'])
installed = json.loads(packages.read_text())
if name == 'rpm' and args[0] == '-q': sys.exit(0 if args[-1] in installed else 1)
if name == 'uname': print(os.environ.get('TEST_ARCH', 'x86_64')); sys.exit(0)
if name == 'timedatectl':
    assert args == ['show', '--property=Timezone', '--value'], args
    print('Asia/Seoul'); sys.exit(0)
if name == 'systemctl':
    assert args[0] in ('is-enabled', 'is-active'), args
    print('enabled' if args[0] == 'is-enabled' else 'active'); sys.exit(0)
if name == 'google-chrome-stable':
    assert args == ['--version'], args
    assert 'google-chrome-stable' in installed
    print('Google Chrome test-version'); sys.exit(0)
with open(os.environ['TEST_LOG'], 'a') as log: log.write(json.dumps([name, *args]) + '\\n')
if name == 'rpm' and args[0] == '--import':
    assert args[1] == os.environ['KEY_URL'], args
    sys.exit(1 if os.environ.get('IMPORT_FAIL') else 0)
if name == 'dnf':
    assert args[:2] == ['install', '-y'], args
    assert '--setopt=localpkg_gpgcheck=1' in args, args
    assert args[-1] == os.environ['RPM_URL'], args
    installed.append('google-chrome-stable')
    packages.write_text(json.dumps(installed)); sys.exit(0)
raise AssertionError((name, args))
'''
        for name in ('rpm', 'dnf', 'uname', 'timedatectl', 'systemctl', 'google-chrome-stable'):
            path = binary / name
            path.write_text(dispatcher)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                        TEST_PACKAGES=str(self.packages), TEST_LOG=str(self.log), RPM_URL=RPM_URL, KEY_URL=KEY_URL,
                        TEST_POLICY_DIR=str(self.policy_dir), TEST_POLICY_BACKUPS=str(self.backups))

    def apply(self, **env):
        return subprocess.run(['bash', str(self.script)], env=dict(self.env, **env),
                              capture_output=True, text=True)

    def test_missing_chrome_is_verified_and_installed_once(self):
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('google-chrome-stable', json.loads(self.packages.read_text()))
        first = self.log.read_text()
        self.assertIn('localpkg_gpgcheck=1', first)
        policy = self.policy_dir / 'did-display.json'
        self.assertIs(json.loads(policy.read_text())['TranslateEnabled'], False)
        original_policy_time = policy.stat().st_mtime_ns
        original_launcher_time = self.launcher.stat().st_mtime_ns
        repeated = self.apply()
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(self.log.read_text(), first)
        self.assertEqual(policy.stat().st_mtime_ns, original_policy_time)
        self.assertEqual(self.launcher.stat().st_mtime_ns, original_launcher_time)
        self.assertFalse(self.backups.exists())

    def test_installed_chrome_is_kept_and_missing_policy_and_launcher_are_prepared(self):
        installed = json.loads(self.packages.read_text()) + ['google-chrome-stable']
        self.packages.write_text(json.dumps(installed))
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Google Chrome test-version', result.stdout)
        self.assertFalse(self.log.exists())
        self.assertTrue((self.policy_dir / 'did-display.json').exists())
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o755)

    def test_unsupported_architecture_does_not_install_an_x86_rpm(self):
        result = self.apply(TEST_ARCH='aarch64')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_key_import_failure_prevents_package_installation(self):
        result = self.apply(IMPORT_FAIL='yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('google-chrome-stable', json.loads(self.packages.read_text()))
        self.assertFalse(any(json.loads(line)[0] == 'dnf' for line in self.log.read_text().splitlines()))
