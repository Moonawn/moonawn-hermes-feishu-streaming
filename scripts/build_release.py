"""Build a deterministic source-only plugin in dist; never install or overwrite another build."""
from pathlib import Path
import argparse
import hashlib
import io
import json
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(output_dir=None):
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    version = project['version']
    plugin = dict(line.split(': ', 1) for line in (ROOT / 'plugin.yaml').read_text().splitlines()
                  if line and not line.startswith('#'))
    if plugin['name'] != 'moonawn-feishu' or plugin['version'] != version:
        raise ValueError('Plugin metadata/version mismatch')
    version_line = f'__version__ = "{version}"'
    if version_line not in (ROOT / 'src/moonawn_feishu/__init__.py').read_text():
        raise ValueError('Module version mismatch')
    files = {'__init__.py': ROOT / '__init__.py', 'plugin.yaml': ROOT / 'plugin.yaml',
             'README.md': ROOT / 'README.md', 'README.en.md': ROOT / 'README.en.md',
             'LICENSE': ROOT / 'LICENSE',
             'ACKNOWLEDGEMENTS.md': ROOT / 'ACKNOWLEDGEMENTS.md'}
    files['scripts/doctor.py'] = ROOT / 'scripts/doctor.py'
    for source in sorted((ROOT / 'src/moonawn_feishu').glob('*.py')):
        files['moonawn_feishu/' + source.name] = source
    for name in ('installation.md', 'architecture.md', 'acceptance.md', 'session-experience.md',
                 'feishu-api-contract.md', 'THIRD_PARTY_NOTICES.md', 'source-lock.json',
                 'host-contract.json', 'feature-host-contract.json'):
        files['docs/' + name] = ROOT / 'docs' / name
    for name in ('cover.png', 'streaming.png', 'agentloop.png', 'sessions.png',
                 'feishu-web.png', 'feishu-web-demo.gif', 'README.md'):
        files['assets/' + name] = ROOT / 'assets' / name
    hashes = {name: hashlib.sha256(source.read_bytes()).hexdigest() for name, source in files.items()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, source in sorted(files.items()):
            info = zipfile.ZipInfo('moonawn-feishu/' + name, (2026, 10, 27, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    data = buffer.getvalue()
    out = output_dir or ROOT / 'dist'
    target = out / f'moonawn-feishu-{version}.zip'
    if target.exists() and target.read_bytes() != data:
        raise FileExistsError(f'{target.name} has different contents; use a new version or output directory')
    out.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with target.open('xb') as stream:
            stream.write(data)
    report = {'version': version, 'artifact': target.name, 'sha256': hashlib.sha256(data).hexdigest(),
              'bytes': len(data), 'files': hashes, 'build_installs_plugin': False,
              'acceptance_report': '../docs/acceptance.md'}
    (out / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    (out / 'SHA256SUMS').write_text(report['sha256'] + '  ' + target.name + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'files'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    build(parser.parse_args().output_dir)
