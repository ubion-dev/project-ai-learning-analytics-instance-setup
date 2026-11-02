#!/usr/bin/env python3
"""Fail-closed public-data gate; never prints matched values or tool stderr."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

PUBLIC_HOSTS = {
    'github.com', 'raw.githubusercontent.com', 'dl.rockylinux.org',
    'mirrors.rockylinux.org', 'dl.google.com', 'www.google.com',
    'support.google.com', 'www.chromium.org', 'chromium.googlesource.com',
    'download.docker.com', 'docs.docker.com', 'nvidia.github.io',
    'developer.download.nvidia.com', 'docs.nvidia.com', 'docs.redhat.com',
    'dnf.readthedocs.io', 'help.gnome.org', 'www.intel.com',
    'networkmanager.pages.freedesktop.org', 'dbus.freedesktop.org',
    'www.freedesktop.org', 'textual.textualize.io',
}
EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
IPV6 = re.compile(r'(?<![\w:])(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:.]+(?:%[\w.-]+)?')
URL = re.compile(r'\b(?:https?|ssh|tcp)://[^\s\x22\x27`<>\\]+')


def git(*args, data=None):
    p = subprocess.run(['git', *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError('git inspection failed')
    return p.stdout


def findings(data):
    text = data.decode('utf8', 'replace')
    hits = set()
    for token in IPV4.findall(text) + IPV6.findall(text):
        try:
            address = ipaddress.ip_address(token.split('%')[0])
        except ValueError:
            continue
        if not address.is_loopback:
            hits.add('non-loopback IP')
    for email in EMAIL.findall(text):
        domain = email.rsplit('@', 1)[1].lower()
        if domain not in ('users.noreply.github.com', 'example.com', 'example.org', 'example.net'):
            hits.add('personal email')
    if re.search(r'/Users/[A-Za-z0-9_.-]+/|/home/(?!user/|\$|\{)[a-zA-Z][\w.-]+/', text):
        hits.add('personal home path')
    for raw in URL.findall(text):
        # Template URLs are reviewed as source; concrete hosts are checked below.
        try:
            host = urlsplit(raw).hostname
        except ValueError:
            continue
        if not host or any(c in host for c in '${}'):
            continue
        if host in PUBLIC_HOSTS or host == 'localhost' or host in ('host', 'proxy', 'loghost'):
            continue
        if any(host == d or host.endswith('.' + d) for d in ('example.com', 'example.org', 'example.net')):
            continue
        try:
            if ipaddress.ip_address(host).is_loopback:
                continue
        except ValueError:
            pass
        hits.add('unapproved URL host')
    return sorted(hits)


def identity(value):
    # Commit identity must remain linked to a GitHub account without personal mail.
    return bool(re.fullmatch(rb'[^<>\s]+@users\.noreply\.github\.com', value.strip()))


def secret_scan(blobs):
    exe = shutil.which('gitleaks')
    if not exe:
        raise RuntimeError('gitleaks is required; install the official distribution before committing')
    with tempfile.TemporaryDirectory(prefix='ala-public-check-') as temporary:
        root = Path(temporary)
        (root / 'rules.toml').write_text('[extend]\nuseDefault = true\n')
        payload = root / 'payload'
        payload.mkdir()
        for i, blob in enumerate(blobs):
            (payload / str(i)).write_bytes(blob)
        env = {k: v for k, v in os.environ.items() if not k.startswith('GITLEAKS_')}
        p = subprocess.run([exe, 'dir', str(payload), '--config', str(root / 'rules.toml'),
                            '--gitleaks-ignore-path', str(root / 'no-ignore'),
                            '--ignore-gitleaks-allow', '--redact', '--no-banner',
                            '--max-archive-depth', '4', '--max-decode-depth', '4'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if p.returncode:
            raise RuntimeError('secret scan failed or found sensitive data; values suppressed')


def inspect(staged=False, refs=None):
    blobs = {}
    errors = []
    if staged:
        entries = git('ls-files', '--stage', '-z').split(b'\0')
        for entry in entries:
            if not entry:
                continue
            meta, name = entry.split(b'\t', 1)
            mode, sha, stage = meta.split()
            if stage != b'0' or mode == b'160000':
                raise RuntimeError('unmerged index or submodule cannot be verified')
            blobs[sha] = git('cat-file', 'blob', sha.decode())
            blobs[b'path-' + sha] = name
        for var in ('GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT'):
            value = git('var', var)
            blobs[var.encode()] = value
            email = re.search(rb'<([^<>]+)>', value)
            if not email or not identity(email[1]):
                errors.append('commit identity must use GitHub noreply email')
    else:
        refs = refs or ['--all']
        commits = git('rev-list', *refs).splitlines()
        for sha in commits:
            content = git('cat-file', 'commit', sha.decode())
            header, message = content.split(b'\n\n', 1)
            for field in (b'author ', b'committer '):
                line = next(x for x in header.splitlines() if x.startswith(field))
                email = re.search(rb'<([^<>]+)>', line)
                if not email or not identity(email[1]):
                    errors.append('historical commit identity must use GitHub noreply email')
            blobs[b'commit-' + sha] = content
        for entry in git('rev-list', '--objects', *refs).splitlines():
            sha, _, name = entry.partition(b' ')
            blobs[b'path-' + sha] = name
            kind = git('cat-file', '-t', sha.decode()).strip()
            if kind == b'blob':
                blobs[sha] = git('cat-file', 'blob', sha.decode())
            elif kind == b'tag':
                tag = git('cat-file', 'tag', sha.decode())
                email = re.search(rb'^tagger .*<([^<>]+)>', tag, re.M)
                if email and not identity(email[1]):
                    errors.append('tagger identity must use GitHub noreply email')
                blobs[b'tag-' + sha] = tag
    for data in blobs.values():
        errors.extend(findings(data))
    if errors:
        raise RuntimeError('; '.join(sorted(set(errors))))
    secret_scan(blobs.values())
    return len(blobs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--staged', action='store_true')
    group.add_argument('--history', action='store_true')
    group.add_argument('--pre-push', action='store_true')
    group.add_argument('--message-file', type=Path)
    args = parser.parse_args()
    try:
        refs = None
        if args.message_file:
            data = args.message_file.read_bytes()
            if findings(data):
                raise RuntimeError('commit message violates public-data policy')
            secret_scan([data])
            print('Public-data commit message check passed')
            return 0
        if args.pre_push:
            refs = []
            for line in sys.stdin:
                fields = line.split()
                if len(fields) != 4:
                    raise RuntimeError('invalid pre-push input')
                sha = fields[1]
                if set(sha) == {'0'}:
                    continue
                if not re.fullmatch(r'[0-9a-f]{40,64}', sha):
                    raise RuntimeError('invalid push object')
                refs.append(sha)
            if not refs:
                return 0
        count = inspect(staged=args.staged, refs=refs)
        print('Public-data check passed:', count, 'objects inspected')
        return 0
    except (RuntimeError, OSError, ValueError, StopIteration):
        # Neither rejected data nor subprocess output may enter public CI logs.
        print('Public-data check BLOCKED. Check IP/email/URL rules, identities and Gitleaks availability; no rejected values logged.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
