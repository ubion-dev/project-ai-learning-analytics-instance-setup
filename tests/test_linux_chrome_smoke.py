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


@pytest.mark.parametrize('page_reload', [False, True])
def test_real_linux_display_probe_and_page_reload_are_distinguished(tmp_path, monkeypatch, page_reload):
    if sys.platform != 'linux':
        pytest.skip('Linux /proc smoke is unavailable on this OS; Mac is not Linux validation.')
    require(os.geteuid() != 0, 'Linux display smoke requires an ordinary sandboxed browser user.')
    interfaces = [line.split(':', 1)[0].strip()
                  for line in Path('/proc/self/net/dev').read_text().splitlines()[2:] if ':' in line]
    require(bool(interfaces) and all(name == 'lo' for name in interfaces),
            'Linux display smoke requires an isolated loopback-only network namespace.')
    chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome')
    require(bool(chrome), 'Linux display smoke requires official Google Chrome.')

    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from ala import display_health as health
    from ala.cdp import PipeBrowser
    from ala.config import Config

    requests = {'probe': 0, 'document': 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/display':
                self.send_response(204)
                self.end_headers()
                return
            kind = 'probe' if self.headers.get('User-Agent', '').startswith('ala-display-readiness/') else 'document'
            requests[kind] += 1
            script = '<script>setTimeout(()=>location.reload(),400)</script>' if page_reload else ''
            body = ('<!doctype html><title>synthetic display</title><p>display</p>' + script).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = 'http://127.0.0.1:' + str(server.server_port) + '/display'
        arguments = [chrome, '--headless=new', '--disable-background-networking',
                     '--disable-component-update', '--disable-default-apps', '--disable-extensions',
                     '--disable-sync', '--metrics-recording-only', '--no-first-run',
                     '--no-default-browser-check', '--password-store=basic', '--remote-debugging-pipe',
                     '--user-data-dir=' + str(tmp_path / 'display-profile'), health.RECOVERY_URL]
        hard_stop = time.monotonic() + 20
        healthy_stop = [None]
        supervisor = health.Supervisor(Config(user='user', url=url), tmp_path, arguments,
            transport_factory=lambda argv: PipeBrowser(argv, spawn=quiet_spawn),
            stop_requested=lambda: time.monotonic() >= hard_stop or
                (healthy_stop[0] is not None and time.monotonic() >= healthy_stop[0]))
        save = supervisor._save

        def observed_save():
            if supervisor.value['state'] == 'healthy' and healthy_stop[0] is None:
                healthy_stop[0] = time.monotonic() + 1.5
            save()

        supervisor._save = observed_save
        monkeypatch.setattr(health, 'INTERVAL', 0.2)
        code = supervisor.run()
        require(code == 0 and healthy_stop[0] is not None,
                'Linux display supervisor did not reach healthy state and stop cleanly.')
        navigation = supervisor.value.get('navigation') or {}
        require(requests['probe'] >= 3, 'Linux display smoke did not perform repeated real HTTP probes.')
        require(navigation.get('display_requests') == 1 and navigation.get('recovery_requests') == 0,
                'Healthy real HTTP probes unexpectedly requested another display navigation.')
        if page_reload:
            require(requests['document'] >= 2 and navigation.get('browser_navigations', 0) >= 1,
                    'Browser-initiated reloads were not observed independently of ala commands.')
        else:
            require(requests['document'] == 1 and navigation.get('browser_navigations') == 0,
                    'Static display was unexpectedly requested or counted as reloading.')
        print('Linux display reload attribution validated: ' + ('browser_reload' if page_reload else 'static'))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
