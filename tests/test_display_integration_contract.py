"""Cross-module safety contracts, with synthetic observations and no host calls."""
import pytest
import importlib.util
from pathlib import Path

from ala import display, monitors, runtime
from ala.config import Config, ScreenConfig
from ala.system import Paths


@pytest.mark.parametrize('site_state', ('recovering', 'waiting_site'))
def test_site_outage_does_not_prevent_independent_owned_window_verification(tmp_path, monkeypatch, site_state):
    config = Config(schema_version=2, user='user', screens=(ScreenConfig(id='default',
                    url='https://example.com/display', output='HDMI-1'),)).for_screen('default')
    report = {'exit_code': 2, 'state': 'pending', 'process_verified': True, 'service': {'ActiveState': 'active'},
              'health': {'state': site_state, 'supervisor_version': '0.1.0rc21', 'browser_pid': 123},
              'results': [{'id': 'chrome.option.user-data-dir', 'state': 'ok'},
                          {'id': 'chrome.health', 'state': 'pending'}]}
    monkeypatch.setattr(display, '_check_user', lambda *_: report)
    calls = []
    monkeypatch.setattr(monitors, 'placement', lambda *args, **kwargs: calls.append(kwargs) or
                        {'state': 'ok', 'confirmed_window_count': 1})
    observed = display.user_action('windows', config=config, home=tmp_path, host=object(), paths=Paths(tmp_path))
    assert observed['state'] == 'ok' and observed['confirmed_window_count'] == 1
    assert calls and calls[0]['apply'] is False
    assert runtime.code_ready(report | {'placement': observed}, '0.1.0rc21')


def test_unknown_process_evidence_still_blocks_window_inspection(tmp_path, monkeypatch):
    config = Config(user='user', url='https://example.com/display')
    monkeypatch.setattr(display, '_check_user', lambda *_: {'exit_code': 1, 'state': 'unknown',
        'service': {'ActiveState': 'active'}, 'health': {'state': 'unknown'},
        'results': [{'id': 'chrome.process', 'state': 'unknown'}]})
    monkeypatch.setattr(monitors, 'placement', lambda *_, **__: pytest.fail('unverified process must not reach bridge'))
    result = display.user_action('windows', config=config, home=tmp_path, host=object(), paths=Paths(tmp_path))
    assert result['state'] == 'unknown' and result['confirmed_window_count'] is None


@pytest.mark.parametrize('missing', (
    'assets/gnome-extension/ala-display@example.com/metadata.json',
    'assets/gnome-extension/ala-display@example.com/extension.js',
    'docs/frontend-contract.md', 'examples/frontend/deployment.json', 'examples/frontend/index.html',
))
def test_a_release_cannot_claim_display_and_frontend_compatibility_without_required_artifacts(tmp_path, missing):
    spec = importlib.util.spec_from_file_location('ala_build_contract', Path(__file__).parents[1] / 'scripts/build-release.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    names = ('entry.py', 'pyproject.toml', 'requirements.lock', 'install.sh', 'ala/__init__.py',
             'scripts/rocky-linux/bin/chrome-did', 'docs/DISPLAY.md', 'docs/UPDATES.md', 'examples/screens.json',
             'assets/gnome-extension/ala-display@example.com/metadata.json',
             'assets/gnome-extension/ala-display@example.com/extension.js',
             'docs/frontend-contract.md', 'examples/frontend/deployment.json', 'examples/frontend/index.html')
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic public artifact')
    (tmp_path / missing).unlink()
    with pytest.raises(ValueError):
        list(builder.source_files(tmp_path))
