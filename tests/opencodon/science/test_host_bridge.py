"""Host-bridge contracts: auth, cell gating, logging, spill, allowlist."""

import json
import socket

import pytest

from opencodon.science.host_bridge import HostBridge
from opencodon.science.store import ScienceStore


class FakeCompletions:
    def __init__(self, reply_fn):
        self._reply_fn = reply_fn

    def create(self, **kwargs):
        text = self._reply_fn(kwargs)

        class _Msg:
            content = text

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


class FakeLLMClient:
    def __init__(self, reply_fn):
        class _Chat:
            completions = FakeCompletions(reply_fn)

        self.chat = _Chat()


def call_bridge(bridge, method, params, token=None):
    payload = json.dumps(
        {
            "token": token if token is not None else bridge.endpoint["token"],
            "method": method,
            "params": params,
        }
    ).encode() + b"\n"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(30)
        s.connect(bridge.endpoint["socket"])
        s.sendall(payload)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
    finally:
        s.close()
    return json.loads(b"".join(chunks).decode())


@pytest.fixture
def store(db):
    db.create_session("s1", source="cli")
    return ScienceStore(db)


@pytest.fixture
def cell(store):
    return store.record_cell("s1", "code", "python", "k1")


@pytest.fixture
def bridge(tmp_path, store, monkeypatch):
    bridge = HostBridge(tmp_path / "ws", store, allowed_tools=["echo_tool"])
    monkeypatch.setattr(
        HostBridge,
        "_llm_client",
        lambda self, model: (
            FakeLLMClient(lambda kw: f"reply:{kw['messages'][-1]['content']}"),
            model or "fake-model",
        ),
    )
    bridge.start()
    yield bridge
    bridge.stop()


class TestAuthAndGating:
    def test_bad_token_is_refused(self, bridge, cell):
        with bridge.current_cell(cell):
            reply = call_bridge(bridge, "llm", {"prompt": "hi"}, token="wrong")
        assert "invalid token" in reply["error"]

    def test_out_of_cell_calls_are_refused(self, bridge):
        reply = call_bridge(bridge, "llm", {"prompt": "hi"})
        assert "while a cell is executing" in reply["error"]

    def test_unknown_method(self, bridge, cell):
        with bridge.current_cell(cell):
            reply = call_bridge(bridge, "teleport", {})
        assert "unknown host method" in reply["error"]


class TestLLM:
    def test_llm_returns_text_and_logs_call(self, bridge, store, cell):
        with bridge.current_cell(cell):
            reply = call_bridge(bridge, "llm", {"prompt": "classify this"})
        assert reply["data"] == "reply:classify this"
        [call] = store.host_calls_for_cell(cell)
        assert call["method"] == "llm"
        assert call["data_inline"] == "reply:classify this"
        assert call["error"] is None
        args = json.loads(call["args_json"])
        assert args["model"] == "fake-model"

    def test_llm_batch_returns_aligned_results(self, bridge, store, cell):
        with bridge.current_cell(cell):
            reply = call_bridge(
                bridge,
                "llm_batch",
                {"prompts": ["a", "b", "c"], "max_concurrency": 2},
            )
        assert [r["text"] for r in reply["data"]] == ["reply:a", "reply:b", "reply:c"]
        [call] = store.host_calls_for_cell(cell)
        assert call["method"] == "llm_batch"
        assert json.loads(call["args_json"])["count"] == 3

    def test_large_result_spills_to_snapshot(self, bridge, store, cell, monkeypatch):
        monkeypatch.setattr(
            HostBridge,
            "_llm_client",
            lambda self, model: (FakeLLMClient(lambda kw: "y" * 50_000), "fake"),
        )
        with bridge.current_cell(cell):
            reply = call_bridge(bridge, "llm", {"prompt": "big"})
        assert reply["data"] == "y" * 50_000
        [call] = store.host_calls_for_cell(cell)
        assert call["data_inline"] is None
        assert call["data_ref"]
        assert store.get_snapshot(call["data_ref"]) == "y" * 50_000

    def test_llm_failure_is_logged_with_error(self, bridge, store, cell, monkeypatch):
        def _boom(self, model):
            raise RuntimeError("no provider configured")

        monkeypatch.setattr(HostBridge, "_llm_client", _boom)
        with bridge.current_cell(cell):
            reply = call_bridge(bridge, "llm", {"prompt": "hi"})
        assert "no provider configured" in reply["error"]
        [call] = store.host_calls_for_cell(cell)
        assert "no provider configured" in call["error"]
        assert call["data_inline"] is None


class TestToolAllowlist:
    def test_tool_outside_allowlist_is_refused(self, bridge, store, cell):
        with bridge.current_cell(cell):
            reply = call_bridge(bridge, "tool", {"name": "terminal", "args": {}})
        assert "not in the science.host_tools allowlist" in reply["error"]

    def test_allowed_tool_dispatches_and_logs(self, bridge, store, cell, monkeypatch):
        from opencodon.tools import model_tools

        monkeypatch.setattr(
            model_tools,
            "handle_function_call",
            lambda name, args, **kw: json.dumps({"echoed": args}),
        )
        with bridge.current_cell(cell):
            reply = call_bridge(
                bridge, "tool", {"name": "echo_tool", "args": {"x": 1}}
            )
        assert reply["data"] == {"echoed": {"x": 1}}
        [call] = store.host_calls_for_cell(cell)
        assert call["method"] == "tool"
        assert json.loads(call["args_json"])["name"] == "echo_tool"


