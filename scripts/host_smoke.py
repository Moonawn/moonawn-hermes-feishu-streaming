"""Execute inside a network-disabled, config-free Hermes image only."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.metadata
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import zipfile
import sys
import faulthandler
import io

faulthandler.dump_traceback_later(60, repeat=True)

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist"


async def check():
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        release = json.loads((RELEASE_DIR / "manifest.json").read_text())
        archive = RELEASE_DIR / release["artifact"]
        archive_bytes = archive.read_bytes()
        assert hashlib.sha256(archive_bytes).hexdigest() == release["sha256"]
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as source:
            source.extractall(folder)
        wheel = ROOT / ".reference-cache/wheels/lark_oapi-1.7.5-py3-none-any.whl"
        # Both pinned wheels are pure Python. Zip import avoids extracting tens
        # of thousands of SDK modules to a temporary filesystem under load.
        for dependency in (ROOT / ".reference-cache/wheels").glob("*.whl"):
            local_wheel = folder / dependency.name
            local_wheel.write_bytes(dependency.read_bytes())
            sys.path.insert(0, str(local_wheel))
        from hermes_cli.plugins import PluginManager
        from hermes_cli.plugins_manifest import PluginManifest
        from gateway.platform_registry import platform_registry
        from plugins.platforms.feishu.adapter import FeishuAdapter
        from gateway.run_turn import GatewayTurnMixin
        from gateway.run_turn_runner import TurnRunner
        from gateway.slash_commands_session import GatewaySessionCommandsMixin
        from gateway.run_busy import GatewayBusySessionMixin
        from gateway import stream_consumer
        original_consumer = stream_consumer.GatewayStreamConsumer
        original_inner = GatewayTurnMixin._run_agent_inner
        original_wire = TurnRunner._wire_turn_agent_callbacks
        original_sessions = GatewaySessionCommandsMixin._handle_sessions_command
        original_busy = GatewayBusySessionMixin._dispatch_busy_slash_command
        manager = PluginManager()
        manifest = PluginManifest(name="moonawn-feishu", key="moonawn-feishu", version=archive.stem.removeprefix("moonawn-feishu-"),
                                  kind="platform", source="user", path=str(folder / "moonawn-feishu"))
        manager._load_plugin(manifest)
        loaded = manager._plugins["moonawn-feishu"]
        assert loaded.enabled, loaded.error
        entry = platform_registry.get("feishu")
        assert entry is not None and issubclass(entry.adapter_factory, FeishuAdapter)
        assert entry.adapter_factory is not FeishuAdapter
        assert stream_consumer.GatewayStreamConsumer._moonawn_router
        assert entry.adapter_factory.send_document is FeishuAdapter.send_document
        assert entry.adapter_factory.send_exec_approval is FeishuAdapter.send_exec_approval
        assert entry.adapter_factory._on_card_action_trigger is FeishuAdapter._on_card_action_trigger
        assert entry.check_fn is not None
        # Keep native media upload and dispatch, but supply the admitted topic
        # reply anchor to the inherited sender instead of a thread_id create.
        media_adapter = object.__new__(entry.adapter_factory)
        media_calls = []
        async def capture_media(**kwargs):
            media_calls.append(kwargs)
            return SimpleNamespace(code=0)
        media_adapter._feishu_send_with_retry = capture_media
        from gateway.run import GatewayRunner
        probe = object.__new__(GatewayRunner)
        assert probe._command_handler_table(("sessions",))["sessions"].__func__ is GatewaySessionCommandsMixin._handle_sessions_command
        from gateway.platforms.base import MessageEvent
        from gateway.config import Platform
        from gateway.session import SessionSource
        panel_calls = []
        @asynccontextmanager
        async def profile_scope(source):
            panel_calls.append("profile")
            yield
        async def show_panel(event): panel_calls.append("panel")
        busy_probe = SimpleNamespace(_delivery_adapter_for=lambda source: SimpleNamespace(_moonawn_controls=True),
            _session_key_for_source=lambda source: "busy-key", _check_slash_access=lambda source, cmd: "denied",
            _async_profile_scope_for_source=profile_scope, _handle_sessions_command=show_panel)
        busy_probe._dispatch_busy_slash_command = lambda *args: GatewayBusySessionMixin._dispatch_busy_slash_command(busy_probe, *args)
        busy_source = SessionSource(Platform.FEISHU, "synthetic-chat", user_id="synthetic-owner")
        busy_event = MessageEvent(text="/sessions", source=busy_source, allow_gateway_control=True)
        assert await GatewayRunner._hm_busy_slash_or_photo(busy_probe, busy_event, busy_source, "busy-key") == (True, "denied")
        assert not panel_calls
        busy_probe._check_slash_access = lambda source, cmd: None
        assert await GatewayRunner._hm_busy_slash_or_photo(busy_probe, busy_event, busy_source, "busy-key") == (True, None)
        assert panel_calls == ["profile", "panel"]
        mod = sys.modules[loaded.module.__package__ + ".moonawn_feishu.feishu_transport"]
        from lark_oapi.core.model import BaseResponse, RawResponse
        seen = []
        class Client:
            def request(self, request):
                seen.append(request)
                response, raw = BaseResponse(), RawResponse()
                raw.status_code, raw.headers, raw.content = 200, {}, b'{"code":0,"data":{}}'
                response.raw = raw
                return response
        async def executor(fn, *args): return fn(*args)
        wire = mod.LarkSDKWire(Client(), executor)
        await wire.request("GET", "/open-apis/im/v1/messages/test?card_msg_content_type=user_card_content")
        await wire.request("PUT", "/open-apis/cardkit/v1/cards/test", {"sequence": 2})
        assert seen[0].uri.endswith("card_msg_content_type=user_card_content")
        assert seen[1].body == {"sequence": 2}
        # Exercise the actual pinned host's callback wiring, not a duplicate
        # implementation. All ownership/services are isolated stand-ins.
        import importlib
        package = loaded.module.__package__ + ".moonawn_feishu."
        plugin = importlib.import_module(package + "plugin")
        runtime_mod = importlib.import_module(package + "runtime")
        engine_mod = importlib.import_module(package + "engine")
        fake_mod = importlib.import_module(package + "fake_lark")
        store_mod = importlib.import_module(package + "store")
        model_mod = importlib.import_module(package + "model")
        from gateway.session import SessionSource, build_session_key
        from gateway.config import Platform
        from dataclasses import replace
        source = SessionSource(Platform.FEISHU, "synthetic-chat", user_id="synthetic-owner", thread_id="topic-a")
        assert build_session_key(source) != build_session_key(replace(source, thread_id="topic-b"))
        own_store = store_mod.Store(folder / "own.db")
        runtime = runtime_mod.Runtime(engine_mod.Engine(own_store, fake_mod.FakeLark()))
        adapter = SimpleNamespace(_moonawn=runtime)
        scope = runtime_mod.Scope(model_mod.Route("fake", "fake", "fake", source.chat_id), "synthetic", "inbound")
        dispatch_token = plugin._dispatch.set(plugin.Dispatch(adapter, scope, source=source))
        media_scope = runtime_mod.Scope(model_mod.Route("fake", "fake", "fake", "chat", "topic", "anchor"), "s", "i")
        media_token = plugin._dispatch.set(plugin.Dispatch(media_adapter, media_scope))
        try:
            await media_adapter._send_uploaded_key(chat_id="chat", reply_to=None,
                metadata={"thread_id": "topic"}, caption=None, key_msg_type="file",
                key_payload={"file_key": "test-upload"}, media_tag={"tag": "media", "file_key": "test-upload"})
            assert media_calls[-1]["reply_to"] == "anchor"
            assert media_calls[-1]["msg_type"] == "file"
        finally:
            plugin._dispatch.reset(media_token)
        noop = lambda *a, **k: None
        ctx = SimpleNamespace(user_config={}, source=source, progress_callback=noop,
            _voice_ack_guild=[None], _native_slack_task_cards=False, _hooks_ref=SimpleNamespace(loaded_hooks=[]),
            _step_callback_sync=noop, _status_callback_sync=noop, _event_callback_sync=noop,
            voice_turn=None, session_key="synthetic", _status_adapter=None, mute_notification_reply=False,
            _thinking_enabled=True, agent_holder=[None], tools_holder=[None], process_task_id="", process_baseline=set())
        runner = SimpleNamespace(_service_tier=None, _consume_pending_turn_sidecar_notes=lambda key: [])
        bridge = SimpleNamespace(_ctx=ctx, _runner=runner, _approval_owner=None, _notice_callback_sync=noop,
            _merge_turn_request_overrides=noop, _make_bg_review_callbacks=lambda: (None, noop),
            _clarify_callback_sync=noop, _attach_session_title_callback=noop)
        agent = SimpleNamespace(model="demo", provider="demo", session_prompt_tokens=10000,
                                session_completion_tokens=500, session_api_calls=8)
        try:
            consumer = plugin.ManagedConsumer(adapter, source.chat_id)
            # A copied ContextVar must not make a different native session an
            # owner, even if it is wired before the parent agent.
            delegated = SimpleNamespace(model="delegated", provider="demo")
            ctx.session_key = "another-native-session"
            with patch("gateway.session_surface.arm_surface_turn"):
                TurnRunner._wire_turn_agent_callbacks(bridge, delegated, {}, None, None, None, False)
            assert consumer.telemetry.agent is None
            assert delegated.tool_start_callback is None
            ctx.session_key = "synthetic"
            with patch("gateway.session_surface.arm_surface_turn"):
                TurnRunner._wire_turn_agent_callbacks(bridge, agent, {}, None, None, None, False)
            assert ctx.agent_holder[0] is agent
            agent.step_callback(2, [])
            agent.tool_start_callback("call-a", "read_file", {})
            agent.tool_complete_callback("call-a", "read_file", {}, '{"success":true}')
            agent.session_prompt_tokens += 120
            agent.session_completion_tokens += 40
            agent.session_api_calls += 1
            agent._last_turn_usage = {"reported": True}
            snapshot = consumer.telemetry.snapshot()
            assert snapshot["usage"]["input"] == 120 and snapshot["usage"]["output"] == 40
            assert snapshot["iteration"] == 2 and snapshot["tools_done"] == 1
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
            controls_mod = importlib.import_module(package + "controls")
            adapter._app_id, adapter._moonawn_profile = "fake-app", "fake-profile"
            adapter._is_interactive_operator_authorized = lambda user: user == "synthetic-owner"
            own_store.save_operation("control:synthetic", {"panel": "synthetic"})
            own_store.save_operation("panel:synthetic", {"profile": "fake-profile", "source": source.to_dict(), "message_id": "synthetic-card"})
            callback = P2CardActionTrigger({"header": {"app_id": "fake-app", "event_id": "synthetic-event"},
                "event": {"action": {"value": {"moonawn_action": "synthetic"}},
                          "operator": {"open_id": "synthetic-owner"},
                          "context": {"open_chat_id": "synthetic-chat", "open_message_id": "synthetic-card"}}})
            controls = controls_mod.ControlService(adapter, own_store, None)
            assert controls.validate(callback)[0] == "synthetic"
            consumer.restore_callbacks()
            await runtime.close()
        finally:
            plugin._dispatch.reset(dispatch_token)
            own_store.close()
        manager.unload("moonawn-feishu")
        assert stream_consumer.GatewayStreamConsumer is original_consumer
        assert GatewayTurnMixin._run_agent_inner is original_inner
        assert TurnRunner._wire_turn_agent_callbacks is original_wire
        assert GatewaySessionCommandsMixin._handle_sessions_command is original_sessions
        assert GatewayBusySessionMixin._dispatch_busy_slash_command is original_busy
        return {"passed": True, "hermes": "0.22.1", "python": sys.version.split()[0],
                "sdk": importlib.metadata.version("lark-oapi"), "sdk_wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "release_sha256": hashlib.sha256(archive_bytes).hexdigest(),
                "checks": ["real directory plugin manager load/unload", "native Feishu adapter inheritance",
                           "native media and approval methods preserved", "current lifecycle signatures",
                           "real SDK BaseRequest/BaseResponse/RawResponse; synthetic payload and executor",
                           "actual TurnRunner callback wiring; per-turn usage delta and tool lifecycle",
                           "native command table resolves session wrapper; real SDK callback owner validation",
                           "actual native busy slash admission preserves ACL before scoped panel dispatch",
                           "actual native Feishu thread session keys are distinct", "all delivery and feature hooks restored on unload"],
                "network": "none", "gateway_started": False, "live_integration_verified": False}


with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
    print(json.dumps(asyncio.run(check()), ensure_ascii=False))
