import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from moonawn_feishu.activity_asset import prepare_ripple, ripple_bytes
from moonawn_feishu.model import Phase, Identity, Route
from moonawn_feishu.render import render, fingerprint
from moonawn_feishu.store import Store
from moonawn_feishu.telemetry import TurnTelemetry

class ActivityTests(unittest.TestCase):
    def phase(self):
        p=Phase(Identity(Route('app','app','profile','chat'),'turn'))
        p.metrics={'iteration':3}
        return p

    def test_animation_follows_execution_and_motion_preference(self):
        p=self.phase()
        title=lambda:render(p,ripple_key='img_fixture')['body']['elements'][-1]['header']['title']
        self.assertEqual(title()['icon']['img_key'],'img_fixture')
        for attr,value in [('terminal','final'),('terminal','cancel'),('terminal','error'),('status','waiting'),('typing',False),('seal','closed')]:
            p=self.phase();setattr(p,attr,value)
            self.assertNotIn('icon',title())
        p=self.phase()
        # No time-varying render/frame counter and no duplicate loop label.
        self.assertEqual(fingerprint(render(p)),fingerprint(render(p)))
        self.assertNotIn('Agent loop',title()['content'])

    def test_groups_keep_parallel_start_order_and_model_only_iteration(self):
        p=self.phase()
        p.tools={'z':{'label':'web_search','state':'done','iteration':1,'preview':'first'},
                 'a':{'label':'terminal','state':'failed','iteration':1,'preview':'second'},
                 'b':{'label':'web_extract','state':'running','iteration':2}}
        p.tool_count=3
        restored=Phase.decode(p.encode())
        rows=render(restored)['body']['elements'][-1]['elements']
        groups=[r for r in rows if r['tag']=='collapsible_panel' and r['element_id'] != 'loop_usage']
        self.assertEqual(len(groups),2)
        first=json.dumps(groups[0],ensure_ascii=False)
        self.assertLess(first.index('first'),first.index('second'))
        self.assertIn('未完成',first)
        self.assertTrue(any('03' in r.get('content','') for r in rows))
        self.assertIn('模型阶段未记录',first)
        self.assertFalse(groups[0]['expanded'])
        self.assertNotIn('\n',groups[0]['header']['title']['content'])
        p.terminal='error'
        self.assertIn('状态未回传',json.dumps(render(p),ensure_ascii=False))

    def test_native_stage_intervals_parallel_tools_and_late_completion(self):
        now=[0.0];meter=TurnTelemetry(clock=lambda:now[0])
        meter.step(1)
        now[0]=2.3;meter.tool('a');meter.tool('b')
        now[0]=4;meter.tool('a',done=True);meter.tool('a',done=True)
        now[0]=5;meter.step(2);meter.step(2)
        now[0]=6;meter.tool('b',done=True)
        timeline=meter.snapshot()['timeline']
        self.assertEqual(timeline[0]['model_elapsed'],2.3)
        self.assertEqual(timeline[0]['tools_started'],2)
        self.assertEqual(timeline[0]['tools_done'],2)
        self.assertEqual(timeline[1]['model_elapsed'],1)
        now[0]=8;frozen=meter.finish()
        now[0]=99;meter.step(3);meter.tool('c')
        self.assertEqual(meter.snapshot(),frozen)
        self.assertEqual(frozen['timeline'][1]['model_elapsed'],3)
        self.assertFalse(frozen['timeline'][1]['model_active'])

    def test_thousand_rounds_have_bounded_payload_and_no_fabricated_history(self):
        from moonawn_feishu.model import Event
        now=[0.0];meter=TurnTelemetry(clock=lambda:now[0]);p=self.phase()
        for i in range(1,1001):
            meter.step(i);now[0]+=.5;meter.tool(str(i));now[0]+=.2;meter.tool(str(i),done=True)
            p.apply(Event(p.identity,str(i),i,'tool','terminal',str(i),'done',
                          {'iteration':i,'elapsed':.2,'preview':'核对数据'}))
        p.metrics=meter.finish();p.terminal='final'
        panel=render(p)['body']['elements'][-1]
        groups=[r for r in panel['elements'] if r['tag']=='collapsible_panel' and r['element_id'] != 'loop_usage']
        self.assertEqual(len(groups),5)
        self.assertEqual(len(meter.steps),5)
        self.assertEqual(len(p.tools),12)
        self.assertLess(len(json.dumps(panel,ensure_ascii=False).encode()),7000)
        self.assertIn('共 1000 轮',json.dumps(panel,ensure_ascii=False))
        self.assertNotIn('loop_995',json.dumps(panel))
        self.assertTrue(all(not x['expanded'] for x in groups))
        self.assertTrue(all('\n' not in x['header']['title']['content'] for x in groups))

    def test_unknown_completion_does_not_fabricate_model_boundary(self):
        now=[0.0];meter=TurnTelemetry(clock=lambda:now[0]);meter.step(1)
        now[0]=1;meter.tool('missing-start',done=True)
        self.assertTrue(meter.snapshot()['timeline'][0]['model_active'])
        self.assertEqual(meter.snapshot()['timeline'][0]['tools_started'],0)

class AssetTests(unittest.IsolatedAsyncioTestCase):
    async def test_asset_cache_is_bound_to_app_profile_and_bytes(self):
        class Wire:
            calls=0
            async def upload_image(self,data):
                self.calls+=1
                assert data.startswith(b'GIF89a')
                return 'img_fixture'
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'test.db');wire=Wire()
            self.assertEqual(await prepare_ripple(store,wire,('a','a','p')),'img_fixture')
            await prepare_ripple(store,wire,('a','a','p'))
            self.assertEqual(wire.calls,1)
            await prepare_ripple(store,wire,('b','b','p'))
            await prepare_ripple(store,wire,('b','b','q'))
            self.assertEqual(wire.calls,3)
            store.close()

    async def test_upload_failure_preserves_static_delivery_and_cancellation(self):
        class Wire:
            async def upload_image(self,data):raise ValueError('failed')
        class Cancel:
            async def upload_image(self,data):raise asyncio.CancelledError()
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'test.db')
            self.assertEqual(await prepare_ripple(store,Wire(),('a','a','p')),'')
            with self.assertRaises(asyncio.CancelledError):await prepare_ripple(store,Cancel(),('a','a','p'))
            store.close()
