import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from moonawn_feishu.engine import Engine
from moonawn_feishu.fake_lark import FakeLark
from moonawn_feishu.plugin import adapter_class
from moonawn_feishu.runtime import Runtime, Scope
from moonawn_feishu.model import Route
from moonawn_feishu.store import Store


class StopAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_is_not_queued_behind_another_topics_slow_command_reply(self):
        entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        lock = asyncio.Lock()

        class Native:
            async def _handle_message_with_guards(self, event):
                async with lock:
                    await self.handle_message(event)

            async def handle_message(self, event):
                if event.get_command() == 'stop':
                    # The native pipeline still decides admission/ACL and scope.
                    self.seen = event
                    if event.allow_gateway_control:
                        stopped.set()
                else:
                    entered.set()
                    await release.wait()

        adapter = adapter_class(Native)()
        busy = SimpleNamespace(get_command=lambda: 'sessions', allow_gateway_control=True)
        stop = SimpleNamespace(get_command=lambda: 'stop', allow_gateway_control=True)
        pending = asyncio.create_task(adapter._handle_message_with_guards(busy))
        await entered.wait()
        request = asyncio.create_task(adapter._handle_message_with_guards(stop))
        try:
            await asyncio.wait_for(stopped.wait(), .25)
            self.assertIs(adapter.seen, stop)
            self.assertFalse(pending.done())
        finally:
            release.set()
            await asyncio.gather(pending, request)

    async def test_stop_preserves_native_permission_rejection(self):
        class Native:
            async def _handle_message_with_guards(self, event):
                return await self.handle_message(event)

            async def handle_message(self, event):
                return 'denied' if not event.allow_gateway_control else 'accepted'

        adapter = adapter_class(Native)()
        event = SimpleNamespace(get_command=lambda: 'stop', allow_gateway_control=False)
        self.assertEqual(await adapter._handle_message_with_guards(event), 'denied')


class RecoveryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopping_during_inflight_update_keeps_other_topic_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'delivery.db')
            fake = FakeLark()
            runtime = Runtime(Engine(store, fake), interval=.001)
            scope = Scope(Route('app', 'bot', 'profile', 'chat', 'topic-a', 'anchor-a'), 'a', 'inbound-a')
            a = runtime.begin(scope)
            a.emit('snapshot', '已生成的 A')
            await a.queue.drain()
            entered, release = asyncio.Event(), asyncio.Event()
            fake.update_barrier = entered, release
            a.emit('snapshot', '已生成的 A 第二段')
            await entered.wait()
            stop = asyncio.create_task(a.cancel())
            await asyncio.sleep(0)
            try:
                self.assertEqual(a.phase.terminal, 'cancel')
                b = runtime.begin(replace(scope, route=replace(scope.route, thread='topic-b', anchor='anchor-b'),
                                          session_key='b', inbound_message_id='inbound-b'))
                self.assertTrue(await asyncio.wait_for(b.commit('B 完整结果'), .25))
                a.emit('snapshot', '迟到结果')
                self.assertEqual(a.phase.body, '已生成的 A 第二段')
            finally:
                release.set()
                await stop
                await runtime.close()
                store.close()

    async def test_restart_after_unknown_update_recovers_same_card_and_exact_final(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'delivery.db'
            store = Store(path)
            fake = FakeLark()
            runtime = Runtime(Engine(store, fake), interval=.001)
            scope = Scope(Route('app', 'bot', 'profile', 'chat'), 'session', 'inbound')
            reply = runtime.begin(scope)
            reply.emit('snapshot', '预览')
            await reply.queue.drain()
            fake.fail_next('update', 'lost_ack')
            self.assertFalse(await reply.commit('完整终稿 END'))
            message = reply.phase.message_id
            await runtime.close()
            store.close()
            # Reopen the closed test DB; no active database copying.
            store = Store(path)
            recovered = Runtime(Engine(store, fake))
            try:
                await recovered.recover_once()
                phase = recovered.engine.phases[reply.identity.key]
                self.assertEqual((phase.message_id, phase.body, phase.delivery, phase.seal),
                                 (message, '完整终稿 END', 'confirmed', 'closed'))
                self.assertEqual(len(fake.cards), 1)
                self.assertEqual(len(fake.messages), 0)
            finally:
                await recovered.close()
                store.close()
