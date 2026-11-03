"""Opt-in real Linux Chrome proc evidence, separate from synthetic unit tests.

Run as an ordinary user inside a network namespace containing only loopback.
The CI step provides that namespace; no sandbox-disabling flag is permitted.
"""
import os
from pathlib import Path
import shutil
import sys

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get('ALA_LINUX_CHROME_SMOKE') != '1',
    reason='Real Linux Chrome smoke is opt-in: ALA_LINUX_CHROME_SMOKE=1.',
)


def require(condition, message):
    # Do not let assertion introspection publish proc argv or temporary paths.
    if not condition:
        pytest.fail(message, pytrace=False)


def quiet_spawn(program, arguments, environment, *, file_actions):
    # Chromium sets this before setproctitle_init. Adding a missing environment
    # entry then can break its original contiguous argv/environ layout check.
    # Seed only this child so title rewriting can be exercised without a real
    # desktop bus or D-Bus autolaunch; no user/system configuration is changed.
    child_environment = dict(environment, DBUS_SESSION_BUS_ADDRESS='disabled:')
    # Browser stderr can contain environment/account/profile details. Keep this
    # smoke's public evidence limited to success/failure and the observed format.
    silence = [(os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0o600),
               (os.POSIX_SPAWN_DUP2, 1, 2)]
    return os.posix_spawn(program, arguments, child_environment, file_actions=[*silence, *file_actions])


def test_real_linux_chrome_proc_observation_and_independent_browser_pid(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Linux /proc smoke is unavailable on this OS; Mac is not Linux validation.')
    require(os.geteuid() != 0, 'The opt-in smoke must run as an ordinary user with Chrome sandbox enabled.')
    chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome')
    require(bool(chrome), 'Opt-in Linux smoke requires installed official Google Chrome; absence is not a semantic skip.')
    interfaces = [line.split(':', 1)[0].strip()
                  for line in Path('/proc/self/net/dev').read_text().splitlines()[2:] if ':' in line]
    require(bool(interfaces) and all(name == 'lo' for name in interfaces),
            'Opt-in Linux smoke requires an isolated network namespace with no external interfaces.')

    from ala.cdp import PipeBrowser
    from ala.chrome_process import observe_chrome_cmdline

    arguments = [chrome, '--headless=new', '--disable-background-networking',
                 '--disable-component-update', '--disable-default-apps', '--disable-extensions',
                 '--disable-sync', '--metrics-recording-only', '--disable-dev-shm-usage',
                 '--no-first-run', '--no-default-browser-check', '--hide-crash-restore-bubble',
                 '--start-fullscreen', '--password-store=basic', '--remote-debugging-pipe',
                 '--user-data-dir=' + str(tmp_path / 'isolated-profile'), 'about:blank']
    browser = None
    try:
        try:
            browser = PipeBrowser(arguments, spawn=quiet_spawn)
            info = browser.call('SystemInfo.getProcessInfo', timeout=15)
        except Exception:
            pytest.fail('Sandboxed Linux Chrome private CDP process query failed.', pytrace=False)
        processes = info.get('processInfo') if isinstance(info, dict) else None
        require(isinstance(processes, list), 'Browser-level CDP returned no process inventory.')
        browsers = [item for item in processes if isinstance(item, dict) and item.get('type') == 'browser']
        require(len(browsers) == 1, 'CDP did not identify exactly one browser process.')
        pid = browsers[0].get('id')
        require(type(pid) is int and pid > 0 and pid == browser.pid,
                'CDP browser identity differs from the child spawned through the private pipe.')
        try:
            process = Path('/proc') / str(pid)
            executable = (process / 'exe').readlink()
            raw = (process / 'cmdline').read_bytes()
            require(str(executable).removesuffix(' (deleted)') == '/opt/google/chrome/chrome',
                    'Real smoke requires the supported official Google Chrome executable, not a Chromium alternative.')
            observation = observe_chrome_cmdline(raw, executable=executable, expected_arguments=arguments[1:])
        except Exception:
            pytest.fail('Shared proc observer rejected live Linux Chrome process evidence.', pytrace=False)
        require(observation.format == 'linux_title',
                'Live Linux Chrome did not exercise the Linux process-title parser branch required by this smoke.')
        require(all(observation.arguments.count(item) == 1 for item in arguments[1:]),
                'A requested Chrome argument was absent or duplicated in live proc evidence.')
        require(not any(item.split('=', 1)[0] in {'--no-sandbox', '--remote-debugging-port', '--remote-debugging-address'}
                        for item in observation.arguments), 'Live Chrome evidence contains a prohibited launch option.')
        print('Linux Chrome proc observation validated: ' + observation.format)
    finally:
        if browser is not None:
            browser.close()
