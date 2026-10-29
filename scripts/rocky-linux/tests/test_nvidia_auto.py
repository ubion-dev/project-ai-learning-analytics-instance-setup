"""Exercise automatic GPU detection using sysfs fixtures; never access host GPUs."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
SUPPORT_TABLE = '''# NVIDIA open GPU kernel modules
| Product name | PCI ID |
| NVIDIA GeForce RTX 4090 | 2684 |
| NVIDIA GeForce RTX 5090 | 2B85 |
| NVIDIA A800 | 20F3 10DE 17A0 |
'''


class GpuFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pci = self.root / 'pci'
        self.pci.mkdir()
        self.table = self.root / 'supported.md'
        self.table.write_text(SUPPORT_TABLE)

    def device(self, address='0000:01:00.0', vendor='0x10de', device='0x2684',
               kind='0x030000', subvendor='0x10de', subdevice='0x17a0'):
        root = self.pci / address
        root.mkdir()
        for name, text in {'vendor': vendor, 'device': device, 'class': kind,
                           'subsystem_vendor': subvendor, 'subsystem_device': subdevice}.items():
            (root / name).write_text(text + '\n')

    def probe(self, *args):
        return subprocess.run(['python3', str(SCRIPTS / 'lib/nvidia_gpu.py'), '--pci-root', str(self.pci), *args],
                              capture_output=True, text=True)


class NvidiaProbeTests(GpuFixture):

    def test_nvidia_audio_controller_is_not_a_gpu(self):
        self.device(kind='0x040300')
        self.assertEqual(self.probe('--present').returncode, 1)

    def test_modern_gpu_matches_vendor_supported_table(self):
        self.device()
        self.device(address='0000:02:00.0', device='0x2b85', kind='0x030200')
        result = self.probe('--support-table', str(self.table))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mixed_supported_and_legacy_gpu_is_rejected(self):
        self.device()
        self.device(address='0000:02:00.0', device='0x1b06')
        self.assertEqual(self.probe('--support-table', str(self.table)).returncode, 1)

    def test_unknown_model_is_rejected(self):
        self.device(device='0xffff')
        self.assertEqual(self.probe('--support-table', str(self.table)).returncode, 1)

    def test_subsystem_specific_support_is_not_generalized(self):
        self.device(device='0x20f3', subdevice='0xffff')
        self.assertEqual(self.probe('--support-table', str(self.table)).returncode, 1)

    def test_invalid_download_is_not_treated_as_a_supported_list(self):
        self.device()
        self.table.write_text('<html>maintenance</html>')
        result = self.probe('--support-table', str(self.table))
        self.assertEqual(result.returncode, 2)
        self.assertIn('지원 표', result.stderr)


class NvidiaInstallTests(GpuFixture):
    def setUp(self):
        super().setUp()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.root / 'lib').mkdir()
        probe = (SCRIPTS / 'lib/nvidia_gpu.py').read_text().replace('/sys/bus/pci/devices', str(self.pci))
        (self.root / 'lib/nvidia_gpu.py').write_text(probe)
        source = (SCRIPTS / 'setup-nvidia.sh').read_text()
        for before, after in {
            '/var/lib/rocky-setup': str(self.root / 'state'),
            '/proc/sys/kernel/random/boot_id': str(self.root / 'boot-id'),
            '/sys/firmware/efi': str(self.root / 'efi'),
            '/var/log/nvidia-installer.log': str(self.root / 'nvidia-installer.log'),
            '/usr/src/kernels': str(self.root / 'kernels'),
            '/etc/yum.repos.d': str(self.root / 'repos'),
        }.items():
            source = source.replace(before, after)
        self.script = self.root / 'setup-nvidia.sh'
        self.script.write_text(source)
        common = (SCRIPTS / 'common.sh').read_text() + '\nrequire_rocky() { :; }\nadd_repo() { :; }\n'
        (self.root / 'common.sh').write_text(common)
        (self.root / 'efi').mkdir()
        (self.root / 'kernels/6.12-test').mkdir(parents=True)
        (self.root / 'boot-id').write_text('boot-one\n')
        self.packages = self.root / 'packages.json'
        self.packages.write_text('[]')
        self.log = self.root / 'operations'
        dispatcher = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
packages = pathlib.Path(os.environ['TEST_PACKAGES'])
installed = json.loads(packages.read_text())
if name == 'rpm':
    if args[0] == '-qa': print('\\n'.join(installed)); sys.exit(0)
    sys.exit(0 if args[-1] in installed else 1)
if name == 'nvidia-smi': sys.exit(0 if os.environ.get('HEALTHY') == 'yes' else 1)
if name == 'uname': print('x86_64' if args == ['-m'] else '6.12-test'); sys.exit(0)
if name == 'mokutil': print(os.environ.get('SECURE_BOOT', 'SecureBoot disabled')); sys.exit(0)
if name == 'dnf' and args[:2] == ['-q', 'repolist']: print('crb'); sys.exit(0)
with open(os.environ['TEST_OPERATIONS'], 'a') as log: log.write(name + ' ' + ' '.join(args) + '\\n')
if name == 'dnf':
    assert args[:2] == ['install', '-y'], args
    installed.extend(args[2:]); packages.write_text(json.dumps(installed))
    if any(p.startswith('kernel-devel-') and p != 'kernel-devel-matched' for p in args[2:]):
        pathlib.Path(os.environ['TEST_KERNEL_DIR']).mkdir(parents=True, exist_ok=True)
elif name == 'curl':
    target = args[args.index('-o') + 1]
    pathlib.Path(target).write_text(pathlib.Path(os.environ['SUPPORT_TABLE']).read_text())
else: raise AssertionError((name, args))
'''
        for name in ('rpm', 'dnf', 'nvidia-smi', 'uname', 'mokutil', 'curl'):
            path = self.bin / name
            path.write_text(dispatcher)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        TEST_PACKAGES=str(self.packages), TEST_OPERATIONS=str(self.log), SUPPORT_TABLE=str(self.table),
                        TEST_KERNEL_DIR=str(self.root / 'kernels/6.12-test'))

    def apply(self, *args, **env):
        return subprocess.run(['bash', str(self.script), *(args or ('--auto',))],
                              env=dict(self.env, **env), capture_output=True, text=True)

    def test_no_gpu_skips_without_installing_detection_tools(self):
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists())

    def test_working_driver_is_kept_without_package_changes(self):
        self.device()
        result = self.apply(HEALTHY='yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists())

    def test_fresh_driver_waits_for_reboot_and_does_not_reinstall_on_repeat(self):
        self.device()
        first = self.apply()
        self.assertEqual(first.returncode, 10, first.stderr)
        self.assertIn('nvidia-open', json.loads(self.packages.read_text()))
        operations = self.log.read_text()
        second = self.apply()
        self.assertEqual(second.returncode, 10, second.stderr)
        self.assertEqual(self.log.read_text(), operations)
        (self.root / 'boot-id').write_text('boot-two\n')
        ready = self.apply(HEALTHY='yes')
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertFalse((self.root / 'state/nvidia-reboot-required').exists())

    def test_secure_boot_requires_enrollment_instead_of_installing_an_unloadable_driver(self):
        self.device()
        result = self.apply(SECURE_BOOT='SecureBoot enabled')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Secure Boot', result.stderr)
        self.assertNotIn('nvidia-open', json.loads(self.packages.read_text()))

    def test_existing_broken_driver_is_not_replaced(self):
        self.device()
        self.packages.write_text('["nvidia-open"]')
        result = self.apply()
        self.assertEqual(result.returncode, 1)
        self.assertIn('기존 NVIDIA 패키지', result.stderr)
        self.assertFalse(self.log.exists())

    def test_unknown_gpu_is_not_installed_blindly(self):
        self.device(device='0xffff')
        result = self.apply()
        self.assertEqual(result.returncode, 1)
        self.assertIn('지원', result.stderr)
        self.assertNotIn('nvidia-open', json.loads(self.packages.read_text()))

    def test_check_mode_has_no_installation_side_effects(self):
        self.device()
        self.assertEqual(self.apply('--check').returncode, 0)
        self.assertFalse(self.log.exists())

    def test_check_mode_does_not_remove_reboot_state(self):
        self.device()
        pending = self.root / 'state/nvidia-reboot-required'
        pending.parent.mkdir()
        pending.write_text('boot-one\n')
        self.assertEqual(self.apply('--check', HEALTHY='yes').returncode, 0)
        self.assertTrue(pending.exists())

    def test_missing_driver_package_does_not_hide_behind_a_stale_reboot_marker(self):
        self.device()
        pending = self.root / 'state/nvidia-reboot-required'
        pending.parent.mkdir()
        pending.write_text('boot-one\n')
        result = self.apply()
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertIn('nvidia-open', json.loads(self.packages.read_text()))

    def test_old_matched_package_does_not_skip_headers_for_the_running_kernel(self):
        self.device()
        self.packages.write_text('["kernel-devel-matched"]')
        (self.root / 'kernels/6.12-test').rmdir()
        result = self.apply()
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertIn('kernel-devel-6.12-test', json.loads(self.packages.read_text()))


if __name__ == '__main__':
    unittest.main()
