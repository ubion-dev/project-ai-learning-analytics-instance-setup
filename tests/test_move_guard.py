from pathlib import Path

import pytest

from ala.config import Config
from ala.jobs import change_lock
from ala.service import Operations
from ala.system import Paths


@pytest.mark.parametrize('reviewed', [False, True])
def test_screens_apply_preserves_existing_call_and_accepts_reviewed_config(monkeypatch, tmp_path, reviewed):
    config = Config(user='user', url='https://example.com/display')
    operations = Operations(paths=Paths(tmp_path), release=Path('/release'))
    monkeypatch.setattr(operations, 'config', lambda: config)
    submitted = []

    def submit(target, *, release, paths, host, settings_config):
        with change_lock(paths):
            submitted.append((target, settings_config()))
        return {'state': 'queued'}

    monkeypatch.setattr('ala.runtime.submit_apply', submit)
    payload = {'target': 'default'}
    if reviewed:
        payload['expected_config'] = config.to_dict()
    assert operations.dispatch('screens-apply', payload) == {'state': 'queued'}
    assert submitted == [('default', config)]


def test_screens_apply_rechecks_reviewed_configuration_inside_runtime_lock(monkeypatch, tmp_path):
    before = Config(user='user', url='https://example.com/before')
    after = Config(user='user', url='https://example.com/after')
    current = [before]
    submitted = []
    operations = Operations(paths=Paths(tmp_path), release=Path('/release'))
    monkeypatch.setattr(operations, 'config', lambda: current[0])

    def submit(target, *, release, paths, host, settings_config):
        # Another client saves after the apply request enters the service.
        current[0] = after
        with change_lock(paths):
            settings = settings_config()
            submitted.append(settings)
        return {'state': 'queued'}

    monkeypatch.setattr('ala.runtime.submit_apply', submit)
    with pytest.raises(RuntimeError, match='검토 후 구성이 변경'):
        operations.dispatch('screens-apply', {'target': 'default', 'expected_config': before.to_dict()})
    assert submitted == []


@pytest.mark.parametrize('expected', [None, True, [], 'config'])
def test_invalid_reviewed_configuration_never_submits(monkeypatch, tmp_path, expected):
    operations = Operations(paths=Paths(tmp_path), release=Path('/release'))
    submitted = []
    monkeypatch.setattr('ala.runtime.submit_apply', lambda *args, **kwargs: submitted.append(kwargs))
    with pytest.raises(ValueError, match='object'):
        operations.dispatch('screens-apply', {'expected_config': expected})
    assert submitted == []
