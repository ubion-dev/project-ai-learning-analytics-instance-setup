"""Fixed command boundary for the system DNF helper."""
import json
from pathlib import Path
import re


def validate_token(token):
    if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{64}', token):
        raise ValueError('Podman 제거 승인 정보가 잘못되었습니다.')
    return token


def inspect_cleanup(host, release):
    command = ['/usr/bin/python3', '-I',
               str(Path(release) / 'scripts/rocky-linux/lib/podman_cleanup.py')]
    result = host.run(command, timeout=150)
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get('state') not in {'ready', 'blocked', 'not_required'}:
            raise ValueError()
        if value.get('state') == 'ready':
            validate_token(value.get('token'))
            if (not isinstance(value.get('packages'), list) or not value['packages']
                    or not all(isinstance(p, str) for p in value['packages'])
                    or not isinstance(value.get('accounts'), list)
                    or not all(isinstance(a, dict) and isinstance(a.get('user'), str)
                               for a in value['accounts'])):
                raise ValueError()
    except (ValueError, TypeError):
        return {'state': 'blocked', 'reason': 'Podman 사용 여부와 제거 목록을 확인하지 못했습니다.'}
    if result.returncode and value.get('state') != 'blocked':
        return {'state': 'blocked', 'reason': 'Podman 조회에 실패했습니다.'}
    return value
