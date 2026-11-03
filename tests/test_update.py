import hashlib
import io
import json
import tarfile

import pytest
from ala import update
from ala.cli import main
from ala.system import Command, Paths


def release(tag, **extra):
    version = update.tag_version(tag)
    return dict(tag_name=tag, assets=[{'name': f'ala-{version}.tar.gz' + suffix, 'state': 'uploaded'}
                                    for suffix in ('', '.sha256')], **extra)


def test_channels_and_numeric_order():
    rows = [release('v1.0.0'), release('v2.0.0-rc.10', prerelease=True), release('v2.0.0-rc.9', prerelease=True)]
    assert update.select_release(rows, current='0.1.0')['latest'] == '1.0.0'
    assert update.select_release(rows, True, '0.1.0')['latest'] == '2.0.0rc10'
    assert update.version_key('2.0.0') > update.version_key('2.0.0rc10')
    assert not update.select_release(rows, current='3.0.0')['available']
    assert not update.select_release([release('v2.0.0', draft=True)])['available']
    assert not update.select_release([{'tag_name': 'v2.0.0', 'assets': []}])['available']


def test_network_failure_does_not_authenticate(monkeypatch, capsys):
    monkeypatch.setattr(update, 'fetch', lambda *a: (_ for _ in ()).throw(OSError()))
    class NoAuth:
        def authenticate(self):
            pytest.fail('read-only update check requested sudo')
    assert main(['update', '--check', '--json'], service=NoAuth()) == 1
    assert json.loads(capsys.readouterr().out)['exit_code'] == 1


def bundle(tmp_path, *, bad_hash=False, evil=None):
    files = {'install.sh': b'echo fixture', 'VERSION': b'2.0.0\n'}
    manifest = {'schema_version': 1, 'version': '2.0.0', 'files': {k: hashlib.sha256(v).hexdigest() for k,v in files.items()}}
    if bad_hash:
        manifest['files']['install.sh'] = '0' * 64
    files['MANIFEST.json'] = json.dumps(manifest).encode()
    archive = tmp_path / 'bundle.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo('ala-2.0.0/' + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if evil:
            info = tarfile.TarInfo(evil)
            tar.addfile(info, io.BytesIO(b''))
    return archive


@pytest.mark.parametrize('evil', ['../outside', '/absolute', 'ala-2.0.0/../outside', 'other/file'])
def test_reject_unsafe_archive(tmp_path, evil):
    with pytest.raises(ValueError):
        update.unpack(bundle(tmp_path, evil=evil), tmp_path / 'out', '2.0.0')


def test_verify_bundle_before_install(tmp_path):
    result = update.unpack(bundle(tmp_path), tmp_path / 'out', '2.0.0')
    assert (result / 'VERSION').read_text().strip() == '2.0.0'
    with pytest.raises(ValueError):
        update.unpack(bundle(tmp_path, bad_hash=True), tmp_path / 'bad', '2.0.0')


def test_bad_checksum_never_executes(monkeypatch):
    monkeypatch.setattr(update, 'fetch', lambda *a, **kw: b'invalid')
    monkeypatch.setattr(update.subprocess, 'run', lambda *a, **kw: pytest.fail('executed invalid download'))
    with pytest.raises(ValueError):
        update.install_release('v9.0.0')


def test_cli_update_submits_selected_channel(capsys):
    class Service:
        def authenticate(self): pass
        def request(self, action, payload):
            assert action == 'update-submit'
            assert payload == {'prerelease': True}
            return {'started': True, 'latest': '2.0.0rc1', 'next_action': 'logs', 'exit_code': 2}
    assert main(['update', '--prerelease', '--detach', '--json'], service=Service()) == 2
    assert json.loads(capsys.readouterr().out)['started']


def test_download_and_verified_installer_handoff(tmp_path, monkeypatch):
    archive = bundle(tmp_path)
    data = archive.read_bytes()
    checksum = hashlib.sha256(data).hexdigest() + '  ala-2.0.0.tar.gz\n'
    monkeypatch.setattr(update, 'fetch', lambda url, *a, **kw: checksum.encode() if url.endswith('.sha256') else data)
    calls = []
    monkeypatch.setattr(update.subprocess, 'run', lambda args, **kw: calls.append((args, kw)))
    update.install_release('v2.0.0')
    assert len(calls) == 1
    assert calls[0][0][0] == 'bash'
    assert calls[0][1]['env']['ALA_UPDATE_ONLY'] == '1'


def test_symlink_archive_rejected(tmp_path):
    archive = tmp_path / 'link.tar'
    with tarfile.open(archive, 'w') as tar:
        info = tarfile.TarInfo('ala-2.0.0/install.sh')
        info.type = tarfile.SYMTYPE
        info.linkname = '/outside'
        tar.addfile(info)
    with pytest.raises(ValueError):
        update.unpack(archive, tmp_path / 'out', '2.0.0')


def test_no_release_does_not_start_job(monkeypatch, tmp_path):
    monkeypatch.setattr(update, 'check', lambda *a, **kw: {'available': False, 'exit_code': 0})
    monkeypatch.setattr(update.subprocess, 'run', lambda *a, **kw: pytest.fail('unnecessary worker'))
    assert not update.submit(tmp_path)['available']


def test_submit_fixed_background_worker(monkeypatch, tmp_path):
    from ala import jobs
    monkeypatch.setattr(update, 'check', lambda *a, **kw: {'available': True, 'exit_code': 0, 'tag': 'v2.0.0', 'latest': '2.0.0'})
    monkeypatch.setattr(jobs.JobStore, 'list', lambda self: [])
    paths = Paths(tmp_path)
    boot = paths.at('/proc/sys/kernel/random/boot_id')
    boot.parent.mkdir(parents=True)
    boot.write_text('synthetic-boot')
    calls = []
    class Host:
        def run(self, args, **kwargs):
            calls.append(args)
            return Command(0)
    result = update.submit(tmp_path, paths=paths, host=Host())
    assert result['started'] and result['exit_code'] == 2
    assert calls[0][:3] == ['systemd-run', '--unit=ala-update', '--collect']
    assert calls[0][-2:] == ['_update-worker', result['run_id']]
    monkeypatch.setattr(jobs.JobStore, 'list', lambda self: [{'state': 'running'}])
    with pytest.raises(RuntimeError):
        update.submit(tmp_path, paths=paths, host=Host())
    assert len(calls) == 1
