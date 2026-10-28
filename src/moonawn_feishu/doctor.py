"""Read-only installation checks. No host imports, sockets, credentials or writes."""
from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

from . import __version__
from .hermes_bridge import inspect_hermes

SUPPORTED_HERMES = '0.22.1'


def source_version(root):
    # Hermes 0.22.1 resolves its lazy __version__ from this installation stamp.
    try:
        stamp = json.loads((root / 'install-stamp.json').read_text(encoding='utf-8-sig'))
        if isinstance(stamp, dict) and isinstance(stamp.get('baseVersion'), str):
            return stamp['baseVersion']
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        tree = ast.parse((root / 'hermes_cli/__init__.py').read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
    except (OSError, SyntaxError, UnicodeError):
        pass
    return None


def diagnose(root, home=None, *, source_only=False, require_running=False, now=None):
    checks = []
    def add(name, status, detail):
        checks.append({'name': name, 'status': status, 'detail': detail})
    add('python', 'pass' if sys.version_info >= (3, 11) else 'fail', '需要 Python 3.11+')
    add('platform', 'pass' if os.name == 'posix' else 'fail', '当前支持 Unix 进程内插件')
    version = source_version(root)
    add('hermes_version', 'pass' if version == SUPPORTED_HERMES else 'fail',
        f'固定兼容目标 Hermes {SUPPORTED_HERMES}；' + ('版本匹配' if version == SUPPORTED_HERMES else '未匹配，请勿直接启用'))
    contract = inspect_hermes(root)
    failed = [x['method'] for x in contract['checks'] if not x['passed']]
    add('host_contract', 'fail' if failed else 'pass',
        '需复核：' + ', '.join(failed) if failed else f"{len(contract['checks'])} 项静态宿主契约通过")
    if not source_only:
        try:
            sdk = importlib.metadata.version('lark-oapi')
        except importlib.metadata.PackageNotFoundError:
            sdk = None
        add('sdk', 'pass' if sdk == '1.7.5' else 'warn' if sdk else 'fail',
            '已验证 SDK 1.7.5' if sdk == '1.7.5' else '当前解释器未使用已验证 SDK；请在 Hermes 的 Python 环境检查')
    if home:
        installed = home / 'plugins/moonawn-feishu'
        add('installed_plugin', 'pass' if (installed / 'plugin.yaml').is_file() else 'fail',
            '所选 home 中的插件目录' if (installed / 'plugin.yaml').is_file() else '所选 home 尚未安装插件')
        # Presence alone is not proof of an enabled conflict; effective factory
        # is verified separately using a recent runtime-owned snapshot.
        others = []
        for manifest in (home / 'plugins').glob('*/plugin.yaml'):
            if manifest.parent == installed:
                continue
            try:
                text = manifest.read_text().lower()
                if any(marker in text for marker in ('feishu', 'lark-stream', 'hls', 'hfc')):
                    others.append(manifest)
            except (OSError, UnicodeError):
                continue
        add('other_feishu_plugins', 'warn' if others else 'pass',
            '发现其他飞书插件目录，请核对启用顺序' if others else '未发现其他飞书插件目录')
        try:
            health = json.loads((home / 'moonawn-feishu/health.json').read_text())
            age = (time.time() if now is None else now) - float(health['updated_at'])
            fresh = health.get('schema') == 1 and health.get('running') is True and 0 <= age <= 20
        except (OSError, ValueError, KeyError, TypeError):
            health, fresh = {}, False
        add('runtime_snapshot', 'pass' if fresh else 'fail' if require_running else 'warn',
            '近期运行快照可用' if fresh else '缺少近期运行快照；启动目标网关后重查')
        if fresh:
            active = health.get('adapter_active') is True and health.get('factory_matches') is True
            add('active_factory', 'pass' if active else 'fail', '实际 Feishu factory 与本插件一致' if active else '实际 factory 不匹配')
            add('loaded_version', 'pass' if health.get('plugin_version') == __version__ else 'fail',
                '运行版本与诊断工具一致' if health.get('plugin_version') == __version__ else '运行版本与诊断工具不同')
            observed = health.get('consumers_observed', 0) > 0
            add('streaming', 'pass' if observed else 'warn',
                '本次启动已实际创建流式 consumer' if observed else '尚未观察到流式 consumer，请发一条测试任务后重查')
    elif require_running:
        add('runtime_snapshot', 'fail', '--require-running 需要显式 --hermes-home')
    return {'plugin_version': __version__, 'passed': not any(c['status'] == 'fail' for c in checks),
            'checks': checks, 'network_used': False, 'files_changed': False,
            'live_delivery_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-root', type=Path, required=True)
    parser.add_argument('--hermes-home', type=Path)
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--require-running', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    report = diagnose(args.hermes_root, args.hermes_home, source_only=args.source_only,
                      require_running=args.require_running)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report['checks']:
            print(f"[{item['status'].upper()}] {item['name']}: {item['detail']}")
        print('只读诊断；未发送消息、修改配置或启动网关。')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
