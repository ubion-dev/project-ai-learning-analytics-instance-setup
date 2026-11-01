"""Read PCI devices and match NVIDIA's open-module support table (no system writes)."""
import argparse
from pathlib import Path
import re
import sys


def gpu_devices(root):
    if not root.is_dir():
        raise ValueError('PCI 장치 정보를 읽을 수 없습니다.')
    devices = []
    for path in sorted(root.iterdir()):
        if int((path / 'vendor').read_text().strip(), 16) != 0x10DE:
            continue
        if int((path / 'class').read_text().strip(), 16) >> 16 != 3:
            continue
        devices.append((path.name, tuple(int((path / name).read_text().strip(), 16)
                                        for name in ('device', 'subsystem_vendor', 'subsystem_device'))))
    return devices


def supported_ids(text):
    entries = set()
    for line in text.splitlines():
        columns = line.split('|')
        if len(columns) < 4:
            continue
        value = columns[2].strip()
        if re.fullmatch(r'[0-9A-Fa-f]{4}(?:\s+[0-9A-Fa-f]{4}){0,2}', value):
            ids = tuple(int(v, 16) for v in value.split())
            if len(ids) in (1, 3):
                entries.add(ids)
    if not entries:
        raise ValueError('NVIDIA 지원 표를 해석할 수 없습니다.')
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pci-root', type=Path, default=Path('/sys/bus/pci/devices'))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--present', action='store_true')
    mode.add_argument('--support-table', type=Path)
    args = parser.parse_args()
    try:
        devices = gpu_devices(args.pci_root)
        if not devices:
            return 1
        if args.present:
            return 0
        entries = supported_ids(args.support_table.read_text())
        unsupported = []
        for address, ids in devices:
            print(f'NVIDIA GPU: {address} [10de:{ids[0]:04x}]')
            if (ids[0],) not in entries and ids not in entries:
                unsupported.append(address)
        if unsupported:
            print('open 드라이버 지원이 확인되지 않은 GPU: ' + ', '.join(unsupported), file=sys.stderr)
            return 1
        return 0
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
