import asyncio
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from moonawn_feishu import __version__
from moonawn_feishu.doctor import diagnose, source_version
from moonawn_feishu.diagnostics import Diagnostics
from moonawn_feishu.hermes_bridge import inspect_hermes
from moonawn_feishu.render import short_elapsed, tool_rows


class DoctorTests(unittest.TestCase):
    def test_untrusted_host_source_is_parsed_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'hermes_cli').mkdir()
            sentinel = root / 'must-not-exist'
            (root / 'hermes_cli/__init__.py').write_text(
                '__version__ = "0.22.1"\nraise RuntimeError("never import")\n')
            self.assertEqual(source_version(root), '0.22.1')
            report = diagnose(root, source_only=True)
            self.assertFalse(report['passed'])
            self.assertFalse(sentinel.exists())
            self.assertNotIn(str(root), json.dumps(report))
            (root / 'install-stamp.json').write_text('{"baseVersion":"0.22.1"}')
            (root / 'hermes_cli/__init__.py').write_text('__version__: str\n')
            self.assertEqual(source_version(root), '0.22.1')

    def test_malformed_host_reports_failure_without_traceback_or_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'gateway').mkdir()
            (root / 'gateway/run_turn.py').write_text('not python !!!!')
            self.assertFalse(inspect_hermes(root)['static_surface_passed'])

    def test_stale_health_cannot_prove_active_factory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'plugins/moonawn-feishu').mkdir(parents=True)
            (root / 'plugins/moonawn-feishu/plugin.yaml').write_text('name: moonawn-feishu\n')
            (root / 'moonawn-feishu').mkdir()
            health = dict(schema=1, running=True, updated_at=10, adapter_active=True,
                          factory_matches=True, plugin_version=__version__, consumers_observed=1)
            path = root / 'moonawn-feishu/health.json'; path.write_text(json.dumps(health))
            with patch('moonawn_feishu.doctor.source_version', return_value='0.22.1'), patch(
                    'moonawn_feishu.doctor.inspect_hermes', return_value={'checks': []}):
                stale = diagnose(root, root, source_only=True, require_running=True, now=100)
                self.assertFalse(stale['passed'])
                self.assertNotIn('active_factory', [x['name'] for x in stale['checks']])
                fresh = diagnose(root, root, source_only=True, require_running=True, now=11)
                self.assertTrue(fresh['passed'])
                health['factory_matches'] = False; path.write_text(json.dumps(health))
                self.assertFalse(diagnose(root, root, source_only=True, require_running=True, now=11)['passed'])

    def test_unknown_timing_and_redundant_success_are_not_fabricated_details(self):
        self.assertEqual(short_elapsed(None), '耗时未记录')
        self.assertEqual(short_elapsed(float('nan')), '耗时未记录')
        self.assertEqual(short_elapsed(.001), '<0.1s')
        tools = [{'label': 'read_file', 'state': 'done', 'elapsed': .002, 'outcome': '已完成'}]
        row = tool_rows(tools, True)[0]['content']
        self.assertEqual(row.count('完成'), 1)
        self.assertIn('&lt;0.1s', row)
        tools[0]['outcome'] = '核对 3 份文件'
        self.assertIn('核对 3 份文件', tool_rows(tools, True)[0]['content'])


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_snapshot_is_marked_stopped_on_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'health.json'
            monitor = Diagnostics(path, lambda: {'adapter_active': True}, interval=.001)
            for i in range(140): monitor.record('stop_dispatch', i / 1000)
            monitor.start()
            await asyncio.sleep(.005)
            self.assertTrue(json.loads(path.read_text())['running'])
            await monitor.close()
            snapshot = json.loads(path.read_text())
            self.assertFalse(snapshot['running'])
            self.assertEqual(len(snapshot['timings_ms']['stop_dispatch']), 128)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
