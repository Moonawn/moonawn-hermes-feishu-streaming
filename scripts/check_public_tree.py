"""Check the exact Git index or a clean export; print locations, never matched secrets."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DOCS = {'architecture.md', 'installation.md', 'acceptance.md', 'session-experience.md',
        'feishu-api-contract.md', 'release-readme.md', 'THIRD_PARTY_NOTICES.md',
        'source-lock.json', 'host-contract.json', 'feature-host-contract.json'}
SCRIPTS = {'build_release.py', 'build_preview.py', 'verify.py', 'verify_container.py',
           'doctor.py',
           'verify_host.py', 'host_smoke.py', 'verify_preview.cjs', 'fetch_references.py',
           'generate_activity_asset.py', 'check_public_tree.py'}
ROOT_FILES = {'.gitignore', '.gitattributes', 'README.md', 'README.en.md', 'LICENSE', 'ACKNOWLEDGEMENTS.md', 'pyproject.toml',
              'plugin.yaml', '__init__.py'}
# Reviewed artwork, synthetic UI and cropped live test captures; changes require review.
ASSET_SHA256 = {'assets/agentloop.png': '1acc745d49d01473fae77133958940d8d6542b995900744a5d1071d7eab3f5d1',
 'assets/cover.png': '1f12b3dbf21f094add6df665e75f88a51cf71efa94d27e34177f0e38e0769d9a',
 'assets/sessions.png': '8ba1c8d46b84ae42873dd7d1c4ba887d5555031d6f21faf43b5bc22aa457667d',
 'assets/streaming.png': 'faf8fc8ba97d79e96677f8bdbcf931260ddbc9f6c68e4e5280bec62ea40a5021'}
ASSET_SHA256.update({'assets/feishu-web.png': 'c5c3164004721b40344a4e91c492df6ea7ecbfed81a687837d8e2f15f6fcf78e', 'assets/feishu-web-demo.gif': '5d62a4ab210a0abdf886ddb42c26a1dfd2a2563c37e276b21cc3cf57502e2da1'})
PATTERNS = {
    'live_feishu_id': r'\b(?:oc|ou|om|omt|cli)_x?[a-f0-9]{16,}\b',
    'personal_home': r'/(?:Users|home)/(?!private\b|example\b|demo\b|user\b)[A-Za-z0-9_.-]+',
    'tenant_url': r'https?://(?!open\.|www\.)[a-z0-9-]+\.feishu\.cn',
    'github_token': r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})',
    'api_key': r'\bsk-[A-Za-z0-9_-]{24,}',
    'aws_key': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'private_key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'credential_url': r'https?://[^\s/:]+:[^\s/@]+@',
    'email': r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
}


def allowed(name):
    p = Path(name)
    if name in ROOT_FILES or name in ASSET_SHA256 or name in {'.github/workflows/ci.yml', 'assets/README.md'}:
        return True
    return ((p.parent.as_posix() == 'src/moonawn_feishu' and p.suffix == '.py')
            or (p.parent.as_posix() == 'tests' and p.name.startswith('test_') and p.suffix == '.py')
            or (p.parent.as_posix() == 'tests/fixtures' and p.suffix == '.json')
            or (p.parent.as_posix() == 'docs' and p.name in DOCS)
            or (p.parent.as_posix() == 'scripts' and p.name in SCRIPTS)
            or name == 'preview/template.html')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree', type=Path, help='Inspect all files in a fresh export instead of the Git index')
    args = parser.parse_args()
    if args.tree:
        files = {p.relative_to(args.tree).as_posix(): p.read_bytes()
                 for p in args.tree.rglob('*') if p.is_file()}
    else:
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        files = {name: subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)
                 for name in names if name}
    failures = []
    if not files:
        failures.append({'rule': 'empty_tree'})
    for name, data in files.items():
        if not allowed(name):
            failures.append({'path': name, 'rule': 'not_in_public_allowlist'})
        if name in ASSET_SHA256:
            signature = b'GIF89a' if name.endswith('.gif') else b'\x89PNG\r\n\x1a\n'
            if (hashlib.sha256(data).hexdigest() != ASSET_SHA256[name]
                    or not data.startswith(signature)):
                failures.append({'path': name, 'rule': 'unreviewed_image'})
            continue
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            failures.append({'path': name, 'rule': 'unexpected_binary'})
            continue
        for rule, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text, re.I if rule == 'tenant_url' else 0):
                failures.append({'path': name, 'line': text.count('\n', 0, match.start()) + 1, 'rule': rule})
    print(json.dumps({'files_checked': len(files), 'passed': not failures, 'findings': failures}, indent=2))
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
