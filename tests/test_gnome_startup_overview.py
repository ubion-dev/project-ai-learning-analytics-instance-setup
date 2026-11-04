"""Exercise the GNOME extension with mocked GJS objects under Node."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


EXTENSION = Path(__file__).resolve().parents[1] / 'assets/gnome-extension/ala-display@example.com/extension.js'
HARNESS = """
import assert from 'node:assert/strict';

class Extension {}
const Config = {PACKAGE_VERSION: '47.4'};
const config = {user: 'user', display: true};
let configMode = 0o644;
let callerUid = 1000;
let hideCalls = 0;
const startupHandlers = new Map();
let handlerId = 0;
const Main = {
    sessionMode: {currentMode: 'user', isGreeter: false, isLocked: false, hasOverview: true},
    overview: {
        visibleTarget: true,
        hide() {
            hideCalls++;
            this.visibleTarget = false;
        },
    },
    layoutManager: {
        _startingUp: false,
        connect(signal, callback) {
            assert.equal(signal, 'startup-complete');
            startupHandlers.set(++handlerId, callback);
            return handlerId;
        },
        disconnect(id) {
            assert.ok(startupHandlers.delete(id));
        },
    },
};
const Gio = {
    Credentials: class { get_unix_user() { return 1000; } },
    DBusExportedObject: {
        wrapJSObject(xml) {
            assert.match(xml, /name="DismissStartupOverview"/);
            return {export() {}, unexport() {}};
        },
    },
    DBus: {
        session: {
            call(_bus, _path, _iface, method, _params, _type, _flags, _timeout, _cancel, callback) {
                assert.equal(method, 'GetConnectionUnixUser');
                callback(this, callerUid);
            },
            call_finish(result) { return [result]; },
        },
    },
    DBusCallFlags: {NONE: 0},
    BusNameOwnerFlags: {NONE: 0},
    bus_own_name_on_connection() { return 1; },
    bus_unown_name() {},
    File: {
        new_for_path(path) {
            assert.equal(path, '/etc/ala/config.json');
            return {
                query_info() {
                    return {
                        get_is_symlink: () => false,
                        get_file_type: () => 1,
                        get_attribute_uint32: key => key === 'unix::mode' ? configMode : 0,
                    };
                },
            };
        },
    },
    FileType: {REGULAR: 1},
    FileQueryInfoFlags: {NOFOLLOW_SYMLINKS: 0},
};
const GLib = {
    Variant: class {
        constructor(_type, value) { this.value = value; }
        deep_unpack() { return this.value; }
    },
    file_get_contents(path) {
        assert.equal(path, '/etc/ala/config.json');
        return [true, new TextEncoder().encode(JSON.stringify(config))];
    },
    get_user_name() { return 'user'; },
};

function requestDismissal(extension) {
    return new Promise(resolve => {
        extension.DismissStartupOverviewAsync([], {
            get_sender: () => ':1.1',
            return_value: response => resolve(JSON.parse(response.deep_unpack()[0])),
        });
    });
}

function completeStartup() {
    Main.layoutManager._startingUp = false;
    for (const callback of [...startupHandlers.values()])
        callback(Main.layoutManager);
}
"""


def run_extension(scenario):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for the extension harness')
    source = re.sub(r'^import [^\n]+;\n', '', EXTENSION.read_text(encoding='utf-8'), flags=re.MULTILINE)
    source = source.replace('export default class AlaDisplay', 'class AlaDisplay', 1)
    result = subprocess.run(
        [node, '--input-type=module', '-'],
        input=HARNESS + source + '\n' + scenario,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('initially_visible', [True, False])
def test_dismissal_is_one_shot_even_when_overview_is_already_hidden(initially_visible):
    run_extension(f"""
const extension = new AlaDisplay();
extension.enable();
Main.overview.visibleTarget = {str(initially_visible).lower()};
const first = await requestDismissal(extension);
assert.equal(first.state, 'ok');
assert.equal(first.overview, '{'dismissed' if initially_visible else 'already_hidden'}');
Main.overview.visibleTarget = true;
extension.disable();
extension.enable();
assert.equal((await requestDismissal(extension)).overview, 'kept');
assert.equal(hideCalls, {int(initially_visible)});
assert.equal(Main.overview.visibleTarget, true);
""")


@pytest.mark.parametrize('startup_state', ['ready', 'disabled', 'locked'])
def test_dismissal_waits_for_shell_startup_and_disconnects(startup_state):
    run_extension(f"""
const extension = new AlaDisplay();
extension.enable();
Main.layoutManager._startingUp = true;
assert.equal((await requestDismissal(extension)).overview, 'scheduled');
assert.equal(hideCalls, 0);
assert.equal((await requestDismissal(extension)).overview, 'kept');
assert.equal(startupHandlers.size, 1);
if ('{startup_state}' === 'disabled')
    extension.disable();
if ('{startup_state}' === 'locked')
    Main.sessionMode.isLocked = true;
completeStartup();
assert.equal(hideCalls, {int(startup_state == 'ready')});
assert.equal(startupHandlers.size, 0);
""")


def test_request_in_flight_does_not_add_handler_after_disable():
    run_extension("""
const extension = new AlaDisplay();
extension.enable();
Main.layoutManager._startingUp = true;
const request = requestDismissal(extension);
extension.disable();
assert.notEqual((await request).state, 'ok');
completeStartup();
assert.equal(hideCalls, 0);
assert.equal(startupHandlers.size, 0);
""")


@pytest.mark.parametrize('change', [
    'callerUid = 1001',
    "config.user = 'other'",
    'config.display = false',
    'configMode = 0o666',
    'Main.sessionMode.isGreeter = true',
    'Main.sessionMode.isLocked = true',
])
def test_dismissal_rejects_other_callers_accounts_and_session_modes(change):
    run_extension(f"""
const extension = new AlaDisplay();
extension.enable();
{change};
assert.notEqual((await requestDismissal(extension)).state, 'ok');
assert.equal(hideCalls, 0);
assert.equal(startupHandlers.size, 0);
""")
