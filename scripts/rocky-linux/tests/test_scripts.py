"""Local contract tests. Never run DNF, systemctl, or a real Docker daemon."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def test_display_policy_flag_keeps_legacy_alias(self):
        for option in ('--display-policy', '--kiosk'):
            with self.subTest(option=option):
                result = subprocess.run(
                    ['bash', str(SCRIPTS / 'verify.sh'), option, '--help'],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--display-policy', result.stdout)

    def test_system_backups_are_hidden_and_preserve_active_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / '00-display'
            policy.write_text('original-policy')
            result = subprocess.run(
                ['bash', '-c', 'source "$1"; backup_file "$2"', '_',
                 str(SCRIPTS / 'common.sh'), str(policy)], capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            backups = list(Path(directory).glob('.00-display.backup.*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'original-policy')
            self.assertEqual(policy.read_text(), 'original-policy')
            self.assertEqual([p.name for p in Path(directory).iterdir() if not p.name.startswith('.')],
                             ['00-display'])

    def test_shell_syntax_including_generated_scripts(self):
        for path in SCRIPTS.glob('*.sh'):
            with self.subTest(script=path.name):
                subprocess.run(['bash', '-n', str(path)], check=True)
                source = path.read_text()
                # Inspect quoted heredocs that are executed or loaded as shell code.
                for marker in ('USER_SETUP', 'THEME', 'INIT', 'HOOK'):
                    opening = "<<'" + marker + "'\n"
                    if opening in source:
                        body = source.split(opening, 1)[1].split('\n' + marker + '\n', 1)[0]
                        subprocess.run(['bash', '-n'], input=body, text=True, check=True)

    def test_help_and_invalid_arguments_have_no_installation_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            forbidden = Path(directory) / 'dnf'
            forbidden.write_text('#!/bin/sh\necho unexpected-package-install >&2\nexit 99\n')
            forbidden.chmod(0o755)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'])
            for path in SCRIPTS.glob('*.sh'):
                if path.name == 'common.sh':
                    continue
                with self.subTest(script=path.name):
                    help_result = subprocess.run(['bash', str(path), '--help'], env=env, capture_output=True)
                    self.assertEqual(help_result.returncode, 0)
                    bad_result = subprocess.run(['bash', str(path), '--invalid-option'], env=env, capture_output=True)
                    self.assertNotEqual(bad_result.returncode, 0)
                    self.assertNotEqual(bad_result.returncode, 99)
                    self.assertNotIn(b'unexpected-package-install', bad_result.stderr)

    def test_substep_markers_are_opt_in_and_validate_protocol_fields(self):
        env = dict(os.environ)
        env.pop('ALA_REPORT_PROGRESS', None)
        command = ['bash', '-c', 'source "$1"; ala_substep packages started; echo ordinary-output',
                   '_', str(SCRIPTS / 'common.sh')]
        quiet = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertEqual(quiet.stdout, 'ordinary-output\n')
        enabled = subprocess.run(command, env=dict(env, ALA_REPORT_PROGRESS='1'), capture_output=True, text=True)
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertEqual(enabled.stdout, '__ALA_SUBSTEP__\tpackages\tstarted\nordinary-output\n')
        for name, state in [('packages;bad', 'started'), ('packages', 'unexpected')]:
            with self.subTest(name=name, state=state):
                invalid = subprocess.run(['bash', '-c', 'source "$1"; ala_substep "$2" "$3"',
                                          '_', str(SCRIPTS / 'common.sh'), name, state],
                                         env=dict(env, ALA_REPORT_PROGRESS='1'), capture_output=True, text=True)
                self.assertNotEqual(invalid.returncode, 0)
                self.assertNotIn('__ALA_SUBSTEP__', invalid.stdout)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        shutil.copy(SCRIPTS / 'setup.sh', self.directory / 'setup.sh')
        (self.directory / 'common.sh').write_text(
            'set -Eeuo pipefail\nrequire_rocky() { :; }\n'
            'getent() { echo test-account; }\ndie() { echo "$*" >&2; exit 1; }\n'
            'nvidia_gpu_present() { [[ ${GPU_PRESENT:-no} == yes ]]; }\n'
        )
        for name in ('setup-base.sh', 'setup-bash.sh', 'setup-dev-tools.sh',
                     'setup-ssh.sh', 'setup-display.sh', 'setup-update-policy.sh', 'setup-docker.sh',
                     'setup-nvidia.sh', 'setup-nvidia-container.sh', 'verify.sh'):
            (self.directory / name).write_text('''#!/usr/bin/env bash
name=${0##*/}
{
  printf '%s' "$name"
  for arg in "$@"; do printf ' <%s>' "$arg"; done
  printf '\\n'
} >> "$BOOTSTRAP_RECORD"
if [[ $name == "${FAIL_STAGE:-}" ]]; then exit 17; fi
if [[ $name == setup-nvidia.sh ]]; then exit "${GPU_STATUS:-0}"; fi
''')
        (self.directory / 'docker').write_text('#!/bin/sh\nprintf "%s\\n" "$GPU_RUNTIMES"\n')
        (self.directory / 'docker').chmod(0o755)
        self.record = self.directory / 'stages'

    def run_setup(self, *args, fail_stage='', gpu_status=0, gpu_present=False, runtime_ready=False):
        result = subprocess.run(
            ['bash', str(self.directory / 'setup.sh'), '--user', 'tester', *args],
            env=dict(os.environ, BOOTSTRAP_RECORD=str(self.record), FAIL_STAGE=fail_stage,
                     PATH=str(self.directory) + os.pathsep + os.environ['PATH'],
                     GPU_STATUS=str(gpu_status), GPU_PRESENT='yes' if gpu_present else 'no',
                     GPU_RUNTIMES='{"nvidia": {}}' if runtime_ready else '{}'),
            capture_output=True, text=True,
        )
        lines = self.record.read_text().splitlines() if self.record.exists() else []
        return result, lines

    def test_default_stages_exclude_display_uv_ssh_and_management_tools(self):
        result, stages = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stages, ['setup-base.sh', 'setup-update-policy.sh',
                                  'setup-bash.sh <--user> <tester>', 'setup-docker.sh', 'setup-nvidia.sh <--auto>', 'verify.sh <--update-policy>'])

    def test_optional_stages_and_arguments_are_forwarded(self):
        result, stages = self.run_setup('--with-root', '--upgrade', '--dev-tools',
                                        '--with-display', '--with-ssh', '--ssh-zone', 'internal')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stages, ['setup-base.sh <--upgrade>', 'setup-display.sh', 'setup-update-policy.sh <--with-display>',
                                  'setup-bash.sh <--user> <tester> <--with-root>',
                                  'setup-dev-tools.sh', 'setup-ssh.sh <--ssh-zone> <internal>',
                                  'setup-docker.sh', 'setup-nvidia.sh <--auto>', 'verify.sh <--update-policy> <--display-policy> <--ssh>'])

    def test_failure_stops_later_stages(self):
        result, stages = self.run_setup(fail_stage='setup-bash.sh')
        self.assertEqual(result.returncode, 17)
        self.assertEqual(len(stages), 3)

    def test_removed_uv_option_is_rejected_before_any_stage(self):
        result, stages = self.run_setup('--with-uv')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(stages, [])

    def test_plan_shows_selected_stages_without_executing_them(self):
        result, stages = self.run_setup('--with-ssh', '--plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stages, [])
        self.assertIn('[1/7]', result.stdout)
        self.assertIn('[7/7]', result.stdout)
        self.assertIn('setup-ssh.sh', result.stdout)
        self.assertNotIn('setup-dev-tools.sh', result.stdout)
        self.assertNotIn('setup-display.sh', result.stdout)

    def test_display_plan_can_be_used_for_either_physical_server(self):
        result, stages = self.run_setup('--with-display', '--with-ssh', '--plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stages, [])
        self.assertIn('setup-display.sh', result.stdout)
        self.assertIn('[8/8]', result.stdout)

    def test_plan_rejects_missing_option_value_without_changes(self):
        result, stages = self.run_setup('--ssh-zone', '--plan')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(stages, [])

    def test_gpu_reboot_pending_does_not_skip_common_verification_or_retry_installation(self):
        result, stages = self.run_setup(gpu_status=10, gpu_present=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(stages[-1].startswith('verify.sh'))
        self.assertFalse(any(line.startswith('setup-nvidia-container.sh') for line in stages))
        self.assertIn('[대기]', result.stdout)

    def test_ready_gpu_automatically_configures_container_and_verifies_runtime(self):
        result, stages = self.run_setup(gpu_present=True, runtime_ready=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('setup-nvidia-container.sh', stages)
        self.assertIn('<--gpu>', stages[-1])

    def test_gpu_installation_error_is_not_reported_as_success(self):
        result, stages = self.run_setup(gpu_status=1, gpu_present=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(any(line.startswith('verify.sh') for line in stages))


class NvidiaContainerTests(unittest.TestCase):
    def test_repeat_before_docker_restart_remains_pending_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (SCRIPTS / 'setup-nvidia-container.sh').read_text()
            source = source.replace('/etc/docker', str(root / 'etc/docker'))
            source = source.replace('/etc/yum.repos.d', str(root / 'etc/yum.repos.d'))
            script = root / 'setup-nvidia-container.sh'
            script.write_text(source)
            docker_source = (SCRIPTS / 'setup-docker.sh').read_text().replace('/etc/docker', str(root / 'etc/docker'))
            (root / 'setup-docker.sh').write_text(docker_source)
            (root / 'common.sh').write_text('''set -Eeuo pipefail
source "$REAL_COMMON"
require_rocky() { :; }
die() { echo "$*" >&2; exit 1; }
add_repo() { :; }
''')
            binary = root / 'bin'
            binary.mkdir()
            for name in ('dnf', 'dockerd', 'nvidia-smi', 'systemctl'):
                (binary / name).write_text('#!/bin/sh\nexit 0\n')
            (binary / 'docker').write_text('#!/bin/sh\nprintf "{}\\n"\n')
            (binary / 'rpm').write_text('''#!/bin/sh
case "$*" in
  '-q podman'|'-q podman-docker'|'-q runc'|'-q docker'|'-q docker-common'|'-q docker-engine') exit 1 ;;
  *) exit 0 ;;
esac
''')
            (binary / 'nvidia-ctk').write_text('''#!/usr/bin/env python3
import json, pathlib, sys
path = pathlib.Path(next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--config=')))
data = json.loads(path.read_text())
data.setdefault('runtimes', {})['nvidia'] = {'path': 'nvidia-container-runtime', 'runtimeArgs': []}
path.write_text(json.dumps(data, indent=4, sort_keys=True) + '\\n')
''')
            for executable in binary.iterdir():
                executable.chmod(0o755)
            env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                       REAL_COMMON=str(SCRIPTS / 'common.sh'))
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    docker_result = subprocess.run(['bash', str(root / 'setup-docker.sh')], env=env, capture_output=True, text=True)
                    self.assertEqual(docker_result.returncode, 0, docker_result.stderr)
                    result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('restart docker', result.stdout)
                    config = root / 'etc/docker/daemon.json'
                    if attempt == 0:
                        mtime = config.stat().st_mtime_ns
                        backups = list(config.parent.glob('.daemon.json.backup.*'))
                    else:
                        self.assertEqual(config.stat().st_mtime_ns, mtime)
                        self.assertEqual(list(config.parent.glob('.daemon.json.backup.*')), backups)


class UpdatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.etc = self.directory / 'etc'
        source = (SCRIPTS / 'setup-update-policy.sh').read_text()
        # Redirect all system paths and replace platform/services with sandbox mocks.
        source = source.replace('/etc/', str(self.etc) + '/')
        source = source.replace(' /system-update', ' ' + str(self.directory / 'system-update'))
        (self.directory / 'setup-update-policy.sh').write_text(source)
        common = (SCRIPTS / 'common.sh').read_text().replace('/etc/', str(self.etc) + '/')
        common = common.replace('prepare_kiosk_dconf()', 'real_prepare_kiosk_dconf()')
        (self.directory / 'common.sh').write_text(common + '''
require_rocky() { :; }
prepare_kiosk_dconf() {
  [[ ${DISPLAY_AVAILABLE:-yes} == yes ]] || die 'GNOME unavailable'
  real_prepare_kiosk_dconf
}
''')
        binary = self.directory / 'bin'
        binary.mkdir()
        (binary / 'systemctl').write_text('''#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['POLICY_RECORD'], 'a') as log:
    log.write(' '.join(args) + '\\n')
if args[0] == 'is-active':
    sys.exit(0 if args[-1] == os.environ.get('ACTIVE_UNIT') else 3)
if args[0] == 'is-enabled':
    lines = pathlib.Path(os.environ['POLICY_RECORD']).read_text().splitlines()
    if any(args[-1] in line.split()[1:] for line in lines if line.startswith('mask ')):
        print('masked')
    sys.exit(1)
''')
        (binary / 'dconf').write_text('''#!/usr/bin/env python3
import os, pathlib, re, sys
with open(os.environ['POLICY_RECORD'], 'a') as log: log.write('dconf update\\n')
for source in (pathlib.Path(os.environ['TEST_ETC']) / 'dconf/db').glob('*.d'):
    source.with_suffix('').touch()
    if not re.fullmatch(r'[A-Za-z0-9_]+', source.stem):
        print('GLib-GIO-CRITICAL: invalid D-Bus object_path', file=sys.stderr)
''')
        for path in binary.iterdir():
            path.chmod(0o755)
        self.record = self.directory / 'operations'
        self.env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                        TEST_ETC=str(self.etc), POLICY_RECORD=str(self.record))

    def apply(self, active='', with_display=True):
        args = ['--with-display'] if with_display else []
        return subprocess.run(['bash', str(self.directory / 'setup-update-policy.sh'), *args],
                              env=dict(self.env, ACTIVE_UNIT=active,
                                       DISPLAY_AVAILABLE='yes' if with_display else 'no'),
                              capture_output=True, text=True)

    def test_system_updates_can_be_blocked_without_gnome(self):
        result = self.apply(with_display=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mask dnf-automatic-install.timer', self.record.read_text())
        self.assertFalse((self.etc / 'dconf').exists())

    def test_missing_units_are_masked_and_not_stopped(self):
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        operations = self.record.read_text()
        self.assertIn('mask dnf-automatic-install.timer', operations)
        self.assertIn('packagekit-offline-update.service', operations)
        self.assertNotIn('stop ', operations)
        policy = self.etc / 'dconf/db/rocky_setup.d/10-updates'
        self.assertIn('allow-updates=false', policy.read_text())

    def test_active_transaction_is_not_interrupted(self):
        result = self.apply(active='dnf-automatic-install.service')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('mask ', self.record.read_text())
        self.assertNotIn('stop ', self.record.read_text())

    def test_same_policy_is_not_rewritten_or_recompiled_on_second_run(self):
        first = self.apply()
        self.assertEqual(first.returncode, 0, first.stderr)
        paths = [self.etc / 'dconf/db/rocky_setup.d/10-updates',
                 self.etc / 'dconf/db/rocky_setup.d/locks/updates', self.etc / 'dconf/db/rocky_setup']
        mtimes = [p.stat().st_mtime_ns for p in paths]
        second = self.apply()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual([p.stat().st_mtime_ns for p in paths], mtimes)
        self.assertEqual(self.record.read_text().count('dconf update'), 1)

    def test_unchanged_sources_recover_an_outdated_compiled_database(self):
        first = self.apply()
        self.assertEqual(first.returncode, 0, first.stderr)
        database = self.etc / 'dconf/db/rocky_setup'
        os.utime(database, ns=(0, 0))
        second = self.apply()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertGreater(database.stat().st_mtime_ns, 0)
        self.assertEqual(self.record.read_text().count('dconf update'), 2)

    def test_pending_broken_symlink_is_detected_before_changes(self):
        (self.directory / 'system-update').symlink_to(self.directory / 'missing-target')
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.record.exists())

    def test_database_can_emit_a_valid_dbus_change_notification(self):
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('GLib-GIO-CRITICAL', result.stderr)
        self.assertTrue((self.etc / 'dconf/db/rocky_setup').is_file())

    def test_legacy_policy_is_migrated_and_custom_sources_are_preserved(self):
        legacy = self.etc / 'dconf/db/rocky-kiosk.d'
        legacy.mkdir(parents=True)
        custom = '[org/example/site]\nvalue=true\n'
        (legacy / '90-site').write_text(custom)
        (self.etc / 'dconf/db/rocky-kiosk').write_bytes(b'legacy compiled database')
        profile = self.etc / 'dconf/profile/user'
        profile.parent.mkdir(parents=True)
        profile.write_text('user-db:user\nsystem-db:rocky-kiosk\nsystem-db:site\n')
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('GLib-GIO-CRITICAL', result.stderr)
        self.assertFalse(legacy.exists())
        self.assertEqual((self.etc / 'dconf/db/rocky_setup.d/90-site').read_text(), custom)
        self.assertEqual(profile.read_text(), 'user-db:user\nsystem-db:rocky_setup\nsystem-db:site\n')
        self.assertEqual((self.etc / 'dconf/db/rocky-kiosk').read_bytes(), b'legacy compiled database')

    def test_nonconflicting_legacy_files_are_merged_and_retired_from_compilation(self):
        legacy = self.etc / 'dconf/db/rocky-kiosk.d'
        current = self.etc / 'dconf/db/rocky_setup.d'
        for path in (legacy, current): path.mkdir(parents=True)
        (legacy / '90-legacy-site').write_text('legacy\n')
        (current / '80-current-site').write_text('current\n')
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('GLib-GIO-CRITICAL', result.stderr)
        self.assertFalse(legacy.exists())
        self.assertEqual((current / '90-legacy-site').read_text(), 'legacy\n')
        self.assertEqual((current / '80-current-site').read_text(), 'current\n')

    def test_conflicting_custom_sources_are_not_overwritten_during_migration(self):
        legacy = self.etc / 'dconf/db/rocky-kiosk.d'
        current = self.etc / 'dconf/db/rocky_setup.d'
        for path in (legacy, current): path.mkdir(parents=True)
        (legacy / '90-site').write_text('old\n')
        (current / '90-site').write_text('different\n')
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((legacy / '90-site').read_text(), 'old\n')
        self.assertEqual((current / '90-site').read_text(), 'different\n')
        self.assertFalse((current / '10-updates').exists())


class DockerConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.original = self.directory / 'daemon.json'
        self.output = self.directory / 'candidate.json'
        source = (SCRIPTS / 'setup-docker.sh').read_text()
        self.code = source.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]

    def merge(self, value):
        self.original.write_text(json.dumps(value))
        result = subprocess.run(
            ['python3', '-', str(self.original), str(self.output)],
            input=self.code, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.output.read_text())

    def test_preserves_gpu_runtime_and_network_settings(self):
        original = {'runtimes': {'nvidia': {'path': 'nvidia-container-runtime'}}, 'bip': '127.0.0.1/16'}
        merged = self.merge(original)
        self.assertEqual(merged['runtimes'], original['runtimes'])
        self.assertEqual(merged['bip'], original['bip'])
        self.assertEqual(merged['log-opts'], {'max-size': '20m', 'max-file': '5'})
        self.assertEqual(merged, self.merge(merged))

    def test_preserves_existing_remote_logging(self):
        original = {'log-driver': 'syslog', 'log-opts': {'syslog-address': 'tcp://loghost:514'}}
        self.assertEqual(self.merge(original), original)

    def test_rejects_malformed_json_without_modifying_source(self):
        self.original.write_text('{invalid')
        result = subprocess.run(
            ['python3', '-', str(self.original), str(self.output)],
            input=self.code, text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.original.read_text(), '{invalid')
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
