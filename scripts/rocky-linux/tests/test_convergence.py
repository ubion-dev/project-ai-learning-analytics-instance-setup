"""Re-running setup converges without repeating system mutations."""
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
DNF_OPTIONS = ['--setopt=install_weak_deps=False',
               '--exclude=kernel,kernel-core,kernel-debug*,kernel-uki*,rocky-release*,rocky-repos*,rocky-gpg-keys*']


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.state = self.root / 'state.json'
        self.state.write_text(json.dumps({'packages': ['git'], 'enabled': [], 'active': [], 'masked': []}))
        self.log = self.root / 'mutations'
        dispatcher = '''#!/usr/bin/env python3
import json, os, pathlib, sys
path = pathlib.Path(os.environ['TEST_STATE'])
state = json.loads(path.read_text())
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'rpm':
    if os.environ.get('TEST_RPM_FAILURE'): sys.exit(int(os.environ['TEST_RPM_FAILURE']))
    sys.exit(0 if args[-1] in state['packages'] else 1)
if name == 'systemctl' and args[0] in ('is-active', 'is-enabled'):
    key = args[-1]
    if args[0] == 'is-active':
        sys.exit(0 if key in state['active'] else 3)
    if key in state['masked']:
        print('masked'); sys.exit(1)
    if key in state['enabled']:
        print('enabled'); sys.exit(0)
    if key in state.get('runtime_enabled', []):
        print('enabled-runtime'); sys.exit(0)
    sys.exit(0 if key in state['enabled'] else 1)
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(name + ' ' + ' '.join(args) + '\\n')
if name == 'dnf':
    assert args[:2] == ['install', '-y'], args
    if os.environ.get('TEST_DNF_FAILURE'): sys.exit(int(os.environ['TEST_DNF_FAILURE']))
    state['packages'].extend(arg for arg in args[2:] if not arg.startswith('-'))
elif name == 'systemctl':
    key = args[-1]
    if args[0] == 'enable': state['enabled'].append(key)
    elif args[0] == 'start': state['active'].append(key)
    elif args[0] == 'mask': state['masked'].append(key)
    else: raise AssertionError(args)
path.write_text(json.dumps(state))
'''
        for name in ('rpm', 'dnf', 'systemctl'):
            file = self.bin / name
            file.write_text(dispatcher)
            file.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        TEST_STATE=str(self.state), TEST_LOG=str(self.log))

    def run_shell(self, body, *args):
        result = subprocess.run(['bash', '-c', 'source "$1"; shift; ' + body, '_',
                                 str(SCRIPTS / 'common.sh'), *map(str, args)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_only_missing_packages_are_installed_and_second_run_has_no_transaction(self):
        self.run_shell('install_missing_packages git tmux; install_missing_packages git tmux')
        # ALA-08 adds transaction protection without weakening the no-op assertion.
        self.assertEqual(self.log.read_text().splitlines(), [' '.join(['dnf', 'install', '-y', *DNF_OPTIONS, 'tmux'])])

    def test_install_policy_blocks_boot_kernel_and_release_but_allows_current_kernel_components(self):
        packages = ['kernel-devel-6.12-test', 'kernel-modules-6.12-test', 'kernel-modules-extra-6.12-test']
        self.run_shell('install_missing_packages "$@"', *packages)
        command = self.log.read_text().strip().split()
        self.assertEqual(command, ['dnf', 'install', '-y', *DNF_OPTIONS, *packages])
        excludes = next(arg.split('=', 1)[1].split(',') for arg in command if arg.startswith('--exclude='))
        for name in ['kernel', 'kernel-core', 'kernel-debug-core', 'kernel-uki-virt',
                     'rocky-release', 'rocky-repos', 'rocky-gpg-keys']:
            self.assertTrue(any(fnmatch.fnmatchcase(name, pattern) for pattern in excludes), name)
        for name in ['kernel-devel', 'kernel-modules', 'kernel-modules-extra']:
            self.assertFalse(any(fnmatch.fnmatchcase(name, pattern) for pattern in excludes), name)

    def test_install_failure_is_reported_without_upgrade_or_removal_fallback(self):
        result = subprocess.run(['bash', '-c', 'source "$1"; install_missing_packages tmux', '_',
                                 str(SCRIPTS / 'common.sh')], env=dict(self.env, TEST_DNF_FAILURE='42'),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn('실행 커널', result.stderr)
        self.assertIn('명시적 유지보수', result.stderr)
        self.assertEqual(json.loads(self.state.read_text())['packages'], ['git'])
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertNotIn('upgrade', self.log.read_text())
        self.assertNotIn('remove', self.log.read_text())

    def test_rpm_query_failure_is_not_treated_as_a_missing_package(self):
        for status in (2, 126, 127):
            with self.subTest(status=status):
                result = subprocess.run(['bash', '-c', 'source "$1"; install_missing_packages tmux', '_',
                                         str(SCRIPTS / 'common.sh')],
                                        env=dict(self.env, TEST_RPM_FAILURE=str(status)), capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn('설치 여부 조회 실패', result.stderr)
                self.assertFalse(self.log.exists())

    def test_unchanged_file_preserves_mtime_and_does_not_create_another_backup(self):
        source, target = self.root / 'candidate', self.root / 'config'
        source.write_text('desired\n')
        target.write_text('old\n')
        self.run_shell('install_if_changed "$1" "$2"', source, target)
        before = target.stat().st_mtime_ns
        self.run_shell('install_if_changed "$1" "$2"', source, target)
        self.assertEqual(target.read_text(), 'desired\n')
        self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertEqual(len(list(self.root.glob('.config.backup.*'))), 1)

    def test_services_and_masks_are_only_changed_once(self):
        self.run_shell('ensure_service chronyd; ensure_service chronyd; ensure_masked sleep.target; ensure_masked sleep.target')
        self.assertEqual(self.log.read_text().splitlines(),
                         ['systemctl enable chronyd', 'systemctl start chronyd', 'systemctl mask sleep.target'])

    def test_runtime_enablement_is_made_persistent_without_restarting_the_service(self):
        state = json.loads(self.state.read_text())
        state.update(active=['chronyd'], runtime_enabled=['chronyd'])
        self.state.write_text(json.dumps(state))
        self.run_shell('ensure_service chronyd')
        self.assertTrue(self.log.exists(), 'runtime-only enablement was left unchanged')
        self.assertEqual(self.log.read_text().splitlines(), ['systemctl enable chronyd'])

    def test_podman_conflict_explains_checks_without_removing_or_installing_packages(self):
        state = json.loads(self.state.read_text())
        state['packages'].append('podman')
        self.state.write_text(json.dumps(state))
        (self.root / 'setup-docker.sh').write_text((SCRIPTS / 'setup-docker.sh').read_text())
        (self.root / 'common.sh').write_text((SCRIPTS / 'common.sh').read_text() + '\nrequire_rocky() { :; }\n')
        result = subprocess.run(['bash', str(self.root / 'setup-docker.sh')], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('podman ps -a', result.stderr)
        self.assertIn('dnf remove', result.stderr)
        self.assertEqual(json.loads(self.state.read_text()), state)
        self.assertFalse(self.log.exists())

    def test_bash_second_run_preserves_files_without_user_access_to_script_directory(self):
        home = self.root / 'home'
        home.mkdir()
        (home / '.bashrc').write_text('# existing user alias\nalias ll="ls -l"\n')
        release = self.root / 'os-release'
        release.write_text('ID=rocky\nVERSION_ID=10\n')
        source = (SCRIPTS / 'setup-bash.sh').read_text()
        source = source.replace('[[ $EUID == 0 ]]', '[[ 1 == 1 ]]').replace('/etc/os-release', str(release))
        script = self.root / 'setup-bash.sh'
        script.write_text(source)
        common = (SCRIPTS / 'common.sh').read_text() + '\nrequire_rocky() { :; }\n'
        (self.root / 'common.sh').write_text(common)
        commands = {
            'getent': '#!/bin/sh\nprintf "tester:x:1000:1000::%s:/bin/bash\\n" "$TEST_HOME"\n',
            'runuser': '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
common = pathlib.Path(os.environ['TEST_COMMON'])
hidden = common.with_suffix('.unreadable')
common.rename(hidden)
try:
    result = subprocess.run(sys.argv[4:], input=sys.stdin.read(), text=True)
finally:
    hidden.rename(common)
sys.exit(result.returncode)
''',
            'git': '''#!/usr/bin/env python3
import pathlib, sys
if sys.argv[1] == 'clone':
    target = pathlib.Path(sys.argv[-1]); target.mkdir()
    (target / 'oh-my-bash.sh').write_text('# test upstream entrypoint\\n')
elif 'rev-parse' in sys.argv:
    print('0123456789abcdef')
else:
    raise AssertionError(sys.argv)
''',
        }
        for name, text in commands.items():
            executable = self.bin / name
            executable.write_text(text)
            executable.chmod(0o755)
        env = dict(self.env, TEST_HOME=str(home), TEST_COMMON=str(self.root / 'common.sh'))
        def apply():
            result = subprocess.run(['bash', str(script), '--user', 'tester'], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        apply()
        files = [home / '.bashrc', home / '.bash_profile', home / '.config/server-bash/init.bash',
                 home / '.config/server-bash/server.theme.sh', home / '.config/server-bash/oh-my-bash.commit',
                 home / '.oh-my-bash/custom/themes/server/server.theme.sh']
        mtimes = [p.stat().st_mtime_ns for p in files]
        backups = sorted(str(p) for p in home.rglob('*backup*'))
        transactions = self.log.read_text()
        apply()
        self.assertEqual([p.stat().st_mtime_ns for p in files], mtimes)
        self.assertEqual(sorted(str(p) for p in home.rglob('*backup*')), backups)
        self.assertEqual(self.log.read_text(), transactions)
        self.assertEqual((home / '.bashrc').read_text().count('# server-bash managed hook'), 1)


if __name__ == '__main__':
    unittest.main()
