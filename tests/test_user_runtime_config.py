"""Pinned display configs stay independent of a future desired config schema."""
from pathlib import Path
from types import SimpleNamespace
import json
import os

import pytest

from ala import display,runtime
from ala.config import Config
from ala.system import Paths


@pytest.fixture
def user_config(tmp_path,monkeypatch):
    paths=Paths(tmp_path)
    paths.config.parent.mkdir(parents=True)
    paths.config.write_text(json.dumps({'schema_version':3,'user':'user'}))
    paths.config.chmod(0o644)
    owner={'uid':0}
    original=Path.lstat
    def protected_metadata(path,*args,**kwargs):
        value=original(path,*args,**kwargs)
        if path==paths.config:
            values=list(value);values[4]=owner['uid']
            return os.stat_result(values)
        return value
    monkeypatch.setattr(Path,'lstat',protected_metadata)
    monkeypatch.setattr(display.os,'getuid',lambda:12345)
    monkeypatch.setattr(display.pwd,'getpwuid',lambda uid:SimpleNamespace(pw_name='user',pw_dir='/home/user'))
    monkeypatch.delenv('ALA_RUNTIME_CONFIG',raising=False)
    monkeypatch.delenv('ALA_RUNTIME_TARGET',raising=False)
    snapshot=Config(schema_version=2,user='user',screens=[{'id':'default','url':'https://example.com/original'},
                        {'id':'second','url':'https://example.com/applied'}]).to_dict()
    selection={'config':snapshot,'revision':'a'*64,'version':'0.1.0rc21'}
    return paths,owner,selection


@pytest.mark.parametrize('mode',['runtime','pinned_rpc'])
def test_future_desired_schema_does_not_break_valid_applied_snapshot(user_config,monkeypatch,mode):
    paths,_,selection=user_config
    calls=[]
    def selected(target,**kwargs):
        calls.append(target)
        assert kwargs['paths']==paths
        return selection
    monkeypatch.setattr(runtime,'load_user_selection',selected)
    if mode=='pinned_rpc':
        monkeypatch.setenv('ALA_RUNTIME_CONFIG','/var/lib/ala-runtime/configs/snapshot.json')
        monkeypatch.setenv('ALA_RUNTIME_TARGET','second')
    before=paths.config.read_bytes(),paths.config.stat().st_mtime_ns
    config,home=display._load_user_config(paths,'second',runtime=mode=='runtime')
    assert config.target_id=='second' and config.url=='https://example.com/applied'
    assert config.config_revision=='a'*64 and home==Path('/home/user')
    assert calls==['second']
    assert before==(paths.config.read_bytes(),paths.config.stat().st_mtime_ns)


def test_without_selected_runtime_future_global_schema_still_fails_closed(user_config,monkeypatch):
    paths,_,_=user_config
    monkeypatch.setattr(runtime,'load_user_selection',lambda *a,**kw:None)
    with pytest.raises(ValueError,match='구성 버전'):
        display._load_user_config(paths,runtime=True)


def test_unpinned_user_operation_does_not_replace_unknown_global_with_a_runtime(user_config,monkeypatch):
    paths,_,_=user_config
    monkeypatch.setattr(runtime,'load_user_selection',lambda *a,**kw:pytest.fail('unpinned operation uses protected desired config'))
    with pytest.raises(ValueError,match='구성 버전'):
        display._load_user_config(paths)


def test_selected_snapshot_failure_is_not_replaced_by_global_config(user_config,monkeypatch):
    paths,_,_=user_config
    paths.config.write_text(json.dumps(Config(user='user',url='https://example.com/desired').to_dict()))
    def reject(*args,**kwargs):raise PermissionError('검증되지 않은 root 선택')
    monkeypatch.setattr(runtime,'load_user_selection',reject)
    monkeypatch.setenv('ALA_RUNTIME_CONFIG','/var/lib/ala-runtime/configs/rejected.json')
    with pytest.raises(PermissionError,match='root 선택'):
        display._load_user_config(paths)


@pytest.mark.parametrize('fault',['wrong_owner','writable','symlink','missing'])
def test_valid_selected_snapshot_does_not_bypass_global_file_protection(user_config,monkeypatch,fault):
    paths,owner,_=user_config
    monkeypatch.setattr(runtime,'load_user_selection',lambda *a,**kw:pytest.fail('file protection must run first'))
    if fault=='wrong_owner':owner['uid']=12346
    elif fault=='writable':paths.config.chmod(0o666)
    elif fault=='symlink':
        other=paths.config.with_name('other.json');other.write_bytes(paths.config.read_bytes())
        paths.config.unlink();paths.config.symlink_to(other)
    else:paths.config.unlink()
    with pytest.raises((PermissionError,FileNotFoundError)):
        display._load_user_config(paths,runtime=True)


def test_legacy_without_selection_still_loads_supported_global_config(user_config,monkeypatch):
    paths,_,_=user_config
    expected=Config(user='user',url='https://example.com/legacy')
    paths.config.write_text(json.dumps(expected.to_dict()))
    monkeypatch.setattr(runtime,'load_user_selection',lambda *a,**kw:None)
    config,_=display._load_user_config(paths,runtime=True)
    assert config.url==expected.url and config.config_revision==''


def test_selected_snapshot_keeps_account_and_schema_checks(user_config,monkeypatch):
    paths,_,selection=user_config
    paths.config.write_text(json.dumps(Config(user='user').to_dict()))
    selection['config']['user']='other'
    monkeypatch.setattr(runtime,'load_user_selection',lambda *a,**kw:selection)
    with pytest.raises(PermissionError,match='화면 사용자'):
        display._load_user_config(paths,runtime=True)
    selection['config']['schema_version']=3
    with pytest.raises(ValueError,match='구성 버전'):
        display._load_user_config(paths,runtime=True)
