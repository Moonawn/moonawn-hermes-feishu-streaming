from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
import json
import sys
import tempfile
import unittest

from moonawn_feishu.model import Event, Phase, Identity, Route
from moonawn_feishu.render import render, tool_rows
from moonawn_feishu.store import Store
from moonawn_feishu.telemetry import DurationHistory, TurnTelemetry, metric_details
from moonawn_feishu.tool_display import native_action


class LearningTests(unittest.TestCase):
    def test_comparable_progress_filters_short_tasks_and_prefers_tool_family(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'own.db')
            history = DurationHistory(store, 'owner')
            fast = dict(iteration=1, tools=1, family='compute')
            slow = dict(iteration=4, tools=3, family='read')
            current = dict(iteration=3, tools=2, family='read')
            for i in range(8): history.record('fast'+str(i), 'm', True, 4+i, shape=fast)
            for i in range(4): history.record('slow'+str(i), 'm', True, 30+i, shape=slow)
            eta = history.estimate('m', True, 10, shape=current)
            self.assertEqual((eta['state'], eta['samples'], eta['total_samples']), ('cold', 4, 12))
            history.record('slow4', 'm', True, 34, shape=slow)
            eta = history.estimate('m', True, 10, shape=current)
            self.assertEqual((eta['basis'], eta['low'], eta['high']), ('similar', 20, 24))
            store.close()
            store = Store(Path(folder) / 'own.db')
            self.assertEqual(DurationHistory(store,'owner').estimate('m',True,10,shape=current), eta)
            self.assertEqual(DurationHistory(store,'another').estimate('m',True,0)['samples'], 0)
            store.close()

    def test_legacy_dedup_finite_bounds_and_wide_variance(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'own.db'); h=DurationHistory(store,'owner')
            store.save_operation(h.key('m',False), {'samples': [['old',12]]})
            self.assertFalse(h.record('old','m',False,900))
            self.assertFalse(h.record('bad','m',False,float('nan')))
            self.assertFalse(h.record('bad','m',False,float('inf')))
            for i,value in enumerate([10,11,80,100]): h.record(str(i),'m',False,value)
            self.assertEqual(h.estimate('m',False,1)['state'],'variable')
            eta=h.estimate('m',False,1,shape=dict(iteration=1,tools=0,family='none'))
            self.assertEqual((eta['state'],eta['samples'],eta['total_samples']),('cold',0,5))
            self.assertEqual(store.operation(h.key('m',False)), {'samples': [['old',12]]})
            for i in range(130): h.record('bound'+str(i),'m',False,20)
            self.assertEqual(len(h.samples('m',False)),120)
            store.close()

    def test_only_success_without_human_wait_learns_and_terminal_keeps_sample_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'own.db'); h=DurationHistory(store,'owner')
            for name,success,waiting in [('ok',True,False),('failed',False,False),('human',True,True)]:
                now=[0]; t=TurnTelemetry(h,name,clock=lambda:now[0]); t.attach(SimpleNamespace(provider='p',model='m'))
                t.step(1); t.tool('tool',name='terminal'); t.waiting=waiting
                now[0]=10; t.tool('tool',done=True,name='terminal'); t.step(2)
                frozen=t.finish(success=success)
                self.assertEqual(frozen['eta']['recorded'],name=='ok')
                self.assertEqual(frozen['eta']['samples'],1)
                t.finish(success=True)  # never learn the same run twice
                p=Phase(Identity(Route('a','a','p','c'),name),metrics=frozen,terminal='final')
                text=json.dumps(render(p),ensure_ascii=False)
                self.assertIn('耗时参考已积累 1 次',text)
                self.assertNotIn('预计还需',text)
            row=h.samples('p/m',True)[0]
            self.assertEqual(row['shape'],dict(iteration=2,tools=1,family='compute'))
            self.assertEqual(set(row),{'run','elapsed','shape'})  # no prompts or commands in learning records
            store.close()


class DetailTests(unittest.TestCase):
    def test_old_failure_survives_thousand_successes_and_payload_stays_bounded(self):
        p=Phase(Identity(Route('a','a','p','c'),'run'))
        for i in range(1,1001):
            p.apply(Event(p.identity,str(i),i,'tool','terminal',str(i),'failed' if i==1 else 'done',
                          {'iteration':i,'preview':'compute','outcome':'[exit 7]' if i==1 else '', 'elapsed':.2}))
        p.metrics={'iteration':1000,'timeline':[{'iteration':i,'model_elapsed':.1,'tools_started':1,'tools_done':1} for i in range(996,1001)]}
        p.terminal='final'
        restored=Phase.decode(p.encode()); card=render(restored)
        text=json.dumps(card,ensure_ascii=False)
        self.assertIn('exit 7',text); self.assertIn('较早关键调用',text)
        self.assertEqual(len(restored.tools),12); self.assertEqual(restored.tool_count,1000)
        self.assertLess(len(text.encode()),10000)
        self.assertNotIn('loop_500',text)

    def test_only_identical_successes_are_merged_with_explicit_count(self):
        base=dict(label='read_file',action='Reading',preview='public reference',elapsed=1,state='done')
        rows=tool_rows([base,dict(base),dict(base,state='failed',outcome='[denied]'),dict(base,state='running')],False)
        self.assertEqual(len(rows),3)
        self.assertIn('×2',rows[0]['content']); self.assertIn('累计 2.0s',rows[0]['content'])
        self.assertIn('denied',rows[1]['content']); self.assertIn('执行中',rows[2]['content'])
        self.assertTrue(all(r['content'].count('\n')==1 for r in rows))

    def test_native_action_is_not_generated_from_tool_arguments(self):
        module=ModuleType('agent.display'); module.get_tool_verb=lambda name:'Reading reference'
        with patch.dict(sys.modules,{'agent.display':module}):
            self.assertEqual(native_action('read_file'),'Reading reference')
        module.get_tool_verb=lambda name: (_ for _ in ()).throw(RuntimeError())
        with patch.dict(sys.modules,{'agent.display':module}):
            self.assertEqual(native_action('read_file'),'')
