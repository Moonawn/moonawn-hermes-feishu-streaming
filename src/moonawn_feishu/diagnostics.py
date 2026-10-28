"""Small, bounded operational observations; never prompts, IDs or credentials."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import os
from pathlib import Path
import time

from . import __version__

logger = logging.getLogger(__name__)


class Diagnostics:
    def __init__(self, path: Path, facts, *, interval=5.0, clock=time.monotonic):
        self.path, self.facts, self.interval, self.clock = path, facts, interval, clock
        self.samples = {}
        self.consumers = 0
        self.max_loop_lag_ms = 0
        self.task = None
        self.stop = asyncio.Event()

    def record(self, stage, seconds):
        if stage not in {'stop_dispatch', 'stop_observed', 'final_delivery'}:
            raise ValueError('unknown diagnostic stage')
        ms = max(0, round(seconds * 1000))
        self.samples.setdefault(stage, deque(maxlen=128)).append(ms)
        logger.info('Streaming stage=%s elapsed_ms=%d', stage, ms)

    def snapshot(self, running=True):
        return {'schema': 1, 'plugin_version': __version__, 'updated_at': time.time(),
                'running': running, 'consumers_observed': self.consumers,
                'max_loop_lag_ms': self.max_loop_lag_ms,
                'timings_ms': {k: list(v) for k, v in self.samples.items()}, **self.facts()}

    def write(self, running=True):
        try:
            data = self.snapshot(running)
            temporary = self.path.with_suffix('.tmp')
            with temporary.open('w', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600)
                json.dump(data, stream, ensure_ascii=False)
                stream.write('\n')
            temporary.replace(self.path)
        except (OSError, ValueError, TypeError):
            # Observability must not interrupt delivery or reveal exception data.
            logger.warning('Streaming diagnostic snapshot unavailable')

    async def _run(self):
        while not self.stop.is_set():
            self.write()
            before = self.clock()
            try:
                await asyncio.wait_for(self.stop.wait(), self.interval)
            except asyncio.TimeoutError:
                lag = max(0, round((self.clock() - before - self.interval) * 1000))
                self.max_loop_lag_ms = max(self.max_loop_lag_ms, lag)
                if lag >= 500:
                    logger.warning('Streaming event_loop_lag_ms=%d', lag)

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def close(self):
        self.stop.set()
        if self.task:
            await self.task
        self.write(running=False)


def runtime_facts(adapter):
    """Report effective registered factory and observed runtime, not configuration text."""
    from gateway.platform_registry import platform_registry
    entry = platform_registry.get('feishu')
    factory = getattr(entry, 'adapter_factory', None)
    runner = getattr(getattr(adapter, '_message_handler', None), '__self__', None)
    streaming = getattr(getattr(runner, 'config', None), 'streaming', None)
    enabled = getattr(streaming, 'enabled', None)
    runtime = adapter._moonawn
    return {'adapter_active': type(adapter).__name__ == 'MoonawnFeishuAdapter',
            'factory_matches': factory is type(adapter),
            'streaming_global': enabled if isinstance(enabled, bool) else None,
            'active_phases': sum(not p.terminal for p in runtime.engine.phases.values()),
            'pending_delivery': sum(bool(p.terminal) and p.delivery not in {'confirmed', 'withdrawn'}
                                    for p in runtime.engine.phases.values())}
