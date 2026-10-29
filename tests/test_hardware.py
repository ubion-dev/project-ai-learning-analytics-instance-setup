from pathlib import Path
import json
import os
import tempfile
import unittest

from ala.hardware import DNF_OPTIONS, MARKER, apply_hardware, inspect_hardware
from ala.system import Command, Host, Paths


KERNEL = "6.12.0-124.15.1.el10_1.x86_64"
BOOT = "9a692999-86db-4415-82d6-8e176e20b1b7"
NEXT_BOOT = "4c7f3da1-bf14-4a28-ad47-b933a26821a4"
MODULES = {f"kernel-modules-{KERNEL}", f"kernel-modules-extra-{KERNEL}"}


class HardwareHost:
    def __init__(self):
        self.packages = set()
        self.calls, self.executed = [], []
        self.replies = {}
        self.code = 0
        self.partial = None

    def run(self, argv, **kwargs):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv in self.replies:
            return self.replies[argv]
        if argv == ("uname", "-r"):
            return Command(0, KERNEL + "\n")
        if argv[:2] == ("rpm", "-q"):
            return Command(0 if argv[2] in self.packages else 1, argv[2])
        return Command(127, "", "명령 없음")

    def execute(self, argv, **kwargs):
        self.executed.append(tuple(argv))
        packages = [value for value in argv[3:] if not value.startswith("--")]
        if self.partial is not None:
            self.packages.update(set(packages) & set(self.partial))
        elif not self.code:
            self.packages.update(packages)
        return self.code


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = Paths(Path(self.tmp.name))
        self.host = HardwareHost()
        for bus in ("pci", "usb"):
            self.paths.at(f"/sys/bus/{bus}/devices").mkdir(parents=True)
        self.write("/proc/sys/kernel/random/boot_id", BOOT)

    def write(self, path, text, mode=0o644):
        value = self.paths.at(path)
        value.parent.mkdir(parents=True, exist_ok=True)
        value.write_text(text)
        value.chmod(mode)
        return value

    def pci(self, vendor="8086", device="1234", kind="020000", driver="e1000e", name="0000:00:01.0"):
        prefix = f"/sys/bus/pci/devices/{name}"
        for filename, value in (("vendor", vendor), ("device", device), ("class", kind)):
            self.write(prefix + "/" + filename, "0x" + value)
        if driver:
            self.paths.at(prefix + "/driver").symlink_to("../../drivers/" + driver)
        return self.paths.at(prefix)

    def usb(self, vendor="0bda", device="1234", kind="ff", driver=None, name="1-2:1.0", subclass="00", protocol="00"):
        prefix = "/sys/bus/usb/devices/"
        self.write(prefix + name + "/bInterfaceClass", kind)
        self.write(prefix + name + "/bInterfaceSubClass", subclass)
        self.write(prefix + name + "/bInterfaceProtocol", protocol)
        address = "usb1" if name == "1-0:1.0" else name.split(":", 1)[0]
        for filename, value in (("idVendor", vendor), ("idProduct", device)):
            self.write(prefix + address + "/" + filename, value)
        if driver:
            self.paths.at(prefix + name + "/driver").symlink_to("../../drivers/" + driver)

    def result(self, results, id):
        return next(r for r in results if r.id == id)

    def states(self, results):
        return {r.id: r.state for r in results}

    def snapshot(self):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.paths.root.rglob("*") if p.is_file()}

    def test_no_devices_is_not_applicable_without_package_or_kernel_queries(self):
        self.assertEqual(self.states(inspect_hardware(self.host, self.paths)), {"hardware": "not_applicable"})
        self.assertEqual(self.host.calls, [])

    def test_inventory_missing_directory_is_unknown_not_no_hardware(self):
        self.paths.at("/sys/bus/usb/devices").rmdir()
        self.assertEqual(self.result(inspect_hardware(self.host, self.paths), "hardware.inventory.usb").state, "unknown")

    def test_inventory_invalid_sysfs_field_is_unknown(self):
        path = self.pci()
        (path / "class").write_text("invalid")
        self.assertEqual(self.result(inspect_hardware(self.host, self.paths), "hardware.inventory.pci.0000:00:01.0").state, "unknown")

    def test_nvidia_display_is_delegated_without_generic_packages(self):
        self.pci(vendor="10de", kind="030000", driver=None)
        results = inspect_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:01.0").state, "not_applicable")
        self.assertEqual(self.host.calls, [])

    def test_normal_bound_network_driver_is_preserved(self):
        self.pci()
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:01.0").state, "ok")
        self.assertEqual(self.host.executed, [])
        self.assertEqual(self.host.calls, [])
        self.assertFalse(self.paths.at(MARKER).exists())

    def test_unbound_storage_and_audio_are_inventory_only(self):
        self.pci(kind="010800", driver=None)
        self.pci(kind="040300", driver=None, name="0000:00:02.0")
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:01.0").state, "not_applicable")
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:02.0").state, "not_applicable")
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.executed, [])

    def test_unbound_usb_multifunction_interfaces_are_inventory_only(self):
        self.usb(kind="01")
        self.usb(kind="03", name="1-2:1.1")
        self.usb(kind="0e", name="1-2:1.2")
        results = apply_hardware(self.host, self.paths)
        self.assertTrue(all(r.state == "not_applicable" for r in results))
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.executed, [])

    def test_passthrough_driver_is_preserved_without_firmware_installation(self):
        self.pci(vendor="10ec", driver="vfio-pci")
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:01.0").state, "ok")
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.executed, [])

    def test_intel_wireless_only_prepares_the_confirmed_firmware_family(self):
        self.pci(kind="028000", driver="iwlwifi")
        results = inspect_hardware(self.host, self.paths)
        packages = [r.id.removeprefix("hardware.package.") for r in results if r.id.startswith("hardware.package.")]
        self.assertEqual(packages, ["iwlwifi-dvm-firmware", "iwlwifi-mvm-firmware"])
        self.assertNotIn("linux-firmware", packages)

    def test_pci_network_firmware_maps_confirmed_vendors(self):
        for vendor, expected in (("10ec", "realtek-firmware"), ("168c", "atheros-firmware"), ("14c3", "mt7xxx-firmware")):
            with self.subTest(vendor=vendor):
                name = "0000:00:0" + str(len(list(self.paths.at('/sys/bus/pci/devices').iterdir())) + 1) + ".0"
                self.pci(vendor=vendor, name=name)
                results = inspect_hardware(self.host, self.paths)
                self.assertEqual(self.result(results, "hardware.package." + expected).state, "change_required")

    def test_usb_vendor_specific_does_not_guess_wifi(self):
        self.usb()
        results = inspect_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.usb.1-2:1.0").state, "not_applicable")
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.executed, [])

    def test_root_usb_hub_does_not_use_a_nonexistent_parent_name(self):
        self.usb(vendor="1d6b", kind="09", driver="hub", name="1-0:1.0")
        results = inspect_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.usb.1-0:1.0").state, "not_applicable")
        self.assertFalse(any(r.state == "unknown" for r in results))

    def test_usb_serial_acm_data_interface_is_not_network_support(self):
        self.usb(kind="02", subclass="02", protocol="01")
        self.usb(kind="0a", name="1-2:1.1")
        results = apply_hardware(self.host, self.paths)
        self.assertTrue(all(r.state == "not_applicable" for r in results))
        self.assertEqual(self.host.calls, [])

    def test_usb_bound_serial_or_bluetooth_driver_is_not_network_evidence(self):
        self.usb(kind="02", subclass="02", protocol="01", driver="cdc_acm")
        self.usb(vendor="0cf3", kind="e0", subclass="01", protocol="01", driver="ath3k", name="1-3:1.0")
        results = apply_hardware(self.host, self.paths)
        self.assertFalse(any(r.id.startswith("hardware.package.") for r in results))
        self.assertEqual(self.host.calls, [])

    def test_confirmed_usb_ethernet_control_does_not_guess_unbound_data_role(self):
        self.usb(kind="02", subclass="06")
        self.usb(kind="0a", name="1-2:1.1")
        results = inspect_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.usb.1-2:1.0").state, "change_required")
        self.assertEqual(self.result(results, "hardware.device.usb.1-2:1.1").state, "not_applicable")
        self.assertEqual(self.result(results, "hardware.package.realtek-firmware").state, "change_required")

    def test_composite_ethernet_does_not_promote_unbound_serial_data(self):
        self.usb(kind="02", subclass="06", driver="cdc_ether")
        self.usb(kind="0a", driver="cdc_ether", name="1-2:1.1")
        self.usb(kind="02", subclass="02", protocol="01", driver="cdc_acm", name="1-2:1.2")
        self.usb(kind="0a", name="1-2:1.3")
        self.host.packages.add("realtek-firmware")
        self.host.packages.update(MODULES)
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.usb.1-2:1.0").state, "ok")
        self.assertEqual(self.result(results, "hardware.device.usb.1-2:1.1").state, "ok")
        self.assertEqual(self.result(results, "hardware.device.usb.1-2:1.2").state, "ok")
        self.assertEqual(self.result(results, "hardware.device.usb.1-2:1.3").state, "not_applicable")
        self.assertFalse(any(r.state in {"blocked", "unknown", "error"} for r in results))
        self.assertEqual(self.host.executed, [])

    def test_usb_bluetooth_is_not_guessed_as_wireless_network(self):
        self.usb(vendor="0e8d", kind="e0", subclass="01", protocol="01")
        results = apply_hardware(self.host, self.paths)
        self.assertTrue(all(r.state == "not_applicable" for r in results))
        self.assertEqual(self.host.calls, [])

    def test_usb_known_network_driver_uses_its_firmware_family(self):
        self.usb(driver="r8152")
        results = inspect_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.package.realtek-firmware").state, "change_required")
        self.assertEqual(self.result(results, "hardware.device.usb.1-2:1.0").actual["driver"], "r8152")

    def test_broadcom_only_matches_known_brcmfmac_driver(self):
        self.pci(vendor="14e4", driver="bnxt_en")
        self.assertFalse(any(r.id == "hardware.package.brcmfmac-firmware" for r in inspect_hardware(self.host, self.paths)))
        (self.paths.at("/sys/bus/pci/devices/0000:00:01.0") / "driver").unlink()
        self.paths.at("/sys/bus/pci/devices/0000:00:01.0/driver").symlink_to("../../drivers/brcmfmac")
        self.assertEqual(self.result(inspect_hardware(self.host, self.paths), "hardware.package.brcmfmac-firmware").state, "change_required")

    def test_plan_is_read_only_and_does_not_run_dnf(self):
        self.pci(kind="028000", driver="iwlwifi")
        before = self.snapshot()
        inspect_hardware(self.host, self.paths)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.host.executed, [])
        self.assertFalse(any(call[0] == "dnf" for call in self.host.calls))

    def test_unbound_prepares_exact_running_kernel_modules_and_waits(self):
        self.pci(driver=None)
        results = apply_hardware(self.host, self.paths)
        command = self.host.executed[0]
        self.assertEqual(set(command[-2:]), MODULES)
        for option in DNF_OPTIONS:
            self.assertIn(option, command)
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:01.0").state, "pending")
        self.assertEqual(self.result(results, "hardware.reboot").state, "pending")
        value = json.loads(self.paths.at(MARKER).read_text())
        self.assertEqual(value["kernel_release"], KERNEL)
        self.assertEqual(value["boot_id"], BOOT)
        self.assertEqual(self.paths.at(MARKER).stat().st_mode & 0o777, 0o600)

    def test_second_apply_keeps_installed_packages_and_marker_mtime(self):
        self.pci(kind="028000", driver="iwlwifi")
        apply_hardware(self.host, self.paths)
        before = self.snapshot()
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(len(self.host.executed), 1)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.result(results, "hardware.reboot").state, "pending")

    def test_next_boot_rechecks_binding_and_keeps_readonly_marker(self):
        path = self.pci(driver=None)
        apply_hardware(self.host, self.paths)
        self.write("/proc/sys/kernel/random/boot_id", NEXT_BOOT)
        (path / "driver").symlink_to("../../drivers/e1000e")
        before = self.snapshot()
        results = inspect_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.reboot").state, "ok")
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:01.0").state, "ok")
        self.assertEqual(before, self.snapshot())

    def test_next_boot_still_unbound_is_actionable_not_success(self):
        self.pci(driver=None)
        apply_hardware(self.host, self.paths)
        self.write("/proc/sys/kernel/random/boot_id", NEXT_BOOT)
        results = inspect_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:01.0").state, "blocked")
        self.assertIn("blacklist", self.result(results, "hardware.device.pci.0000:00:01.0").next_action)

    def test_existing_override_never_installs_or_rebinds(self):
        self.pci(driver=None)
        self.write("/sys/bus/pci/devices/0000:00:01.0/driver_override", "vfio-pci")
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:01.0").state, "blocked")
        self.assertEqual(self.host.executed, [])

    def test_complete_module_packages_with_unbound_device_are_not_reinstalled(self):
        self.pci(driver=None)
        self.host.packages.update(MODULES)
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.host.executed, [])
        self.assertEqual(self.result(results, "hardware.device.pci.0000:00:01.0").state, "blocked")

    def test_malicious_kernel_release_is_rejected_before_dnf(self):
        self.pci(driver=None)
        self.host.replies[("uname", "-r")] = Command(0, "$(touch /tmp/bad).x86_64")
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.kernel").state, "unknown")
        self.assertEqual(self.host.executed, [])

    def test_missing_kernel_command_is_unknown(self):
        self.pci(driver=None)
        self.host.replies[("uname", "-r")] = Command(127, "", "missing")
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.kernel").state, "unknown")
        self.assertEqual(self.host.executed, [])

    def test_rpm_query_error_is_unknown(self):
        self.pci(vendor="10ec")
        self.host.replies[("rpm", "-q", "realtek-firmware")] = Command(126, "", "permission denied")
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.package.realtek-firmware").state, "unknown")
        self.assertEqual(self.host.executed, [])

    def test_failed_dnf_reports_error_without_fake_success_marker(self):
        self.pci(driver=None)
        self.host.code = 1
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.apply").state, "error")
        self.assertFalse(self.paths.at(MARKER).exists())

    def test_partial_transaction_records_what_was_installed_and_stays_failed(self):
        self.pci(driver=None)
        self.host.code = 1
        self.host.partial = [f"kernel-modules-{KERNEL}"]
        results = apply_hardware(self.host, self.paths)
        self.assertEqual(self.result(results, "hardware.apply").state, "error")
        self.assertEqual(json.loads(self.paths.at(MARKER).read_text())["packages"], self.host.partial)

    def test_apply_rechecks_state_instead_of_using_previous_plan(self):
        self.pci(vendor="10ec")
        inspect_hardware(self.host, self.paths)
        self.host.packages.add("realtek-firmware")
        apply_hardware(self.host, self.paths)
        self.assertEqual(self.host.executed, [])

    def test_invalid_or_symlinked_marker_blocks_changes(self):
        self.pci(vendor="10ec")
        marker = self.write(MARKER, "{}", 0o600)
        self.assertEqual(self.result(apply_hardware(self.host, self.paths), "hardware.reboot").state, "blocked")
        marker.unlink()
        marker.symlink_to("/tmp/not-ala-managed")
        self.assertEqual(self.result(apply_hardware(self.host, self.paths), "hardware.reboot").state, "blocked")
        self.assertEqual(self.host.executed, [])

    def test_real_host_cannot_mutate_through_test_paths(self):
        results = apply_hardware(Host(), self.paths)
        self.assertEqual(self.result(results, "hardware.apply").state, "blocked")


if __name__ == "__main__":
    unittest.main()
