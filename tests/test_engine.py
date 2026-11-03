from pathlib import Path
import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from ala.config import Config
from ala.engine import BASE_PACKAGES, DOCKER_PACKAGES, Engine, SCRIPTS, SLEEP_UNITS, UPDATE_UNITS, _template
from ala.model import CheckResult, Report
from ala.system import Command, Paths


# One synthetic clock drives boot time, service starts, and fixture file mtimes.
# Wall-clock writes can cross the engine's one-second restart-age tolerance on CI.
FAKE_BOOT_SECONDS = 1_700_000_000
FAKE_SERVICE_START_US = 1_000_000_000
FAKE_SERVICE_START_NS = (FAKE_BOOT_SECONDS * 1_000_000_000 + FAKE_SERVICE_START_US * 1000)
FAKE_FILE_START_NS = (FAKE_BOOT_SECONDS + 100) * 1_000_000_000


class FakeHost:
    def __init__(self):
        self.packages = set(BASE_PACKAGES) | set(DOCKER_PACKAGES) | {"bash", "openssh-server", "google-chrome-stable"}
        self.calls = []
        self.executed = []
        self.replies = {}
        self.on_execute = None
        self.execution_code = 0

    def run(self, argv, **kwargs):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv in self.replies:
            return self.replies[argv]
        if argv[:2] == ("rpm", "-q"):
            return Command(0 if argv[2] in self.packages else 1, argv[2] if argv[2] in self.packages else "未설치")
        if argv[:2] == ("rpm", "-qa"):
            return Command(0, "\n".join(self.packages))
        if argv[:2] == ("getent", "passwd"):
            uid = 0 if argv[2] == "root" else os.geteuid()
            # The synthetic regular user must have a nonzero UID even under root CI.
            uid = uid if uid or argv[2] == "root" else 1000
            return Command(0, f"{argv[2]}:x:{uid}:1000::/home/{argv[2]}:/bin/bash\n")
        if argv[:2] == ("systemctl", "is-enabled"):
            return Command(1, "masked\n") if argv[2] in UPDATE_UNITS + SLEEP_UNITS else Command(0, "enabled\n")
        if argv[:2] == ("systemctl", "is-active"):
            return Command(3, "inactive\n") if argv[2] in UPDATE_UNITS else Command(0, "active\n")
        if argv[:2] == ("timedatectl", "show"):
            return Command(0, "Asia/Seoul\n")
        if argv == ("gsettings", "list-schemas"):
            return Command(0, "\n".join(("org.gnome.desktop.session", "org.gnome.desktop.screensaver", "org.gnome.settings-daemon.plugins.power")))
        if argv[:2] == ("systemctl", "show") and "--property=ExecMainStartTimestampMonotonic" in argv:
            return Command(0, f"{FAKE_SERVICE_START_US}\n")
        if argv == ("/usr/sbin/sshd", "-T"):
            return Command(0, "port 22\nport 2222\n")
        if argv[:2] == ("firewall-cmd", "--get-default-zone"):
            return Command(0, "public\n")
        if argv and argv[0] == "firewall-cmd" and any(v.startswith("--query-port=") for v in argv):
            return Command(0, "yes\n")
        if argv[:2] == ("ip", "route"):
            return Command(0, "")
        if argv[:2] == ("git", "-C"):
            return Command(0, "a" * 40 + "\n")
        if argv and argv[0] == "runuser" and "git" in argv:
            return Command(0, "a" * 40 + "\n")
        if argv == ("docker", "info", "--format", "{{json .LoggingDriver}}"):
            return Command(0, '"local"\n')
        if argv == ("docker", "info", "--format", "{{json .Runtimes}}"):
            return Command(0, '{"runc":{},"nvidia":{}}')
        if argv in (("docker", "info"), ("docker", "compose", "version"), ("/usr/sbin/sshd", "-t"), ("google-chrome-stable", "--version")) or argv[:2] == ("bash", "-n"):
            return Command(0, "ok")
        return Command(127, "", "명령 없음: " + " ".join(argv))

    def execute(self, argv, **kwargs):
        self.executed.append(tuple(argv))
        if self.on_execute:
            self.on_execute(argv)
        return self.execution_code


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = Paths(Path(self.tmp.name))
        self.host = FakeHost()
        self.file_time_ns = FAKE_FILE_START_NS
        self.config = Config(user="user")
        self.engine = Engine(self.config, self.host, self.paths)
        self.addCleanup(patch.stopall)
        patch("ala.engine.platform_error", return_value=None).start()
        patch.object(Engine, "_extra_display", return_value=[CheckResult("chrome.integration", "ok")]).start()
        self.prepare()

    def write(self, filename, content, mode=0o644):
        path = self.paths.at(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        self.file_time_ns += 10_000_000
        os.utime(path, ns=(self.file_time_ns, self.file_time_ns))
        return path

    def prepare(self):
        self.write("/proc/stat", f"btime {FAKE_BOOT_SECONDS}\n")
        self.paths.at("/sys/bus/pci/devices").mkdir(parents=True)
        self.paths.at("/sys/bus/usb/devices").mkdir(parents=True)
        self.paths.at("/sys/class/net").mkdir(parents=True)
        self.write("/usr/local/bin/chrome-did", (SCRIPTS / "bin/chrome-did").read_text(), 0o755)
        policy = self.write("/etc/opt/chrome/policies/managed/did-display.json", json.dumps({key: False for key in ("TranslateEnabled", "DefaultBrowserSettingEnabled", "PromotionsEnabled")}))
        policy.parent.chmod(0o755)
        policy.parent.parent.chmod(0o755)
        self.write("/etc/docker/daemon.json", '{"log-driver":"local","log-opts":{"max-size":"20m","max-file":"5"}}')
        self.write("/home/user/.oh-my-bash/oh-my-bash.sh", "# fake upstream")
        self.write("/home/user/.config/server-bash/managed", "")
        self.write("/home/user/.config/server-bash/oh-my-bash.commit", "a" * 40 + "\n", 0o600)
        for filename, label in (("/home/user/.config/server-bash/init.bash", "INIT"), ("/home/user/.config/server-bash/server.theme.sh", "THEME"), ("/home/user/.oh-my-bash/custom/themes/server/server.theme.sh", "THEME")):
            self.write(filename, _template("setup-bash.sh", label), 0o600)
        self.write("/home/user/.bashrc", "# custom retained\n" + _template("setup-bash.sh", "HOOK"), 0o600)
        self.write("/home/user/.bash_profile", "source ~/.bashrc\n")
        for name, lock, script in (("00-display", "display", "setup-display.sh"), ("10-updates", "updates", "setup-update-policy.sh")):
            self.write("/etc/dconf/db/rocky_setup.d/" + name, _template(script, "POLICY"))
            self.write("/etc/dconf/db/rocky_setup.d/locks/" + lock, _template(script, "LOCKS"))
        self.write("/etc/dconf/profile/user", "user-db:user\nsystem-db:rocky_setup\n")
        self.write("/etc/dconf/db/rocky_setup", "fake compiled DB")
        self.write("/etc/systemd/logind.conf.d/60-rocky-kiosk.conf", _template("setup-display.sh", "LOGIND"))
        if os.geteuid() == 0:
            for path in self.paths.at("/home/user").rglob("*"):
                path.chown(1000, 1000)

    def by_id(self, report, id):
        return next(r for r in report.results if r.id == id)

    def gpu(self):
        for name, value in (("vendor", "0x10de"), ("class", "0x030000"), ("device", "0x2684"), ("subsystem_vendor", "0x10de"), ("subsystem_device", "0x0001")):
            self.write("/sys/bus/pci/devices/0000:01:00.0/" + name, value)

    def track_display_apply(self, state="ok"):
        applied = []
        def display(apply=False):
            if apply:
                applied.append(True)
                return [CheckResult("chrome.apply_only", state, actual="synthetic native result")]
            return [CheckResult("chrome.integration", "ok")]
        patch.object(self.engine, "_extra_display", side_effect=display).start()
        return applied

    def test_complete_state_and_plan_do_not_mutate_files_or_execute(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.paths.root.rglob("*") if p.is_file()}
        report = self.engine.inspect()
        self.assertEqual(report.exit_code, 0, [(r.id, r.state) for r in report.results if r.state != "ok"])
        self.assertEqual(self.engine.plan().to_dict(), report.to_dict())
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.paths.root.rglob("*") if p.is_file()})
        self.assertEqual(self.host.executed, [])
        self.assertFalse(any(any(part in {"start", "stop", "enable", "mask", "install"} for part in call) for call in self.host.calls))

    def test_only_changed_component_applies_and_rechecks(self):
        self.host.packages.remove("curl")
        def execute(argv):
            self.assertEqual(Path(argv[1]).name, "setup-base.sh")
            self.host.packages.add("curl")
        self.host.on_execute = execute
        stages = []
        report = self.engine.apply(lambda *args: stages.append(args))
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(len(self.host.executed), 1)
        self.assertIn(("base", "started"), stages)
        self.assertIn(("base", "completed"), stages)
        self.engine.apply()
        self.assertEqual(len(self.host.executed), 1)

    def test_hardware_pending_continues_other_stages_without_shell_runner(self):
        state = "change_required"

        def hardware(apply=False):
            nonlocal state
            if apply:
                state = "pending"
            return [CheckResult("hardware.reboot", state)]

        stages = []
        with patch.object(self.engine, "_hardware", side_effect=hardware):
            report = self.engine.apply(lambda *args: stages.append(args))
        self.assertEqual(report.exit_code, 2)
        self.assertIn(("hardware", "pending"), stages)
        self.assertIn(("chrome", "completed"), stages)
        self.assertEqual(self.host.executed, [])

    def test_hardware_install_failure_remains_in_final_report(self):
        def hardware(apply=False):
            return [CheckResult("hardware.apply", "error", actual="DNF failed")] if apply else [CheckResult("hardware.package.firmware", "change_required")]

        stages = []
        with patch.object(self.engine, "_hardware", side_effect=hardware):
            report = self.engine.apply(lambda *args: stages.append(args))
        self.assertEqual(self.by_id(report, "hardware.apply").state, "error")
        self.assertIn(("hardware", "failed"), stages)
        self.assertNotIn(("chrome", "started"), stages)

    def test_unresolved_hardware_is_reported_while_independent_setup_continues(self):
        stages = []
        with patch.object(self.engine, "_hardware", return_value=[CheckResult("hardware.device.pci.example", "blocked")]):
            report = self.engine.apply(lambda *args: stages.append(args))
        self.assertEqual(report.exit_code, 1)
        self.assertIn(("hardware", "blocked"), stages)
        self.assertIn(("chrome", "completed"), stages)
        self.assertEqual(self.host.executed, [])

    def test_new_hardware_diagnostic_after_install_does_not_stop_independent_setup(self):
        state = "change_required"

        def hardware(apply=False):
            nonlocal state
            if apply:
                state = "unknown"
            return [CheckResult("hardware.device.usb.hotplug", state)]

        stages = []
        with patch.object(self.engine, "_hardware", side_effect=hardware):
            report = self.engine.apply(lambda *args: stages.append(args))
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(self.by_id(report, "hardware.device.usb.hotplug").state, "unknown")
        self.assertIn(("hardware", "blocked"), stages)
        self.assertIn(("chrome", "completed"), stages)

    def test_inspection_continues_after_command_failure(self):
        self.host.replies[("timedatectl", "show", "--property=Timezone", "--value")] = Command(1, "", "permission denied")
        report = self.engine.inspect()
        self.assertEqual(self.by_id(report, "base.timezone").state, "unknown")
        self.assertEqual(self.by_id(report, "gpu").state, "not_applicable")
        self.assertEqual(self.by_id(report, "display.logind").state, "ok")

    def test_runtime_only_enabled_is_configuration_drift(self):
        self.host.replies[("systemctl", "is-enabled", "docker")] = Command(0, "enabled-runtime")
        self.assertEqual(self.by_id(self.engine.inspect(), "docker.service.docker.enabled").state, "change_required")

    def test_actual_ssh_ports_are_checked_in_both_scopes(self):
        self.host.replies[("firewall-cmd", "--permanent", "--zone=public", "--query-port=2222/tcp")] = Command(1, "no")
        report = self.engine.inspect()
        self.assertEqual(self.by_id(report, "ssh.firewall.public.2222.permanent").state, "change_required")
        self.assertEqual(self.by_id(report, "ssh.firewall.public.2222.runtime").state, "ok")

    def test_docker_conflict_does_not_remove_packages(self):
        self.host.packages.add("podman")
        report = self.engine.apply()
        self.assertEqual(self.by_id(report, "docker.conflict.podman").state, "blocked")
        self.assertEqual(self.host.executed, [])

    def test_display_registration_precedes_docker_and_gpu_once_per_apply(self):
        applied = self.track_display_apply()
        for attempt in range(2):
            stages = []
            report = self.engine.apply(lambda *args: stages.append(args))
            self.assertEqual(report.exit_code, 0)
            self.assertEqual(applied, [True] * (attempt + 1))
            self.assertEqual(stages.count(("chrome", "started")), 1)
            self.assertEqual(stages.count(("chrome", "completed")), 1)
            self.assertLess(stages.index(("ssh", "kept")), stages.index(("display", "kept")))
            self.assertLess(stages.index(("display", "kept")), stages.index(("chrome", "started")))
            self.assertLess(stages.index(("chrome", "completed")), stages.index(("docker", "kept")))
            self.assertLess(stages.index(("chrome", "completed")), stages.index(("gpu", "kept")))
            self.assertEqual(sum(r.id == "chrome.apply_only" for r in report.results), 1)
        self.assertEqual(self.host.executed, [])

    def test_docker_blocker_keeps_earlier_chrome_registration_result(self):
        applied = self.track_display_apply()
        self.host.packages.add("podman")
        stages = []
        report = self.engine.apply(lambda *args: stages.append(args))
        self.assertEqual(applied, [True])
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(self.by_id(report, "docker.conflict.podman").state, "blocked")
        self.assertEqual(self.by_id(report, "chrome.apply_only").state, "ok")
        self.assertLess(stages.index(("chrome", "completed")), stages.index(("docker", "blocked")))
        self.assertEqual(self.host.executed, [])

    def test_gpu_blocker_keeps_earlier_chrome_registration_result(self):
        applied = self.track_display_apply()
        stages = []
        with patch.object(self.engine, "_gpu", return_value=[CheckResult("gpu.driver", "blocked")]):
            report = self.engine.apply(lambda *args: stages.append(args))
        self.assertEqual(applied, [True])
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(self.by_id(report, "gpu.driver").state, "blocked")
        self.assertEqual(self.by_id(report, "chrome.apply_only").state, "ok")
        self.assertLess(stages.index(("chrome", "completed")), stages.index(("gpu", "blocked")))
        self.assertEqual(self.host.executed, [])

    def test_chrome_native_error_is_preserved_while_later_stages_continue(self):
        applied = self.track_display_apply("error")
        stages = []
        report = self.engine.apply(lambda *args: stages.append(args))
        self.assertEqual(applied, [True])
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(self.by_id(report, "chrome.apply_only").state, "error")
        self.assertIn(("chrome", "failed"), stages)
        self.assertIn(("docker", "kept"), stages)
        self.assertIn(("gpu", "kept"), stages)

    def test_chrome_event_distinguishes_blockers_from_application_wait(self):
        for state, event, code in (("blocked", "blocked", 1), ("unknown", "blocked", 1),
                                   ("change_required", "blocked", 1), ("pending", "pending", 2),
                                   ("not_applicable", "completed", 0)):
            with self.subTest(state=state):
                with patch.object(self.engine, "_extra_display", side_effect=lambda apply=False: [
                        CheckResult("chrome.apply_only", state)] if apply else [CheckResult("chrome.integration", "ok")]):
                    stages = []
                    report = self.engine.apply(lambda *args: stages.append(args))
                self.assertEqual(report.exit_code, code)
                self.assertIn(("chrome", event), stages)
                self.assertEqual(self.by_id(report, "chrome.apply_only").state, state)
                self.assertIn(("docker", "kept"), stages)
                self.assertIn(("gpu", "kept"), stages)

    def test_chrome_error_event_has_priority_over_blocked_and_pending(self):
        outcome = [CheckResult("chrome.apply.wait", "pending"), CheckResult("chrome.apply.block", "blocked"),
                   CheckResult("chrome.apply.error", "error")]
        with patch.object(self.engine, "_extra_display", side_effect=lambda apply=False: outcome if apply else [CheckResult("chrome.integration", "ok")]):
            stages = []
            report = self.engine.apply(lambda *args: stages.append(args))
        self.assertEqual(report.exit_code, 1)
        self.assertIn(("chrome", "failed"), stages)
        self.assertNotIn(("chrome", "blocked"), stages)
        self.assertNotIn(("chrome", "pending"), stages)

    def test_chrome_result_survives_later_module_failure_and_unresolved_install(self):
        applied = self.track_display_apply("pending")
        self.host.packages.remove("docker-ce")
        for code in (7, 0):
            with self.subTest(module_code=code):
                self.host.execution_code = code
                report = self.engine.apply()
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(self.by_id(report, "chrome.apply_only").state, "pending")
                self.assertEqual(self.by_id(report, "docker.package.docker-ce").state, "change_required")
                if code:
                    self.assertEqual(self.by_id(report, "docker.apply").actual, code)
        self.assertEqual(applied, [True, True])
        self.assertEqual([Path(argv[1]).name for argv in self.host.executed], ["setup-docker.sh"] * 2)

    def test_chrome_result_survives_gpu_container_install_failure(self):
        applied = self.track_display_apply("error")
        self.gpu()
        self.host.replies[("nvidia-smi",)] = Command(0, "GPU ready")
        self.host.replies[("docker", "info", "--format", "{{json .Runtimes}}" )] = Command(0, '{"runc":{}}')
        def execute(argv):
            self.host.execution_code = 6 if Path(argv[1]).name == "setup-nvidia-container.sh" else 0
        self.host.on_execute = execute
        report = self.engine.apply()
        self.assertEqual(applied, [True])
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(self.by_id(report, "chrome.apply_only").state, "error")
        self.assertEqual(self.by_id(report, "gpu_container.apply").actual, 6)

    def test_pre_display_blockers_still_prevent_native_registration(self):
        applied = self.track_display_apply()
        for stage in ("base", "updates", "shell", "ssh", "display_policy"):
            with self.subTest(stage=stage):
                with patch.object(self.engine, "_" + stage, return_value=[CheckResult(stage + ".probe", "blocked")]):
                    report = self.engine.apply()
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(applied, [])
                self.assertEqual(self.host.executed, [])

    def test_display_policy_pending_still_prepares_chrome(self):
        applied = self.track_display_apply()
        with patch.object(self.engine, "_display_policy", return_value=[CheckResult("display.policy", "pending")]):
            report = self.engine.apply()
        self.assertEqual(applied, [True])
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(self.by_id(report, "chrome.apply_only").state, "ok")

    def test_invalid_docker_json_is_not_overwritten(self):
        path = self.write("/etc/docker/daemon.json", "[]")
        report = self.engine.apply()
        self.assertEqual(self.by_id(report, "docker.config").state, "blocked")
        self.assertEqual(path.read_text(), "[]")
        self.assertEqual(self.host.executed, [])

    def test_policy_external_conflict_is_blocked_and_no_write(self):
        self.write("/etc/opt/chrome/policies/managed/other", '{"TranslateEnabled":true}')
        self.assertEqual(self.by_id(self.engine.inspect(), "base.chrome.policy.TranslateEnabled").state, "blocked")
        self.engine.apply()
        self.assertEqual(self.host.executed, [])

    def test_dconf_lock_and_compiled_freshness_are_checked(self):
        lock = self.write("/etc/dconf/db/rocky_setup.d/locks/display", "")
        report = self.engine.inspect()
        self.assertEqual(self.by_id(report, "display.dconf.locks").state, "change_required")
        self.assertEqual(self.by_id(report, "display.dconf.compiled").state, "change_required")

    def test_active_package_transaction_is_blocked_not_stopped(self):
        self.host.replies[("systemctl", "is-active", "dnf-automatic.service")] = Command(0, "active")
        report = self.engine.apply()
        self.assertEqual(self.by_id(report, "update.transaction.dnf-automatic.service").state, "blocked")
        self.assertEqual(self.host.executed, [])

    def test_shell_unmanaged_install_is_not_replaced(self):
        self.paths.at("/home/user/.config/server-bash/managed").unlink()
        report = self.engine.apply()
        self.assertEqual(self.by_id(report, "shell.user.ownership").state, "blocked")
        self.assertEqual(self.host.executed, [])

    def test_masked_active_packagekit_is_pending(self):
        self.host.replies[("systemctl", "is-active", "packagekit.service")] = Command(0, "active")
        self.assertEqual(self.engine.inspect().exit_code, 2)

    def test_gpu_present_same_boot_is_pending_without_reinstall(self):
        self.gpu()
        self.host.packages.add("nvidia-open")
        self.write("/proc/sys/kernel/random/boot_id", "boot-A")
        self.write("/var/lib/rocky-setup/nvidia-reboot-required", "boot-A")
        report = self.engine.apply()
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(self.by_id(report, "gpu.driver").state, "pending")
        self.assertEqual(self.host.executed, [])

    def test_gpu_failure_after_reboot_does_not_reinstall(self):
        self.gpu()
        self.host.packages.add("nvidia-open")
        self.write("/proc/sys/kernel/random/boot_id", "boot-B")
        self.write("/var/lib/rocky-setup/nvidia-reboot-required", "boot-A")
        report = self.engine.apply()
        self.assertEqual(self.by_id(report, "gpu.driver").state, "blocked")
        self.assertEqual(self.host.executed, [])

    def test_gpu_support_network_error_is_unknown_not_unsupported(self):
        self.gpu()
        report = self.engine.inspect()
        self.assertEqual(self.by_id(report, "gpu.support").state, "unknown")
        self.assertFalse(any(r.state == "blocked" and r.id == "gpu.support" for r in report.results))

    def test_gpu_runtime_saved_but_not_live_is_pending(self):
        self.gpu()
        self.host.replies[("nvidia-smi",)] = Command(0, "GPU ready")
        self.host.packages.add("nvidia-container-toolkit")
        self.write("/etc/docker/daemon.json", '{"log-driver":"local","runtimes":{"nvidia":{}}}')
        self.host.replies[("docker", "info", "--format", "{{json .Runtimes}}" )] = Command(0, '{"runc":{}}')
        report = self.engine.apply()
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(self.by_id(report, "gpu.runtime.live").state, "pending")
        self.assertEqual(self.host.executed, [])

    def test_fresh_gpu_exit10_remains_pending_not_success(self):
        self.gpu()
        self.host.replies[("curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "10", "https://raw.githubusercontent.com/NVIDIA/open-gpu-kernel-modules/main/README.md")] = Command(0, "| GPU | 2684 | supported |")
        self.host.execution_code = 10
        def installed(argv):
            self.assertEqual(Path(argv[1]).name, "setup-nvidia.sh")
            self.host.packages.add("nvidia-open")
            self.write("/proc/sys/kernel/random/boot_id", "boot-A")
            self.write("/var/lib/rocky-setup/nvidia-reboot-required", "boot-A")
        self.host.on_execute = installed
        report = self.engine.apply()
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(self.by_id(report, "gpu.driver").state, "pending")
        self.assertEqual(len(self.host.executed), 1)

    def test_working_gpu_installs_missing_container_toolkit(self):
        self.gpu()
        self.host.replies[("nvidia-smi",)] = Command(0, "GPU ready")
        self.host.replies[("docker", "info", "--format", "{{json .Runtimes}}" )] = Command(0, '{"runc":{}}')
        def installed(argv):
            if Path(argv[1]).name == "setup-nvidia-container.sh":
                self.host.packages.add("nvidia-container-toolkit")
                self.write("/etc/docker/daemon.json", '{"log-driver":"local","runtimes":{"nvidia":{}}}')
                self.host.replies[("docker", "info", "--format", "{{json .Runtimes}}" )] = Command(0, '{"nvidia":{}}')
        self.host.on_execute = installed
        report = self.engine.apply()
        self.assertEqual(report.exit_code, 0)
        self.assertIn("setup-nvidia-container.sh", [Path(c[1]).name for c in self.host.executed])

    def test_stopped_docker_can_be_started_before_runtime_query(self):
        self.host.replies[("systemctl", "is-active", "docker")] = Command(3, "inactive")
        def installed(argv):
            self.assertEqual(Path(argv[1]).name, "setup-docker.sh")
            self.host.replies.pop(("systemctl", "is-active", "docker"))
        self.host.on_execute = installed
        self.assertEqual(self.engine.apply().exit_code, 0)

    def test_stopped_firewall_is_not_an_unknown_query_blocker(self):
        self.host.replies[("systemctl", "is-active", "firewalld")] = Command(3, "inactive")
        def installed(argv):
            self.assertEqual(Path(argv[1]).name, "setup-ssh.sh")
            self.host.replies.pop(("systemctl", "is-active", "firewalld"))
        self.host.on_execute = installed
        self.assertEqual(self.engine.apply().exit_code, 0)

    def test_changed_docker_options_are_pending_until_restart(self):
        path = self.paths.at("/etc/docker/daemon.json")
        changed_ns = FAKE_SERVICE_START_NS + 100 * 1_000_000_000
        os.utime(path, ns=(changed_ns, changed_ns))
        report = self.engine.inspect()
        self.assertEqual(self.by_id(report, "docker.runtime.config_age").state, "pending")
        self.assertEqual(report.exit_code, 2)

    def test_fixture_writes_use_the_same_clock_as_service_start(self):
        path = self.write("/etc/docker/daemon.json", self.paths.at("/etc/docker/daemon.json").read_text())
        self.assertEqual(path.stat().st_mtime_ns, self.file_time_ns)
        self.assertGreater(path.stat().st_mtime_ns, FAKE_FILE_START_NS)
        self.assertLess(path.stat().st_mtime_ns, FAKE_SERVICE_START_NS)
        self.assertEqual(self.engine.inspect().exit_code, 0)

    def test_runtime_age_boundary_is_deterministic(self):
        path = self.paths.at("/etc/docker/daemon.json")
        for offset_seconds, expected in ((-1, "ok"), (1, "ok"), (2, "pending")):
            with self.subTest(offset_seconds=offset_seconds):
                timestamp_ns = FAKE_SERVICE_START_NS + offset_seconds * 1_000_000_000
                os.utime(path, ns=(timestamp_ns, timestamp_ns))
                result = self.engine._restart_pending("docker.runtime.config_age", "docker.service", "/etc/docker/daemon.json")
                self.assertEqual(result.state, expected)

    def test_unknown_user_does_not_apply(self):
        self.host.replies[("getent", "passwd", "user")] = Command(2, "", "unknown user")
        self.assertEqual(self.engine.apply().exit_code, 1)
        self.assertEqual(self.host.executed, [])

    def test_wrong_platform_blocks_before_host_commands(self):
        with patch("ala.engine.platform_error", return_value="Rocky Linux 10 전용"):
            report = self.engine.inspect()
            self.assertEqual(report.exit_code, 1)
            self.assertEqual(self.host.calls, [])

    def test_shell_revision_is_queried_as_repository_owner(self):
        self.engine.inspect()
        git = [call for call in self.host.calls if "git" in call and "rev-parse" in call]
        self.assertEqual(len(git), 1)
        self.assertEqual(git[0][:6], ("runuser", "-u", "user", "--", "env", "HOME=/home/user"))
        self.assertFalse(any(call[:2] == ("git", "-C") for call in self.host.calls))

    def test_mask_permission_denial_is_unknown(self):
        self.host.replies[("systemctl", "is-enabled", "packagekit.service")] = Command(1, "", "Access denied")
        report = self.engine.inspect()
        self.assertEqual(self.by_id(report, "update.mask.packagekit.service").state, "unknown")

    def test_service_account_is_blocked_before_apply(self):
        self.host.replies[("getent", "passwd", "user")] = Command(0, "user:x:1000:1000::/home/user:/sbin/nologin")
        report = self.engine.apply()
        self.assertEqual(self.by_id(report, "user").state, "blocked")
        self.assertEqual(self.host.executed, [])


if __name__ == "__main__":
    unittest.main()