class TestCheapModel:
    """A model slug is only valid against the provider it belongs to.

    ``claude-haiku-4-5`` is a real Anthropic alias, and nothing else: on
    OpenRouter the slug is ``anthropic/claude-haiku-4-5`` and on OpenAI or a
    local endpoint it does not exist at all. Resolving it in the kernel — which
    cannot see which provider is configured — sends a broken slug to whichever
    one the user actually has.
    """

    @pytest.mark.requirement("SCI-P3-07")
    def test_configured_pin_wins(self, monkeypatch):
        from opencodon.science import host_bridge

        monkeypatch.setattr(
            host_bridge, "_configured_cheap_model", lambda: "anthropic/claude-haiku-4-5"
        )
        # Provider identity is irrelevant once the user has named a model.
        monkeypatch.setattr(host_bridge, "_provider_is_anthropic", lambda: True)
        assert host_bridge.cheap_model("gpt-5") == "anthropic/claude-haiku-4-5"

    @pytest.mark.requirement("SCI-P3-07")
    def test_anthropic_provider_gets_the_haiku_pin(self, monkeypatch):
        from opencodon.science import host_bridge

        monkeypatch.setattr(host_bridge, "_configured_cheap_model", lambda: None)
        monkeypatch.setattr(host_bridge, "_provider_is_anthropic", lambda: True)
        assert host_bridge.cheap_model("claude-opus-5") == "claude-haiku-4-5"

    @pytest.mark.requirement("SCI-P3-07")
    def test_other_providers_fall_back_to_their_own_default(self, monkeypatch):
        """Not cheaper, but a slug the configured provider will accept."""
        from opencodon.science import host_bridge

        monkeypatch.setattr(host_bridge, "_configured_cheap_model", lambda: None)
        monkeypatch.setattr(host_bridge, "_provider_is_anthropic", lambda: False)
        assert host_bridge.cheap_model("google/gemini-3-flash") == "google/gemini-3-flash"

    @pytest.mark.requirement("SCI-P3-07")
    def test_a_broken_config_read_does_not_break_the_call(self, monkeypatch):
        from opencodon.science import host_bridge

        def boom(task):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(
            "opencodon.core.auxiliary_client._get_auxiliary_task_config", boom
        )
        assert host_bridge._configured_cheap_model() is None

    @pytest.mark.requirement("SCI-P3-07")
    def test_models_reports_both_default_and_cheap(self, bridge, cell, monkeypatch):
        from opencodon.science import host_bridge

        monkeypatch.setattr(host_bridge, "_configured_cheap_model", lambda: "cheap-1")
        with bridge.current_cell(cell):
            reply = call_bridge(bridge, "models", {})
        assert reply["data"]["cheap"] == "cheap-1"
        assert reply["data"]["default"]


class TestReasoningModel:
    """The counterpart role to `cheap`: work that needs the strongest model.

    Both skills that use it called `host.reasoning_model()`, a donor-SDK name
    opencodon never implemented — an AttributeError on first use.
    """

    @pytest.mark.requirement("SCI-P3-07")
    def test_configured_pin_wins(self, monkeypatch):
        from opencodon.science import host_bridge

        monkeypatch.setattr(
            host_bridge, "_configured_model",
            lambda key: "anthropic/claude-opus-5" if key == "reasoning_model" else None,
        )
        assert host_bridge.reasoning_model("gpt-5") == "anthropic/claude-opus-5"

    @pytest.mark.requirement("SCI-P3-07")
    def test_unset_falls_back_to_the_provider_default(self, monkeypatch):
        """No built-in pin: "strongest model" is not a claim we can make for
        someone else's provider and budget."""
        from opencodon.science import host_bridge

        monkeypatch.setattr(host_bridge, "_configured_model", lambda key: None)
        assert host_bridge.reasoning_model("google/gemini-3-pro") == "google/gemini-3-pro"

    @pytest.mark.requirement("SCI-P3-07")
    def test_the_anthropic_pin_is_cheap_only(self, monkeypatch):
        """A provider check must not leak the haiku pin into the reasoning role."""
        from opencodon.science import host_bridge

        monkeypatch.setattr(host_bridge, "_configured_model", lambda key: None)
        monkeypatch.setattr(host_bridge, "_provider_is_anthropic", lambda: True)
        assert host_bridge.reasoning_model("claude-opus-5") == "claude-opus-5"

    @pytest.mark.requirement("SCI-P3-07")
    def test_models_reports_every_role(self, bridge, cell, monkeypatch):
        from opencodon.science import host_bridge

        monkeypatch.setattr(host_bridge, "_configured_model", lambda key: None)
        with bridge.current_cell(cell):
            reply = call_bridge(bridge, "models", {})
        assert set(reply["data"]) == {"default", "cheap", "reasoning"}
