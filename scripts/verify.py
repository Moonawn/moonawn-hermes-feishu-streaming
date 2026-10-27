"""Reproducible offline verification and measured synthetic performance report."""
from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
from pathlib import Path
import platform
import socket
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from moonawn_feishu.engine import Coalescer, Engine
from moonawn_feishu.fake_lark import FakeLark
from moonawn_feishu.hermes_bridge import inspect_hermes
from moonawn_feishu.model import Event, Identity, Route
from moonawn_feishu.store import Store


async def benchmark():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "synthetic.db")
        transport = FakeLark()
        engine = Engine(store, transport)
        identity = Identity(Route("fake", "fake", "fake", "fake"), "benchmark")
        engine.accept(Event(identity, "start", 0, "begin"))
        queue = Coalescer(lambda: engine.sync(identity.key), interval=0.25)
        tracemalloc.start()
        t0 = time.perf_counter()
        for seq in range(1, 10001):
            engine.accept(Event(identity, f"snapshot:{seq}", seq, "snapshot", f"模拟累计正文 {seq}"))
            queue.schedule()
        engine.accept(Event(identity, "final", 10001, "final", "完整最终正文 END"))
        queue.schedule(terminal=True)
        await queue.drain()
        elapsed = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert engine.confirmed(identity, "完整最终正文 END")
        report = {"kind": "single-event-loop synthetic burst; no real API/network/model",
                  "input_snapshots": 10000, "scheduled": queue.scheduled, "coalesced": queue.coalesced,
                  "writer_passes": queue.writes, "transport_calls": dict(transport.counts),
                  "elapsed_ms": round(elapsed * 1000, 2), "traced_peak_bytes": peak,
                  "final_exact": True, "network_latency_claim": False}
        store.close()
        return report


async def paced_simulation():
    """Unlike a burst, yield to the writer while the producer is still running."""
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "paced.db")
        transport = FakeLark()
        engine = Engine(store, transport)
        identity = Identity(Route("fake", "fake", "fake", "fake"), "paced")
        engine.accept(Event(identity, "begin", 0, "begin"))
        queue = Coalescer(lambda: engine.sync(identity.key), interval=0.02)
        for seq in range(1, 101):
            engine.accept(Event(identity, f"snapshot:{seq}", seq, "snapshot", "模拟持续生成 " + "文" * seq))
            queue.schedule()
            await asyncio.sleep(0.002)
        final = "模拟持续生成 " + "文" * 100 + " END"
        engine.accept(Event(identity, "final", 101, "final", final))
        queue.schedule(terminal=True)
        await queue.drain()
        report = {"kind": "paced local coroutine; 2 ms producer / 20 ms throttle / no real API",
                  "snapshots": 100, "scheduled": queue.scheduled, "writer_passes": queue.writes,
                  "coalesced": queue.coalesced, "transport_calls": dict(transport.counts),
                  "final_exact": engine.confirmed(identity, final)}
        assert report["final_exact"] and transport.counts["update_body"] > 0
        store.close()
        return report


def source_integrity():
    lock = json.loads((ROOT / "docs/source-lock.json").read_text())
    results = []
    for entry in lock["files"]:
        if "sha256" not in entry:
            continue
        target = ROOT / ".reference-cache" / entry["repo"].replace("/", "--") / entry["path"]
        passed = target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == entry["sha256"]
        results.append({"repo": entry["repo"], "path": entry["path"], "passed": passed})
    return {"passed": bool(results) and all(r["passed"] for r in results),
            "files_checked": len(results), "failures": [r for r in results if not r["passed"]]}


def feature_integrity():
    lock = json.loads((ROOT / "docs/feature-host-contract.json").read_text())
    failures = []
    for item in lock["files"]:
        target = ROOT / ".reference-cache/hermes-runtime-0.22.1" / item["path"]
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
            failures.append(item["path"])
    return {"passed": not failures, "files_checked": len(lock["files"]), "failures": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-reference-cache", action="store_true",
                        help="Also require the pinned research and Hermes source caches")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    # OS/runtime setup needs socketpair; outbound connection APIs are forbidden.
    with patch.object(socket.socket, "connect", side_effect=AssertionError("offline verification prohibits network")):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        bench = asyncio.run(benchmark())
        paced = asyncio.run(paced_simulation())
    source_root = ROOT / ".reference-cache/hermes-runtime-0.22.1"
    not_run = {"passed": None, "status": "not_run", "reason": "optional pinned source cache check"}
    compatibility = inspect_hermes(source_root) if args.with_reference_cache else dict(not_run)
    report = {"python": platform.python_version(), "tests_run": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
              "offline_passed": result.wasSuccessful(), "benchmark": bench, "paced_simulation": paced,
              "reference_integrity": source_integrity() if args.with_reference_cache else dict(not_run),
              "feature_contract_integrity": feature_integrity() if args.with_reference_cache else dict(not_run),
              "hermes_0221": compatibility}
    output = ROOT / "artifacts" / "verification.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    cache_passed = not args.with_reference_cache or (
        compatibility["static_surface_passed"] and report["reference_integrity"]["passed"]
        and report["feature_contract_integrity"]["passed"])
    return 0 if result.wasSuccessful() and cache_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
