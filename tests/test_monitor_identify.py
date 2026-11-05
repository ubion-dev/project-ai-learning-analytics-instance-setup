"""Read-only numbering and the explicit, capability-gated identify request."""
from copy import deepcopy
import json

import pytest

from ala import monitors
from ala.system import Command
from test_monitors import state


class IdentifyHost:
    def __init__(self, observation=None, result=None):
        self.observation = state() | {'capabilities': ['identify-outputs']} if observation is None else observation
        inventory = monitors.topology(host=StateHost(self.observation))
        self.result = result if result is not None else {
            'protocol': 1, 'state': 'ok', 'serial': 1, 'duration_seconds': 10,
            'logical_outputs': monitors.numbered_outputs(inventory),
        }
        self.calls = []

    def run(self, argv, **kwargs):
        method = argv[-1].rsplit('.', 1)[-1]
        self.calls.append(method)
        assert method in {'GetState', 'IdentifyOutputs'}
        return Command(0, repr((json.dumps(self.observation if method == 'GetState' else self.result),)))


class StateHost:
    def __init__(self, observation):
        self.observation = observation

    def run(self, argv, **kwargs):
        assert argv[-1].endswith('.GetState')
        return Command(0, repr((json.dumps(self.observation),)))


def test_numbering_uses_logical_ids_and_never_changes_saved_identity_or_input():
    inventory = {'logical_outputs': [
        {'id': 'HDMI-1', 'connectors': ['HDMI-1'], 'x': -1920},
        {'id': 'DP-1+DP-2', 'connectors': ['DP-1', 'DP-2'], 'x': 0},
    ]}
    original = deepcopy(inventory)
    numbered = monitors.numbered_outputs(inventory)
    assert [(item['id'], item['display_number']) for item in numbered] == [('DP-1+DP-2', 1), ('HDMI-1', 2)]
    numbered[0]['connectors'].append('changed')
    assert inventory == original


def test_topology_exposes_numbers_and_capability_without_requesting_an_overlay():
    host = IdentifyHost()
    inventory = monitors.topology(host=host)
    assert inventory['capabilities'] == ['identify-outputs']
    assert [(item['id'], item['display_number']) for item in inventory['logical_outputs']] == [('HDMI-1', 2), ('DP-1', 1)]
    assert host.calls == ['GetState']


@pytest.mark.parametrize('observation', [state(), state() | {'capabilities': []}])
def test_old_extension_is_unknown_and_never_receives_identify(observation):
    host = IdentifyHost(observation)
    result = monitors.identify_outputs(host=host)
    assert result['state'] == 'unknown'
    assert '재로그인' in result['next_action']
    assert host.calls == ['GetState']


def test_explicit_identify_returns_bounded_display_request_not_panel_evidence():
    host = IdentifyHost()
    result = monitors.identify_outputs(host=host)
    assert result['state'] == 'ok' and result['duration_seconds'] == 10
    assert [item['display_number'] for item in result['logical_outputs']] == [1, 2]
    assert '실제 패널' in result['reason']
    assert host.calls == ['GetState', 'IdentifyOutputs']


@pytest.mark.parametrize('change', ['duration', 'number', 'bool-number', 'geometry', 'connector', 'duplicate'])
def test_malformed_identify_success_is_not_reported_as_success(change):
    host = IdentifyHost()
    if change == 'duration':
        host.result['duration_seconds'] = 60
    elif change == 'number':
        host.result['logical_outputs'][0]['display_number'] = 2
    elif change == 'bool-number':
        host.result['logical_outputs'][0]['display_number'] = True
    elif change == 'geometry':
        host.result['logical_outputs'][0]['width'] = 0
    elif change == 'connector':
        host.result['logical_outputs'][0]['connectors'] = ['another']
    else:
        host.result['logical_outputs'].append(deepcopy(host.result['logical_outputs'][0]))
    assert monitors.identify_outputs(host=host)['state'] == 'unknown'


def test_unconfirmed_output_does_not_receive_an_identify_request():
    observation = state() | {'capabilities': ['identify-outputs'], 'compositor_monitors': []}
    host = IdentifyHost(observation)
    assert monitors.identify_outputs(host=host)['state'] == 'pending'
    assert host.calls == ['GetState']
