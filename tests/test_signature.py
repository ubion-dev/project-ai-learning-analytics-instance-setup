import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from ala import signature, update
from ala.system import Command, Paths


class Response(io.BytesIO):
    status = 200


def verifier(paths, monkeypatch, data=b'fixed verifier fixture'):
    monkeypatch.setattr(signature, 'COSIGN_SHA256', hashlib.sha256(data).hexdigest())
    path = signature.verifier_path(paths)
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def test_bootstrap_checks_pinned_bytes_before_install_or_execution(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    expected = b'fixed verifier fixture'
    monkeypatch.setattr(signature, 'COSIGN_SHA256', hashlib.sha256(expected).hexdigest())
    with pytest.raises(ValueError):
        signature.bootstrap(paths=paths, opener=lambda *a, **k: Response(b'changed verifier'))
    assert not signature.verifier_path(paths).exists()
    result = signature.bootstrap(paths=paths, opener=lambda *a, **k: Response(expected))
    assert result['state'] == 'completed'
    assert signature.ensure_verifier(paths).read_bytes() == expected
    assert signature.bootstrap(paths=paths, opener=lambda *a, **k: pytest.fail('repeat download'))['state'] == 'kept'


def test_verifier_hash_or_writable_parent_never_executes(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    path = verifier(paths, monkeypatch)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError): signature.ensure_verifier(paths)
    path.parent.chmod(0o777)
    with pytest.raises(PermissionError): signature.ensure_verifier(paths)


def test_blob_verification_pins_exact_workflow_identity_and_clean_environment(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    path = verifier(paths, monkeypatch)
    artifact, bundle = tmp_path / 'artifact.tar.gz', tmp_path / 'artifact.sigstore.json'
    artifact.write_bytes(b'archive')
    bundle.write_text('{}')
    monkeypatch.setenv('SIGSTORE_ROOT_FILE', '/untrusted/fixture')
    calls = []
    class Host:
        def run(self, args, **kwargs):
            calls.append((args, kwargs))
            return Command(0)
    result = signature.verify_blob(artifact, bundle, 'v2.0.0', paths=paths, host=Host())
    assert result['state'] == 'verified'
    args, kwargs = calls[0]
    assert args[:3] == [str(path), 'verify-blob', str(artifact)]
    identity = args[args.index('--certificate-identity') + 1]
    assert identity.endswith('/.github/workflows/release.yml@refs/tags/v2.0.0')
    assert args[args.index('--certificate-github-workflow-ref') + 1] == 'refs/tags/v2.0.0'
    assert args[args.index('--certificate-github-workflow-trigger') + 1] == 'push'
    assert 'SIGSTORE_ROOT_FILE' not in kwargs['env']
    assert not any('ignore' in arg or 'insecure' in arg or 'regexp' in arg for arg in args)


def signed_fixture(tmp_path, compatibility=None):
    files = {'install.sh': b'# fixture', 'VERSION': b'2.0.0', 'entry.py': b'# entry fixture'}
    if compatibility is not False:
        files['COMPATIBILITY.json'] = json.dumps(compatibility if compatibility is not None else {
            'schema_version': 1, 'runtime_protocol': 1, 'frontend_protocol': 1,
            'extension_protocol': 1, 'config_schemas': [1, 2]}).encode()
    manifest = {'schema_version': 1, 'version': '2.0.0',
                'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    files['MANIFEST.json'] = json.dumps(manifest).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as out:
        for name, content in files.items():
            item = tarfile.TarInfo('ala-2.0.0/' + name)
            item.size = len(content)
            out.addfile(item, io.BytesIO(content))
    return files, archive.getvalue()


def test_enrollment_compares_installed_manifest_and_payload_before_receipt(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    files, archive = signed_fixture(tmp_path)
    directory = paths.at('/opt/ala/releases/2.0.0')
    directory.mkdir(parents=True)
    for name, content in files.items(): (directory / name).write_bytes(content)
    paths.at('/opt/ala/current').symlink_to(directory)
    monkeypatch.setattr(update, 'fetch', lambda url, *a, **k: b'{}' if url.endswith('.sigstore.json') else archive)
    monkeypatch.setattr(signature, 'verify_blob', lambda *a, **k: {
        'state': 'verified', 'verifier_version': signature.COSIGN_VERSION, 'tag': 'v2.0.0'})
    (directory / 'entry.py').write_text('# modified')
    with pytest.raises(ValueError): signature.verify_installed_release(paths=paths)
    assert not paths.at('/var/lib/ala/trust/last-verified.json').exists()
    (directory / 'entry.py').write_bytes(files['entry.py'])
    assert signature.verify_installed_release(paths=paths)['version'] == '2.0.0'
    assert signature.verified_release('2.0.0', paths=paths)


def test_signature_failure_prevents_root_installer_handoff(tmp_path, monkeypatch):
    files, archive = signed_fixture(tmp_path)
    digest = hashlib.sha256(archive).hexdigest() + '  ala-2.0.0.tar.gz\n'
    monkeypatch.setattr(update, 'fetch', lambda url, *a, **k: digest.encode() if url.endswith('.sha256') else
                        b'{}' if url.endswith('.sigstore.json') else archive)
    monkeypatch.setattr(signature, 'verify_blob', lambda *a, **k: (_ for _ in ()).throw(ValueError('verification failed')))
    monkeypatch.setattr(update.subprocess, 'run', lambda *a, **k: pytest.fail('unverified installer executed'))
    with pytest.raises(ValueError): update.install_release('v2.0.0', require_signature=True, paths=Paths(tmp_path))


def test_cas_cli_rollback_restores_only_its_own_pointer(tmp_path):
    paths = Paths(tmp_path)
    for version in ('1.0.0', '2.0.0', '3.0.0'):
        directory = paths.at('/opt/ala/releases') / version
        (directory / '.venv/bin').mkdir(parents=True)
        (directory / 'VERSION').write_text(version)
        (directory / 'entry.py').write_text('# entry fixture')
        (directory / '.venv/bin/python').write_text('# interpreter fixture')
    current = paths.at('/opt/ala/current')
    current.symlink_to(paths.at('/opt/ala/releases/2.0.0'))
    class Host:
        def run(self, args, **kwargs):
            assert args[-1] == '--version'
            return Command(0, '1.0.0')
    assert update.rollback_current('2.0.0', '1.0.0', paths=paths, host=Host())['state'] == 'completed'
    assert update.installed_version(paths) == '1.0.0'
    current.unlink()
    current.symlink_to(paths.at('/opt/ala/releases/3.0.0'))
    assert update.rollback_current('2.0.0', '1.0.0', paths=paths, host=Host())['state'] == 'superseded'
    assert update.installed_version(paths) == '3.0.0'


def installed_fixture(paths):
    directory = paths.at('/opt/ala/releases/1.0.0')
    (directory / '.venv/bin').mkdir(parents=True)
    (directory / 'VERSION').write_text('1.0.0')
    (directory / 'entry.py').write_text('# old entry fixture')
    (directory / '.venv/bin/python').write_text('# old interpreter fixture')
    (directory / 'COMPATIBILITY.json').write_text(json.dumps({
        'schema_version': 1, 'runtime_protocol': 1, 'frontend_protocol': 1,
        'extension_protocol': 1, 'config_schemas': [1, 2]}))
    paths.at('/opt/ala/current').symlink_to(directory)
    return directory


def fake_signed_download(monkeypatch, archive):
    digest = hashlib.sha256(archive).hexdigest() + '  ala-2.0.0.tar.gz\n'
    monkeypatch.setattr(update, 'fetch', lambda url, *a, **k: digest.encode() if url.endswith('.sha256') else
                        b'{}' if url.endswith('.sigstore.json') else archive)
    monkeypatch.setattr(signature, 'verify_blob', lambda *a, **k: {
        'state': 'verified', 'verifier_version': signature.COSIGN_VERSION, 'tag': 'v2.0.0'})


@pytest.mark.parametrize('override', [False, {'schema_version': 2}, {'schema_version': True},
    {'runtime_protocol': 2}, {'frontend_protocol': 2}, {'extension_protocol': 2},
    {'config_schemas': []}, {'config_schemas': [True]}, {'config_schemas': [1, 1]}, {'config_schemas': [2]}])
def test_signed_incompatible_protocol_never_invokes_installer_or_changes_current(tmp_path, monkeypatch, override):
    paths = Paths(tmp_path)
    before = installed_fixture(paths)
    metadata = False if override is False else {
        'schema_version': 1, 'runtime_protocol': 1, 'frontend_protocol': 1,
        'extension_protocol': 1, 'config_schemas': [1, 2]} | override
    _, archive = signed_fixture(tmp_path, metadata)
    fake_signed_download(monkeypatch, archive)
    monkeypatch.setattr(update.subprocess, 'run', lambda *a, **k: pytest.fail('incompatible installer executed'))
    with pytest.raises(ValueError): update.install_release('v2.0.0', require_signature=True, paths=paths)
    assert paths.at('/opt/ala/current').resolve() == before
    assert not paths.at('/var/lib/ala/trust').exists()


@pytest.mark.parametrize('location', ['current', 'committed', 'desired'])
def test_signed_candidate_must_support_each_existing_config_schema(tmp_path, monkeypatch, location):
    from ala.config import Config, ScreenConfig
    from ala.runtime import RuntimeStore
    paths = Paths(tmp_path)
    before = installed_fixture(paths)
    schema_two = Config(schema_version=2, user='user', screens=(ScreenConfig(id='default', url='https://example.com/'),))
    if location == 'current':
        paths.config.parent.mkdir(parents=True)
        paths.config.write_text(json.dumps(schema_two.to_dict()))
    else:
        store = RuntimeStore(paths)
        store.bootstrap('default', before, schema_two if location == 'committed' else Config(user='user'))
        if location == 'desired': store.stage('default', before, schema_two)
    snapshots = {p: p.read_bytes() for p in paths.root.rglob('*.json')}
    _, archive = signed_fixture(tmp_path, {'schema_version': 1, 'runtime_protocol': 1,
        'frontend_protocol': 1, 'extension_protocol': 1, 'config_schemas': [1]})
    fake_signed_download(monkeypatch, archive)
    monkeypatch.setattr(update.subprocess, 'run', lambda *a, **k: pytest.fail('unsupported config installer executed'))
    with pytest.raises(ValueError): update.install_release('v2.0.0', require_signature=True, paths=paths)
    assert paths.at('/opt/ala/current').resolve() == before
    assert snapshots == {p: p.read_bytes() for p in snapshots}
    assert not paths.at('/var/lib/ala/trust').exists()


def test_compatible_signed_candidate_requests_locked_install_recheck(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    installed_fixture(paths)
    _, archive = signed_fixture(tmp_path)
    fake_signed_download(monkeypatch, archive)
    calls = []
    monkeypatch.setattr(update.subprocess, 'run', lambda *a, **k: calls.append((a, k)))
    update.install_release('v2.0.0', require_signature=True, paths=paths)
    assert len(calls) == 1
    assert calls[0][1]['env']['ALA_REQUIRE_COMPATIBILITY'] == '1'
