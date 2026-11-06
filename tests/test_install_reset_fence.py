"""Direct installation cannot step over a durable unfinished reset fence."""
import json
import pytest
from test_install_swap_fence import installation,existing_installation,installed_state


@pytest.mark.parametrize('mode',['pending','missing-pending','symlink','writable'])
def test_reset_fence_preserves_installed_release(installation,mode):
    bundle,_=installation.bundle('9.0.0')
    existing_installation(installation)
    fence=installation.root/'var/lib/ala-runtime/display-reset.json'
    fence.parent.mkdir(parents=True,exist_ok=True)
    fence.write_text(json.dumps({'pending':True} if mode!='missing-pending' else {}))
    if mode=='writable':
        fence.chmod(0o666)
    if mode=='symlink':
        outside=installation.tmp/'synthetic-reset.json'
        outside.write_text(fence.read_text())
        fence.unlink();fence.symlink_to(outside)
    before=installed_state(installation)
    result=installation.install(bundle)
    assert result.returncode==1 and 'DID 초기화' in result.stderr
    assert installed_state(installation)==before
