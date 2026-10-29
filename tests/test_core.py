from pathlib import Path
import json
import pytest

from ala.config import Config
from ala.model import CheckResult, Report
from ala.system import Paths, atomic_json

@pytest.mark.parametrize("value", [[], None, "bad", {"password": "secret"}, {"schema_version": True},
 {"schema_version": 2}, {"user": "root"}, {"user": "x;touch /tmp/no"}, {"user": "-n"},
 {"url": "https://user:password@example.com"}, {"url": "file:///etc/passwd"},
 {"url": "https://example.com\n"}, {"url": "http://host:bad"}, {"url": "http://["},
 {"display": 1}, {"autostart": "true"}, {"timezone": "../../etc/passwd"}, {"ssh_zone": "$(id)"}])
def test_config_rejects_ambiguous_or_unsafe_data(value):
    with pytest.raises((ValueError, TypeError)):
        Config.from_dict(value)

def test_config_roundtrip_preserves_url_as_data(tmp_path):
    config = Config(user="user", url="https://example.com/?x=$literal&y=${literal}")
    p = tmp_path / "config.json"
    atomic_json(p, config.to_dict(), 0o644)
    assert Config.load(p) == config
    assert p.stat().st_mode & 0o777 == 0o644

def test_report_failure_has_priority_over_pending():
    assert Report([CheckResult("gpu", "pending")]).exit_code == 2
    assert Report([CheckResult("gpu", "pending"), CheckResult("other", "unknown")]).exit_code == 1
    assert Report([CheckResult("gpu", "not_applicable")]).exit_code == 0
    assert json.loads(json.dumps(Report().to_dict()))["schema_version"] == 1

def test_paths_are_explicitly_injected_without_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ALA_ROOT", str(tmp_path))
    assert Paths().config == Path("/etc/ala/config.json")
    assert Paths(tmp_path).config == tmp_path / "etc/ala/config.json"

def test_invalid_state_rejected():
    with pytest.raises(ValueError):
        CheckResult("test", "fine")
