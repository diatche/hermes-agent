"""Tests for topic-aware gateway progress updates."""

import asyncio
import importlib
import sys
import time
import types
from types import SimpleNamespace

import pytest

import gateway.platforms.base as base_platform
from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


class ProgressCaptureAdapter(BasePlatformAdapter):
    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.sent = []
        self.edits = []
        self.deletes = []
        self.typing = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="progress-1")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
            }
        )
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id) -> bool:
        self.deletes.append({"chat_id": chat_id, "message_id": message_id})
        return True

    async def send_typing(self, chat_id, metadata=None) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": metadata})

    async def stop_typing(self, chat_id) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": {"stopped": True}})

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class PinCaptureBot:
    def __init__(self):
        self.pins = []
        self.unpins = []
        self.pinned_messages = []

    async def pin_chat_message(self, **kwargs):
        self.pins.append(kwargs)
        return True

    async def unpin_chat_message(self, **kwargs):
        self.unpins.append(kwargs)
        self.pinned_messages = [
            message
            for message in self.pinned_messages
            if message.message_id != kwargs["message_id"]
        ]
        return True

    async def get_chat(self, chat_id):
        return SimpleNamespace(
            pinned_message=self.pinned_messages[-1] if self.pinned_messages else None
        )


class PinningProgressAdapter(ProgressCaptureAdapter):
    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(platform=platform)
        self._bot = PinCaptureBot()
        self._next_message_id = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self._next_message_id += 1
        result = await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
        result.message_id = f"progress-{self._next_message_id}"
        return result


class BlockingDeleteProgressAdapter(ProgressCaptureAdapter):
    """Hold checklist deletion open to exercise progress-task shutdown."""

    latest = None

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(platform=platform)
        self.delete_started = asyncio.Event()
        self.allow_delete = asyncio.Event()
        type(self).latest = self

    async def delete_message(self, chat_id, message_id) -> bool:
        self.delete_started.set()
        await self.allow_delete.wait()
        return await super().delete_message(chat_id, message_id)


class HangingDeleteProgressAdapter(ProgressCaptureAdapter):
    """Never finish deletion unless released by test cleanup."""

    latest = None

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(platform=platform)
        self.delete_started = asyncio.Event()
        self.allow_delete = asyncio.Event()
        self.delete_cancelled = asyncio.Event()
        type(self).latest = self

    async def delete_message(self, chat_id, message_id) -> bool:
        self.delete_started.set()
        try:
            await self.allow_delete.wait()
        except asyncio.CancelledError:
            self.delete_cancelled.set()
            raise
        return await super().delete_message(chat_id, message_id)


class FailedDeleteProgressAdapter(ProgressCaptureAdapter):
    async def delete_message(self, chat_id, message_id) -> bool:
        return False


class RaisingDeleteProgressAdapter(ProgressCaptureAdapter):
    async def delete_message(self, chat_id, message_id) -> bool:
        raise RuntimeError("delete failed")


class SmallLimitProgressAdapter(ProgressCaptureAdapter):
    """Adapter with a tiny platform limit to exercise progress rollover."""

    MAX_MESSAGE_LENGTH = 180

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(platform=platform)
        self._next_id = 0
        self.oversized_edits = []
        self.oversized_sends = []

    def _mint_id(self):
        self._next_id += 1
        return f"progress-{self._next_id}"

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        if len(content) > self.MAX_MESSAGE_LENGTH:
            self.oversized_sends.append(content)
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id=self._mint_id())

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        if len(content) > self.MAX_MESSAGE_LENGTH:
            self.oversized_edits.append(content)
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
            }
        )
        return SendResult(success=True, message_id=message_id)


class MetadataEditProgressCaptureAdapter(ProgressCaptureAdapter):
    async def edit_message(
        self, chat_id, message_id, content, *, finalize: bool = False, metadata=None
    ) -> SendResult:
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id=message_id)


class NonEditingProgressCaptureAdapter(ProgressCaptureAdapter):
    SUPPORTS_MESSAGE_EDITING = False

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        raise AssertionError("non-editable adapters should not receive edit_message calls")


class FakeAgent:
    def __init__(self, **kwargs):
        # Capture anything passed via kwargs (older code path) but don't
        # freeze it — production now assigns tool_progress_callback after
        # construction (see gateway/run.py around the agent-cache hit),
        # so we must read it at call time, not at init.
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.35)
            cb("tool.started", "browser_navigate", "https://example.com", {})
            time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class ThinkingAgent:
    """Agent that emits _thinking scratch text (no tool calls).

    Used to prove the progress callback relays _thinking bubbles when
    thinking_progress is enabled but tool_progress is off.
    """

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("_thinking", "weighing the options here")
            time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class TodoChecklistAgent:
    """Emits authoritative todo results plus a delegated-task batch."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tool_complete_callback = kwargs.get("tool_complete_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        complete = self.tool_complete_callback
        assert cb is not None
        assert complete is not None
        cb(
            "tool.started",
            "todo",
            "planning 3 task(s)",
            {
                "todos": [
                    {"id": "inspect", "content": "Inspect configuration", "status": "completed"},
                    {"id": "patch", "content": "Implement checklist", "status": "in_progress"},
                    {"id": "verify", "content": "Run tests", "status": "pending"},
                ],
                "merge": False,
            },
        )
        complete(
            "todo-1",
            "todo",
            {"merge": False},
            '{"todos": ['
            '{"id":"inspect","content":"Inspect configuration","status":"completed"},'
            '{"id":"patch","content":"Implement checklist","status":"in_progress"},'
            '{"id":"verify","content":"Run tests","status":"pending"}'
            ']}'
        )
        time.sleep(0.35)
        cb(
            "tool.started",
            "todo",
            "updating 2 task(s)",
            {
                "todos": [
                    {"id": "patch", "content": "Implement checklist", "status": "completed"},
                    {"id": "verify", "content": "Run tests", "status": "in_progress"},
                ],
                "merge": True,
            },
        )
        complete(
            "todo-2",
            "todo",
            {"todos": [{"id": "patch", "status": "completed"}], "merge": True},
            '{"todos": ['
            '{"id":"inspect","content":"Inspect configuration","status":"completed"},'
            '{"id":"patch","content":"Implement checklist","status":"completed"},'
            '{"id":"verify","content":"Run tests","status":"in_progress"}'
            ']}'
        )
        time.sleep(0.35)
        complete(
            "todo-2-retry",
            "todo",
            {"todos": [{"id": "patch", "status": "completed"}], "merge": True},
            '{"todos": ['
            '{"id":"inspect","content":"Inspect configuration","status":"completed"},'
            '{"id":"patch","content":"Implement checklist","status":"completed"},'
            '{"id":"verify","content":"Run tests","status":"in_progress"}'
            ']}'
        )
        time.sleep(0.35)
        cb(
            "tool.started",
            "delegate_task",
            "delegating 2 tasks",
            {
                "tasks": [
                    {"goal": "Review gateway integration"},
                    {"goal": "Check Telegram rendering"},
                ]
            },
        )
        time.sleep(0.35)
        cb("tool.started", "terminal", "should stay hidden", {"command": "echo hidden"})
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class InitiallyEmptyTodoAgent:
    """Emits an authoritative empty todo list before any checklist exists."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tool_complete_callback = kwargs.get("tool_complete_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        assert self.tool_complete_callback is not None
        self.tool_complete_callback("todo-empty", "todo", {"merge": False}, '{"todos": []}')
        return {"final_response": "done", "messages": [], "api_calls": 1}


class TodoClearingAgent(InitiallyEmptyTodoAgent):
    """Displays one task, then replaces the authoritative state with empty."""

    def run_conversation(self, message, conversation_history=None, task_id=None):
        complete = self.tool_complete_callback
        assert complete is not None
        complete(
            "todo-one",
            "todo",
            {"merge": False},
            '{"todos":[{"id":"one","content":"Only task","status":"in_progress"}]}',
        )
        time.sleep(0.35)
        complete("todo-empty", "todo", {"merge": False}, '{"todos": []}')
        time.sleep(0.35)
        return {"final_response": "done", "messages": [], "api_calls": 1}


class ImmediateTodoClearingAgent(TodoClearingAgent):
    """Queues create and clear back-to-back immediately before returning."""

    def run_conversation(self, message, conversation_history=None, task_id=None):
        complete = self.tool_complete_callback
        assert complete is not None
        complete(
            "todo-one",
            "todo",
            {"merge": False},
            '{"todos":[{"id":"one","content":"Only task","status":"in_progress"}]}',
        )
        complete("todo-empty", "todo", {"merge": False}, '{"todos": []}')
        return {"final_response": "done", "messages": [], "api_calls": 1}


class TodoClearThenRestoreAgent(TodoClearingAgent):
    """Restores state after a failed clear so the old bubble should be edited."""

    def run_conversation(self, message, conversation_history=None, task_id=None):
        complete = self.tool_complete_callback
        assert complete is not None
        complete(
            "todo-one",
            "todo",
            {"merge": False},
            '{"todos":[{"id":"one","content":"First task","status":"in_progress"}]}',
        )
        time.sleep(0.35)
        complete("todo-empty", "todo", {"merge": False}, '{"todos": []}')
        time.sleep(0.35)
        complete(
            "todo-restored",
            "todo",
            {"merge": False},
            '{"todos":[{"id":"two","content":"Restored task","status":"pending"}]}',
        )
        time.sleep(0.35)
        return {"final_response": "done", "messages": [], "api_calls": 1}


class TodoClearingWithDelegationAgent(TodoClearingAgent):
    """Clears ordinary tasks while retaining visible delegated work."""

    def run_conversation(self, message, conversation_history=None, task_id=None):
        complete = self.tool_complete_callback
        progress = self.tool_progress_callback
        assert complete is not None
        assert progress is not None
        complete(
            "todo-one",
            "todo",
            {"merge": False},
            '{"todos":[{"id":"one","content":"Only task","status":"in_progress"}]}',
        )
        time.sleep(0.35)
        progress(
            "tool.started",
            "delegate_task",
            "delegating 1 task",
            {"goal": "Review the implementation"},
        )
        time.sleep(0.35)
        complete("todo-empty", "todo", {"merge": False}, '{"todos": []}')
        time.sleep(0.35)
        return {"final_response": "done", "messages": [], "api_calls": 1}


class LongPreviewAgent:
    """Agent that emits a tool call with a very long preview string."""
    LONG_CMD = "cd /home/teknium/.hermes/hermes-agent/.worktrees/hermes-d8860339 && source .venv/bin/activate && python -m pytest tests/gateway/test_run_progress_topics.py -n0 -q"

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        self.tool_progress_callback("tool.started", "terminal", self.LONG_CMD, {})
        time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class DelayedProgressAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        self.tool_progress_callback("tool.started", "terminal", "first command", {})
        time.sleep(0.45)
        self.tool_progress_callback("tool.started", "terminal", "second command", {})
        time.sleep(0.1)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class ManyProgressLinesAgent:
    """Emits enough tool-progress lines to exceed a single platform bubble."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        assert cb is not None
        cb("tool.started", "terminal", "first-short", {})
        # Let the progress task create the first editable bubble, then enqueue
        # the rest quickly.  The cancellation drain must roll them into fresh
        # editable bubbles instead of trying to edit the first one past limit.
        time.sleep(0.35)
        for idx in range(1, 8):
            cb("tool.started", "terminal", f"overflow-line-{idx}-" + "x" * 45, {})
        time.sleep(0.1)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class DelayedInterimAgent:
    def __init__(self, **kwargs):
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        self.interim_assistant_callback("first interim")
        time.sleep(0.45)
        self.interim_assistant_callback("second interim")
        time.sleep(0.1)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._pinned_todo_messages = {}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.session_store = SimpleNamespace(_entries={}, _save=lambda: None)
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


@pytest.mark.asyncio
async def test_run_agent_progress_stays_in_originating_topic(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 - register terminal emoji for this fake-agent test

    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:telegram:group:-1001:17585",
    )

    assert result["final_response"] == "done"
    assert adapter.sent == [
        {
            "chat_id": "-1001",
            "content": '💻 Running pwd',
            "reply_to": None,
            "metadata": {"thread_id": "17585"},
        }
    ]
    assert adapter.edits
    assert all(call["metadata"] == {"thread_id": "17585"} for call in adapter.typing)


@pytest.mark.asyncio
async def test_run_agent_progress_edits_keep_originating_topic_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = MetadataEditProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-progress-edit-topic",
        session_key="agent:main:telegram:group:-1001:17585",
    )

    assert result["final_response"] == "done"
    assert adapter.edits
    assert all(call["metadata"] == {"thread_id": "17585"} for call in adapter.edits)


@pytest.mark.asyncio
async def test_run_agent_progress_does_not_use_event_message_id_for_telegram_dm(monkeypatch, tmp_path):
    """Telegram DM progress must not reuse event message id as thread metadata."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.TELEGRAM)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-2",
        session_key="agent:main:telegram:dm:12345",
        event_message_id="777",
    )

    assert result["final_response"] == "done"
    assert adapter.sent
    assert adapter.sent[0]["metadata"] is None
    assert all(call["metadata"] is None for call in adapter.typing)


@pytest.mark.asyncio
async def test_run_agent_progress_uses_event_message_id_for_slack_dm(monkeypatch, tmp_path):
    """Slack DM progress should keep event ts fallback threading."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")
    # Since PR #8006, Slack's built-in display tier sets tool_progress="off"
    # by default. Override via config so this test still exercises the
    # progress-callback path the Slack DM event_message_id threading depends on.
    import yaml
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"display": {"platforms": {"slack": {"tool_progress": "all"}}}}),
        encoding="utf-8",
    )

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.SLACK)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="D123",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-3",
        session_key="agent:main:slack:dm:D123",
        event_message_id="1234567890.000001",
    )

    assert result["final_response"] == "done"
    assert adapter.sent
    assert adapter.sent[0]["metadata"] == {"thread_id": "1234567890.000001"}
    assert all(call["metadata"] == {"thread_id": "1234567890.000001"} for call in adapter.typing)


@pytest.mark.asyncio
async def test_run_agent_feishu_progress_replies_inside_existing_thread(monkeypatch, tmp_path):
    """Feishu needs reply_to plus reply_in_thread metadata for topic-scoped progress."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.FEISHU)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_type="group",
        thread_id="topic_17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-feishu-progress",
        session_key="agent:main:feishu:group:oc_chat:topic_17585",
        event_message_id="om_triggering_user_message",
    )

    assert result["final_response"] == "done"
    assert adapter.sent
    assert adapter.sent[0]["reply_to"] == "om_triggering_user_message"
    assert adapter.sent[0]["metadata"] == {"thread_id": "topic_17585"}
    assert adapter.edits
    assert adapter.edits[0]["message_id"] == "progress-1"


# ---------------------------------------------------------------------------
# Preview truncation tests (all/new mode respects tool_preview_length)
# ---------------------------------------------------------------------------


def _extract_progress_preview(content: str) -> str | None:
    """Extract the argument-preview portion from a tool-progress message.

    Handles both render styles:
    - Legacy / custom tools:  ``🔧 tool_name: "<preview>"`` (quoted)
    - Friendly built-in verb: ``💻 Running <preview>`` (verb prefix, no quotes)
    """
    import re

    # Legacy quoted form takes precedence when present.
    match = re.search(r'"(.+)"', content)
    if match:
        return match.group(1)
    # Friendly form: "<emoji> <verb> <preview>". The terminal verb is "Running".
    marker = " Running "
    idx = content.find(marker)
    if idx != -1:
        return content[idx + len(marker):].strip()
    return None


def _run_long_preview_helper(monkeypatch, tmp_path, preview_length=0):
    """Shared setup for long-preview truncation tests.

    Returns (adapter, result) after running the agent with LongPreviewAgent.
    ``preview_length`` controls display.tool_preview_length in the config file
    that _run_agent reads — so the gateway picks it up the same way production does.
    """
    import asyncio
    import yaml

    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = LongPreviewAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    # Write config.yaml so _run_agent picks up tool_preview_length
    config = {"display": {"tool_preview_length": preview_length}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = asyncio.get_event_loop().run_until_complete(
        runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=source,
            session_id="sess-trunc",
            session_key="agent:main:telegram:dm:12345",
        )
    )
    return adapter, result


def test_all_mode_default_truncation_40_chars(monkeypatch, tmp_path):
    """When tool_preview_length is 0 (default), all/new mode truncates to 40 chars."""
    adapter, result = _run_long_preview_helper(monkeypatch, tmp_path, preview_length=0)
    assert result["final_response"] == "done"
    assert adapter.sent
    content = adapter.sent[0]["content"]
    # The long command should be truncated — the preview portion <= 40 chars.
    assert "..." in content
    preview_text = _extract_progress_preview(content)
    assert preview_text is not None, f"No preview found in: {content}"
    assert len(preview_text) <= 40, f"Preview too long ({len(preview_text)}): {preview_text}"


def test_all_mode_respects_custom_preview_length(monkeypatch, tmp_path):
    """When tool_preview_length is explicitly set (e.g. 120), all/new mode uses that."""
    adapter, result = _run_long_preview_helper(monkeypatch, tmp_path, preview_length=120)
    assert result["final_response"] == "done"
    assert adapter.sent
    content = adapter.sent[0]["content"]
    # With 120-char cap, the command (165 chars) should still be truncated but longer.
    preview_text = _extract_progress_preview(content)
    assert preview_text is not None, f"No preview found in: {content}"
    # Should be longer than the 40-char default
    assert len(preview_text) > 40, f"Preview suspiciously short ({len(preview_text)}): {preview_text}"
    # But still capped at 120
    assert len(preview_text) <= 120, f"Preview too long ({len(preview_text)}): {preview_text}"


def test_all_mode_no_truncation_when_preview_fits(monkeypatch, tmp_path):
    """Short previews (under the cap) are not truncated."""
    # Set a generous cap — the LongPreviewAgent's command is ~165 chars
    adapter, result = _run_long_preview_helper(monkeypatch, tmp_path, preview_length=200)
    assert result["final_response"] == "done"
    assert adapter.sent
    content = adapter.sent[0]["content"]
    # With a 200-char cap, the 165-char command should NOT be truncated
    assert "..." not in content, f"Preview was truncated when it shouldn't be: {content}"


class CommentaryAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.interim_assistant_callback:
            self.interim_assistant_callback("I'll inspect the repo first.", already_streamed=False)
        time.sleep(0.1)
        if self.stream_delta_callback:
            self.stream_delta_callback("done")
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class PreviewedResponseAgent:
    def __init__(self, **kwargs):
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.interim_assistant_callback:
            self.interim_assistant_callback("You're welcome.", already_streamed=False)
        return {
            "final_response": "You're welcome.",
            "response_previewed": True,
            "messages": [],
            "api_calls": 1,
        }


class PreviewedSplitAfterCommentaryAgent:
    def __init__(self, **kwargs):
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.session_id = kwargs.get("session_id")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.interim_assistant_callback:
            self.interim_assistant_callback("I'll inspect the repo first.", already_streamed=False)
        self.session_id = f"{self.session_id}-child"
        return {
            "final_response": "Final answer after compression.",
            "response_previewed": True,
            "messages": [],
            "api_calls": 1,
        }


class StreamingRefineAgent:
    def __init__(self, **kwargs):
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.stream_delta_callback:
            self.stream_delta_callback("Continuing to refine:")
        time.sleep(0.1)
        if self.stream_delta_callback:
            self.stream_delta_callback(" Final answer.")
        return {
            "final_response": "Continuing to refine: Final answer.",
            "response_previewed": True,
            "messages": [],
            "api_calls": 1,
        }


class QueuedCommentaryAgent:
    calls = 0

    def __init__(self, **kwargs):
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).calls += 1
        if type(self).calls == 1 and self.interim_assistant_callback:
            self.interim_assistant_callback("I'll inspect the repo first.", already_streamed=False)
        return {
            "final_response": f"final response {type(self).calls}",
            "messages": [],
            "api_calls": 1,
        }


class QueuedSilenceAgent:
    """First turn is intentionally silent; queued follow-up still runs."""

    calls = 0

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).calls += 1
        return {
            "final_response": "NO_REPLY" if type(self).calls == 1 else "follow-up processed",
            "messages": [],
            "api_calls": 1,
        }


class QueuedFailedEmptyAgent:
    """First turn fails empty; its normalized error must send before follow-up."""

    calls = 0

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).calls += 1
        if type(self).calls == 1:
            return {
                "final_response": "",
                "messages": [],
                "api_calls": 1,
                "failed": True,
                "error": "provider exploded",
            }
        return {
            "final_response": "follow-up processed",
            "messages": [],
            "api_calls": 1,
        }


class BackgroundReviewAgent:
    def __init__(self, **kwargs):
        self.background_review_callback = kwargs.get("background_review_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.background_review_callback:
            self.background_review_callback("💾 Skill 'prospect-scanner' created.")
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class VerboseAgent:
    """Agent that emits a tool call with args whose JSON exceeds 200 chars."""
    LONG_CODE = "x" * 300

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        self.tool_progress_callback(
            "tool.started", "execute_code", None,
            {"code": self.LONG_CODE},
        )
        time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


async def _run_with_agent(
    monkeypatch,
    tmp_path,
    agent_cls,
    *,
    session_id,
    pending_text=None,
    config_data=None,
    platform=Platform.TELEGRAM,
    chat_id="-1001",
    chat_type="group",
    thread_id="17585",
    adapter_cls=ProgressCaptureAdapter,
    runner=None,
    adapter=None,
):
    if config_data:
        import yaml

        (tmp_path / "config.yaml").write_text(yaml.dump(config_data), encoding="utf-8")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = adapter or adapter_cls(platform=platform)
    runner = runner or _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    if config_data and "streaming" in config_data:
        runner.config.streaming = StreamingConfig.from_dict(config_data["streaming"])
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    source = SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        thread_id=thread_id,
    )
    session_key = f"agent:main:{platform.value}:{chat_type}:{chat_id}"
    if thread_id:
        session_key = f"{session_key}:{thread_id}"
    if pending_text is not None:
        adapter._pending_messages[session_key] = MessageEvent(
            text=pending_text,
            message_type=MessageType.TEXT,
            source=source,
            message_id="queued-1",
        )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id=session_id,
        session_key=session_key,
    )
    return adapter, result


@pytest.mark.asyncio
async def test_run_agent_rolls_progress_bubble_before_platform_limit(monkeypatch, tmp_path):
    """Tool progress should start a second editable bubble before Telegram's limit.

    Regression: once the first progress bubble grew past the platform limit,
    the gateway kept trying to edit that same oversized full transcript.  The
    Telegram adapter then split-and-sent a fresh continuation on every update,
    causing a noisy trail of one-line messages instead of a new editable bubble.
    """
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        ManyProgressLinesAgent,
        session_id="sess-progress-overflow-rollover",
        config_data={
            "display": {
                "tool_progress": "all",
                "interim_assistant_messages": False,
                "tool_preview_length": 60,
            }
        },
        adapter_cls=SmallLimitProgressAdapter,
    )

    assert result["final_response"] == "done"
    assert isinstance(adapter, SmallLimitProgressAdapter)
    assert len(adapter.sent) >= 2, "expected a fresh progress bubble after the first filled"
    assert adapter.oversized_sends == []
    assert adapter.oversized_edits == []
    all_bubbles = [call["content"] for call in adapter.sent + adapter.edits]
    assert all(len(text) <= adapter.MAX_MESSAGE_LENGTH for text in all_bubbles)


@pytest.mark.asyncio
async def test_run_agent_surfaces_real_interim_commentary(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary",
        config_data={"display": {"interim_assistant_messages": True}},
    )

    assert result.get("already_sent") is not True
    assert any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_surfaces_interim_commentary_by_default(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-default-on",
    )

    assert any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_suppresses_interim_commentary_when_disabled(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-disabled",
        config_data={"display": {"interim_assistant_messages": False}},
    )

    assert result.get("already_sent") is not True
    assert not any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_tool_progress_does_not_control_interim_commentary(monkeypatch, tmp_path):
    """tool_progress=all with interim_assistant_messages=false should not surface commentary."""
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-tool-progress",
        config_data={"display": {"tool_progress": "all", "interim_assistant_messages": False}},
    )

    assert result.get("already_sent") is not True
    assert not any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_streaming_does_not_enable_completed_interim_commentary(
    monkeypatch, tmp_path
):
    """Streaming alone with interim_assistant_messages=false should not surface commentary."""
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-streaming",
        config_data={
            "display": {"tool_progress": "off", "interim_assistant_messages": False},
            "streaming": {"enabled": True},
        },
    )

    assert result.get("already_sent") is True
    assert not any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_display_streaming_does_not_enable_gateway_streaming(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-display-streaming-cli-only",
        config_data={
            "display": {
                "streaming": True,
                "interim_assistant_messages": True,
            },
            "streaming": {"enabled": False},
        },
    )

    assert result.get("already_sent") is not True
    assert adapter.edits == []
    assert [call["content"] for call in adapter.sent] == ["I'll inspect the repo first."]


@pytest.mark.asyncio
async def test_run_agent_interim_commentary_works_with_tool_progress_off(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-commentary-explicit-on",
        config_data={
            "display": {
                "tool_progress": "off",
                "interim_assistant_messages": True,
            },
        },
    )

    assert result.get("already_sent") is not True
    assert any(call["content"] == "I'll inspect the repo first." for call in adapter.sent)


@pytest.mark.asyncio
async def test_run_agent_bluebubbles_uses_commentary_send_path_for_quick_replies(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        CommentaryAgent,
        session_id="sess-bluebubbles-commentary",
        config_data={"display": {"interim_assistant_messages": True}},
        platform=Platform.BLUEBUBBLES,
        chat_id="iMessage;-;user@example.com",
        chat_type="dm",
        thread_id=None,
        adapter_cls=NonEditingProgressCaptureAdapter,
    )

    assert result.get("already_sent") is not True
    assert [call["content"] for call in adapter.sent] == ["I'll inspect the repo first."]
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_run_agent_previewed_final_marks_already_sent(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        PreviewedResponseAgent,
        session_id="sess-previewed",
        config_data={"display": {"interim_assistant_messages": True}},
    )

    assert result.get("already_sent") is True
    assert [call["content"] for call in adapter.sent] == ["You're welcome."]


@pytest.mark.asyncio
async def test_run_agent_previewed_split_keeps_final_delivery_pending(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        PreviewedSplitAfterCommentaryAgent,
        session_id="sess-split",
        config_data={"display": {"interim_assistant_messages": True}},
    )

    assert result["session_id"] == "sess-split-child"
    assert result.get("already_sent") is not True
    assert [call["content"] for call in adapter.sent] == ["I'll inspect the repo first."]


@pytest.mark.asyncio
async def test_run_agent_matrix_streaming_omits_cursor(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        StreamingRefineAgent,
        session_id="sess-matrix-streaming",
        config_data={
            "display": {"tool_progress": "off", "interim_assistant_messages": False},
            "streaming": {"enabled": True, "edit_interval": 0.01, "buffer_threshold": 1},
        },
        platform=Platform.MATRIX,
        chat_id="!room:matrix.example.org",
        chat_type="group",
        thread_id="$thread",
    )

    assert result.get("already_sent") is True
    all_text = [call["content"] for call in adapter.sent] + [call["content"] for call in adapter.edits]
    assert all_text, "expected streamed Matrix content to be sent or edited"
    assert all("▉" not in text for text in all_text)
    assert any("Continuing to refine:" in text for text in all_text)


class TransformedStreamAgent:
    """Streams a response, then signals the gateway that a plugin hook
    (``transform_llm_output``) modified the final text after streaming
    finished. ``run_conversation`` returns ``response_transformed=True``
    plus a ``final_response`` that diverges from what was streamed.
    """

    def __init__(self, **kwargs):
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.stream_delta_callback:
            self.stream_delta_callback("original answer")
        return {
            "final_response": "original answer\n\n[plugin appended this]",
            "response_previewed": True,
            "response_transformed": True,
            "messages": [],
            "api_calls": 1,
        }


@pytest.mark.asyncio
async def test_transformed_response_edits_streamed_message_in_place(monkeypatch, tmp_path):
    """When a transform_llm_output hook modifies the response after streaming,
    the gateway must edit the existing streamed message in place with the full
    transformed content (so plugins like content filters / appenders reach the
    user) and still mark already_sent=True (no duplicate send).
    """
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        TransformedStreamAgent,
        session_id="sess-transformed-stream",
        config_data={
            "display": {"tool_progress": "off", "interim_assistant_messages": False},
            "streaming": {"enabled": True, "edit_interval": 0.01, "buffer_threshold": 1},
        },
        platform=Platform.MATRIX,
        chat_id="!room:matrix.example.org",
        chat_type="group",
        thread_id="$thread",
        adapter_cls=MetadataEditProgressCaptureAdapter,
    )

    # Final delivery happened (no duplicate send fallback).
    assert result.get("already_sent") is True
    # The transformed final text reached the user — appended portion is present
    # in an edit_message call (not just in the streamed sends).
    edited_texts = [e["content"] for e in adapter.edits]
    assert any("[plugin appended this]" in text for text in edited_texts), (
        f"expected transformed text in adapter.edits, got: {edited_texts!r}"
    )


@pytest.mark.asyncio
async def test_run_agent_queued_message_does_not_treat_commentary_as_final(monkeypatch, tmp_path):
    QueuedCommentaryAgent.calls = 0
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        QueuedCommentaryAgent,
        session_id="sess-queued-commentary",
        pending_text="queued follow-up",
        config_data={"display": {"interim_assistant_messages": True}},
    )

    sent_texts = [call["content"] for call in adapter.sent]
    assert result["final_response"] == "final response 2"
    assert "I'll inspect the repo first." in sent_texts
    assert "final response 1" in sent_texts


@pytest.mark.asyncio
async def test_run_agent_suppresses_silent_first_turn_and_processes_queued_followup(
    monkeypatch, tmp_path,
):
    """Regression: queued direct-send must not leak NO_REPLY to the channel."""
    QueuedSilenceAgent.calls = 0
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        QueuedSilenceAgent,
        session_id="sess-queued-silence",
        pending_text="queued follow-up",
        platform=Platform.SLACK,
        chat_id="C123",
        thread_id="1712345678.000100",
    )

    sent_texts = [call["content"] for call in adapter.sent]
    assert QueuedSilenceAgent.calls == 2
    assert result["final_response"] == "follow-up processed"
    assert "NO_REPLY" not in sent_texts


@pytest.mark.asyncio
async def test_run_agent_sends_normalized_failure_before_queued_followup(
    monkeypatch, tmp_path,
):
    """Queued delivery uses finalized output, not the raw empty agent result."""
    QueuedFailedEmptyAgent.calls = 0
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        QueuedFailedEmptyAgent,
        session_id="sess-queued-failed-empty",
        pending_text="queued follow-up",
        platform=Platform.SLACK,
        chat_id="C123",
        thread_id="1712345678.000100",
    )

    sent_texts = [call["content"] for call in adapter.sent]
    assert QueuedFailedEmptyAgent.calls == 2
    assert result["final_response"] == "follow-up processed"
    assert any("The request failed: provider exploded" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_run_agent_defers_background_review_notification_until_release(monkeypatch, tmp_path):
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        BackgroundReviewAgent,
        session_id="sess-bg-review-order",
        config_data={"display": {"interim_assistant_messages": True}},
    )

    assert result["final_response"] == "done"
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_base_processing_releases_post_delivery_callback_after_main_send():
    """Post-delivery callbacks on the adapter fire after the main response."""
    adapter = ProgressCaptureAdapter()

    async def _handler(event):
        return "done"

    adapter.set_message_handler(_handler)

    released = []

    def _post_delivery_cb():
        released.append(True)
        adapter.sent.append(
            {
                "chat_id": "bg-review",
                "content": "💾 Skill 'prospect-scanner' created.",
                "reply_to": None,
                "metadata": None,
            }
        )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-1",
    )
    session_key = "agent:main:telegram:group:-1001:17585"
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._post_delivery_callbacks[session_key] = _post_delivery_cb

    await adapter._process_message_background(event, session_key)

    sent_texts = [call["content"] for call in adapter.sent]
    assert sent_texts == ["done", "💾 Skill 'prospect-scanner' created."]
    assert released == [True]


@pytest.mark.asyncio
async def test_base_processing_stops_typing_before_hung_post_delivery_callback(
    monkeypatch,
):
    """A stuck post-delivery callback must not keep the typing task alive."""
    monkeypatch.setattr(base_platform, "_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS", 0.01)
    adapter = ProgressCaptureAdapter()
    events = []

    async def _handler(event):
        return "done"

    async def _post_delivery_cb():
        events.append("callback-start")
        await asyncio.Event().wait()

    async def _stop_typing(chat_id):
        events.append("typing-stopped")
        await ProgressCaptureAdapter.stop_typing(adapter, chat_id)

    adapter.set_message_handler(_handler)
    adapter.stop_typing = _stop_typing

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-1",
    )
    session_key = "agent:main:telegram:group:-1001:17585"
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._post_delivery_callbacks[session_key] = _post_delivery_cb

    await asyncio.wait_for(
        adapter._process_message_background(event, session_key), timeout=1.0
    )

    assert [call["content"] for call in adapter.sent] == ["done"]
    # Invariant: typing must stop before the (hung) post-delivery callback
    # starts.  Don't pin the exact stop_typing call count — the shared
    # cleanup path may make more than one bounded stop attempt.
    assert "typing-stopped" in events
    assert "callback-start" in events
    assert events.index("typing-stopped") < events.index("callback-start")
    assert events[: events.index("callback-start")] == (
        ["typing-stopped"] * events.index("callback-start")
    )
    assert any(call["metadata"] == {"stopped": True} for call in adapter.typing)


@pytest.mark.asyncio
async def test_run_agent_drops_tool_progress_after_generation_invalidation(monkeypatch, tmp_path):
    import yaml

    (tmp_path / "config.yaml").write_text(
        yaml.dump({"display": {"tool_progress": "all"}}),
        encoding="utf-8",
    )

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = DelayedProgressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 - register terminal tool metadata

    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="dm-1",
        chat_type="dm",
        thread_id=None,
    )
    session_key = "agent:main:discord:dm:dm-1"
    runner._session_run_generation[session_key] = 1

    original_send = adapter.send
    invalidated = {"done": False}

    async def send_and_invalidate(chat_id, content, reply_to=None, metadata=None):
        result = await original_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if "first command" in content and not invalidated["done"]:
            invalidated["done"] = True
            runner._invalidate_session_run_generation(session_key, reason="test_stop")
        return result

    adapter.send = send_and_invalidate

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-progress-stop",
        session_key=session_key,
        run_generation=1,
    )

    all_progress_text = " ".join(call["content"] for call in adapter.sent)
    all_progress_text += " ".join(call["content"] for call in adapter.edits)
    assert result["final_response"] == "done"
    assert 'first command' in all_progress_text
    assert 'second command' not in all_progress_text


@pytest.mark.asyncio
async def test_run_agent_drops_interim_commentary_after_generation_invalidation(monkeypatch, tmp_path):
    import yaml

    (tmp_path / "config.yaml").write_text(
        yaml.dump({"display": {"tool_progress": "off", "interim_assistant_messages": True}}),
        encoding="utf-8",
    )

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = DelayedInterimAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="dm-2",
        chat_type="dm",
        thread_id=None,
    )
    session_key = "agent:main:discord:dm:dm-2"
    runner._session_run_generation[session_key] = 1

    original_send = adapter.send
    invalidated = {"done": False}

    async def send_and_invalidate(chat_id, content, reply_to=None, metadata=None):
        result = await original_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if content == "first interim" and not invalidated["done"]:
            invalidated["done"] = True
            runner._invalidate_session_run_generation(session_key, reason="test_stop")
        return result

    adapter.send = send_and_invalidate

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-commentary-stop",
        session_key=session_key,
        run_generation=1,
    )

    sent_texts = [call["content"] for call in adapter.sent]
    assert result["final_response"] == "done"
    assert "first interim" in sent_texts
    assert "second interim" not in sent_texts


@pytest.mark.asyncio
async def test_keep_typing_stops_immediately_when_interrupt_event_is_set():
    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        adapter._keep_typing(
            "dm-typing-stop",
            interval=30.0,
            stop_event=stop_event,
        )
    )
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.5)

    normal_typing_calls = [
        call for call in adapter.typing if call.get("metadata") != {"stopped": True}
    ]
    stopped_calls = [
        call for call in adapter.typing if call.get("metadata") == {"stopped": True}
    ]
    assert len(normal_typing_calls) == 1
    assert len(stopped_calls) == 1


@pytest.mark.asyncio
async def test_verbose_mode_does_not_truncate_args_by_default(monkeypatch, tmp_path):
    """Verbose mode with default tool_preview_length (0) should NOT truncate args.

    Previously, verbose mode capped args at 200 chars when tool_preview_length
    was 0 (default).  The user explicitly opted into verbose — show full detail.
    """
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        VerboseAgent,
        session_id="sess-verbose-no-truncate",
        config_data={"display": {"tool_progress": "verbose", "tool_preview_length": 0}},
    )

    assert result["final_response"] == "done"
    # The full 300-char 'x' string should be present, not truncated to 200
    all_content = " ".join(call["content"] for call in adapter.sent)
    all_content += " ".join(call["content"] for call in adapter.edits)
    assert VerboseAgent.LONG_CODE in all_content


@pytest.mark.asyncio
async def test_verbose_mode_respects_explicit_tool_preview_length(monkeypatch, tmp_path):
    """When tool_preview_length is set to a positive value, verbose truncates to that."""
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        VerboseAgent,
        session_id="sess-verbose-explicit-cap",
        config_data={"display": {"tool_progress": "verbose", "tool_preview_length": 50}},
    )

    assert result["final_response"] == "done"
    all_content = " ".join(call["content"] for call in adapter.sent)
    all_content += " ".join(call["content"] for call in adapter.edits)
    # Should be truncated — full 300-char string NOT present
    assert VerboseAgent.LONG_CODE not in all_content
    # But should still contain the truncated portion with "..."
    assert "..." in all_content


class CodeBlockProgressAdapter(ProgressCaptureAdapter):
    """A markdown-capable progress adapter (declares supports_code_blocks)."""

    supports_code_blocks = True


class TerminalCommandAgent:
    """Emits a terminal tool.started with a real, multi-line command arg."""

    CMD = (
        "set -euo pipefail\n"
        "printf 'node: '; node --version\n"
        "npm install -g hyperframes@latest"
    )

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        self.tool_progress_callback(
            "tool.started", "terminal", self.CMD, {"command": self.CMD}
        )
        # Let the async progress task drain the queue and send before returning.
        time.sleep(0.35)
        return {"final_response": "done", "messages": [], "api_calls": 1}


@pytest.mark.asyncio
async def test_terminal_progress_renders_fenced_code_block(monkeypatch, tmp_path):
    """Terminal progress on a markdown-capable (supports_code_blocks) gateway
    renders a bare fenced code block — no language tag (Slack mrkdwn would print
    'bash' as a literal first code line).  In non-verbose ("all"/"new") mode the
    command is collapsed to a single line capped at tool_preview_length so a long
    or multi-line command doesn't render as a huge block (#42634)."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = TerminalCommandAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 - register terminal emoji

    adapter = CodeBlockProgressAdapter(platform=Platform.TELEGRAM)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-terminal-code-block",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "done"
    all_content = " ".join(call["content"] for call in adapter.sent)
    all_content += " ".join(call["content"] for call in adapter.edits)
    # Bare fenced block, no language tag (no '```bash').
    assert "```" in all_content
    assert "```bash" not in all_content
    # Non-verbose collapses to the first line + truncation marker — the later
    # command lines must NOT appear (this was the "huge block" regression).
    assert "set -euo pipefail" in all_content
    assert "npm install -g hyperframes@latest" not in all_content
    assert "node --version" not in all_content
    # No truncated quoted preview for the terminal command.
    assert 'terminal: "' not in all_content


@pytest.mark.asyncio
async def test_terminal_progress_verbose_shows_full_command(monkeypatch, tmp_path):
    """Verbose mode on a markdown-capable gateway renders the FULL multi-line
    command in a bare fenced block (no truncation, no 'bash' tag).  This is the
    parity guarantee for #42634: verbose keeps full detail, non-verbose caps."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "verbose")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = TerminalCommandAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 - register terminal emoji

    adapter = CodeBlockProgressAdapter(platform=Platform.TELEGRAM)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-terminal-code-block-verbose",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "done"
    all_content = " ".join(call["content"] for call in adapter.sent)
    all_content += " ".join(call["content"] for call in adapter.edits)
    assert "```" in all_content
    assert "```bash" not in all_content
    # Full command body present — verbose is uncapped.
    assert "npm install -g hyperframes@latest" in all_content
    assert "node --version" in all_content


@pytest.mark.asyncio
async def test_terminal_progress_no_bash_block_in_verbose_mode(monkeypatch, tmp_path):
    """#41215 also rendered the bash block in verbose mode. The revert removed it
    from both branches, so verbose progress must not emit a fenced ```bash block
    either (verbose still shows args by opt-in, just not as a code block)."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "verbose")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = TerminalCommandAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 - register terminal emoji

    adapter = CodeBlockProgressAdapter(platform=Platform.TELEGRAM)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-terminal-verbose-no-bash",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "done"
    all_content = " ".join(call["content"] for call in adapter.sent)
    all_content += " ".join(call["content"] for call in adapter.edits)
    assert "```bash" not in all_content

class MultiTerminalCommandAgent:
    """Emits several consecutive terminal tool.started events, then a
    different tool, then terminal again — to exercise header collapsing."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        cb("tool.started", "terminal", "echo one", {"command": "echo one"})
        cb("tool.started", "terminal", "echo two", {"command": "echo two"})
        cb("tool.started", "terminal", "echo three", {"command": "echo three"})
        cb("tool.started", "web_search", "query stuff", {"query": "query stuff"})
        cb("tool.started", "terminal", "echo four", {"command": "echo four"})
        time.sleep(0.35)
        return {"final_response": "done", "messages": [], "api_calls": 1}


@pytest.mark.asyncio
async def test_consecutive_terminal_progress_collapses_headers(monkeypatch, tmp_path):
    """Back-to-back terminal calls render ONE "terminal" header followed by
    adjacent code blocks; a different tool in between resets the header so the
    next terminal call gets a fresh one."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = MultiTerminalCommandAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 - register terminal emoji

    adapter = CodeBlockProgressAdapter(platform=Platform.TELEGRAM)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-terminal-consecutive",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "done"
    contents = [call["content"] for call in adapter.sent] + [
        call["content"] for call in adapter.edits
    ]
    final = max(contents, key=len) if contents else ""
    # All four commands present as code blocks.
    for cmd in ("echo one", "echo two", "echo three", "echo four"):
        assert cmd in final
    # Exactly TWO terminal headers: one for the first run of three calls,
    # one for the terminal call after web_search broke the streak.
    assert final.count("terminal\n```") == 2


@pytest.mark.asyncio
async def test_run_agent_renders_live_todo_checklist_when_tool_progress_off(monkeypatch, tmp_path):
    """Task checklists are a dedicated surface, not ordinary tool chrome."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoChecklistAgent,
        session_id="sess-todo-checklist",
        config_data={
            "display": {
                "tool_progress": "off",
                "todo_progress": True,
                "delegated_tasks": "count",
                "interim_assistant_messages": False,
            }
        },
    )

    assert result["final_response"] == "done"
    contents = [call["content"] for call in adapter.sent + adapter.edits]
    assert contents
    final_checklist = contents[-1]
    assert "Working on 1 remaining task:" in final_checklist
    assert "🔄 Run tests" in final_checklist
    assert final_checklist.index("🔄 Run tests") < final_checklist.index("✅ Inspect configuration")
    assert final_checklist.index("✅ Inspect configuration") < final_checklist.index("✅ Implement checklist")
    assert "Delegated 2 tasks 🤖" in final_checklist
    assert "↳ Review gateway integration" not in final_checklist
    assert "should stay hidden" not in "\n".join(contents)
    assert len(adapter.edits) == 2, (
        "one edit should update todo state and one should add delegations; "
        "the repeated todo result must not edit unchanged text"
    )


@pytest.mark.asyncio
async def test_initial_empty_todo_sends_no_checklist(monkeypatch, tmp_path):
    adapter, _ = await _run_with_agent(
        monkeypatch,
        tmp_path,
        InitiallyEmptyTodoAgent,
        session_id="sess-todo-initial-empty",
        config_data={"display": {"tool_progress": "off", "todo_progress": True}},
    )

    assert adapter.sent == []
    assert adapter.edits == []
    assert adapter.deletes == []


@pytest.mark.asyncio
async def test_empty_todo_deletes_existing_checklist(monkeypatch, tmp_path):
    adapter, _ = await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoClearingAgent,
        session_id="sess-todo-clear",
        config_data={"display": {"tool_progress": "off", "todo_progress": True}},
    )

    assert len(adapter.sent) == 1
    assert "Working on 1 task:" in adapter.sent[0]["content"]
    assert adapter.edits == []
    assert adapter.deletes == [{"chat_id": "-1001", "message_id": "progress-1"}]


@pytest.mark.asyncio
async def test_telegram_todo_pin_pins_created_checklist_silently(monkeypatch, tmp_path):
    adapter, _ = await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoChecklistAgent,
        session_id="sess-todo-pin",
        config_data={
            "display": {
                "tool_progress": "off",
                "todo_progress": True,
                "platforms": {"telegram": {"todo_progress_pin": True}},
            }
        },
        adapter_cls=PinningProgressAdapter,
    )

    assert adapter._bot.pins == [
        {
            "chat_id": -1001,
            "message_id": "progress-1",
            "disable_notification": True,
        }
    ]
    assert adapter._bot.unpins == []


@pytest.mark.asyncio
async def test_telegram_todo_pin_replaces_prior_checklist_in_same_topic(monkeypatch, tmp_path):
    adapter = PinningProgressAdapter()
    runner = _make_runner(adapter)
    config_data = {
        "display": {
            "tool_progress": "off",
            "todo_progress": True,
            "platforms": {"telegram": {"todo_progress_pin": True}},
        }
    }

    await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoChecklistAgent,
        session_id="sess-todo-pin-first",
        config_data=config_data,
        runner=runner,
        adapter=adapter,
    )
    await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoChecklistAgent,
        session_id="sess-todo-pin-second",
        config_data=config_data,
        runner=runner,
        adapter=adapter,
    )

    assert adapter._bot.unpins == [
        {"chat_id": -1001, "message_id": "progress-1"}
    ]
    assert adapter._bot.pins == [
        {"chat_id": -1001, "message_id": "progress-1", "disable_notification": True},
        {"chat_id": -1001, "message_id": "progress-2", "disable_notification": True},
    ]


@pytest.mark.asyncio
async def test_telegram_todo_pin_removes_stale_bot_checklists_but_keeps_user_pin(
    monkeypatch, tmp_path
):
    adapter = PinningProgressAdapter()
    runner = _make_runner(adapter)
    runner._pinned_todo_messages[("-1001", "17585")] = "42"
    adapter._bot.pinned_messages = [
        SimpleNamespace(
            message_id=40,
            text="Parish notices for Sunday",
            from_user=SimpleNamespace(is_bot=False),
            message_thread_id=17585,
        ),
        SimpleNamespace(
            message_id=41,
            text="Working on 2 tasks:\n\nFirst old task",
            from_user=SimpleNamespace(is_bot=True),
            message_thread_id=17585,
        ),
        SimpleNamespace(
            message_id=42,
            text="Working on 1 task:\n\nSecond old task",
            from_user=SimpleNamespace(is_bot=True),
            message_thread_id=17585,
        ),
    ]

    await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoChecklistAgent,
        session_id="sess-todo-pin-clean-stale",
        thread_id="17585",
        config_data={
            "display": {
                "tool_progress": "off",
                "todo_progress": True,
                "platforms": {"telegram": {"todo_progress_pin": True}},
            }
        },
        runner=runner,
        adapter=adapter,
    )

    assert adapter._bot.unpins == [
        {"chat_id": -1001, "message_id": 42},
        {"chat_id": -1001, "message_id": 41},
    ]
    assert [message.message_id for message in adapter._bot.pinned_messages] == [40]


@pytest.mark.parametrize(
    "delegated_text",
    [
        "Delegated 1 task 🤖",
        "Delegated 2 tasks 🤖",
        "🤖 Delegated tasks\n↳ Review the implementation",
    ],
)
@pytest.mark.asyncio
async def test_telegram_todo_pin_removes_stale_delegated_only_checklist(
    monkeypatch, tmp_path, delegated_text
):
    adapter = PinningProgressAdapter()
    runner = _make_runner(adapter)
    adapter._bot.pinned_messages = [
        SimpleNamespace(
            message_id=41,
            text=delegated_text,
            from_user=SimpleNamespace(is_bot=True),
            message_thread_id=17585,
        )
    ]

    await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoChecklistAgent,
        session_id="sess-todo-pin-clean-stale-delegated",
        thread_id="17585",
        config_data={
            "display": {
                "tool_progress": "off",
                "todo_progress": True,
                "platforms": {"telegram": {"todo_progress_pin": True}},
            }
        },
        runner=runner,
        adapter=adapter,
    )

    assert adapter._bot.unpins == [{"chat_id": -1001, "message_id": 41}]


@pytest.mark.asyncio
async def test_telegram_todo_pin_does_not_unpin_other_topic(monkeypatch, tmp_path):
    adapter = PinningProgressAdapter()
    runner = _make_runner(adapter)
    config_data = {
        "display": {
            "tool_progress": "off",
            "todo_progress": True,
            "platforms": {"telegram": {"todo_progress_pin": True}},
        }
    }

    for thread_id in ("17585", "17586"):
        await _run_with_agent(
            monkeypatch,
            tmp_path,
            TodoChecklistAgent,
            session_id=f"sess-todo-pin-{thread_id}",
            thread_id=thread_id,
            config_data=config_data,
            runner=runner,
            adapter=adapter,
        )

    assert adapter._bot.unpins == []
    assert runner._pinned_todo_messages == {
        ("-1001", "17585"): "progress-1",
        ("-1001", "17586"): "progress-2",
    }


@pytest.mark.asyncio
async def test_telegram_todo_pin_unpins_cleared_checklist(monkeypatch, tmp_path):
    adapter, _ = await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoClearingAgent,
        session_id="sess-todo-pin-clear",
        config_data={
            "display": {
                "tool_progress": "off",
                "todo_progress": True,
                "platforms": {"telegram": {"todo_progress_pin": True}},
            }
        },
        adapter_cls=PinningProgressAdapter,
    )

    assert adapter._bot.unpins == [
        {"chat_id": -1001, "message_id": "progress-1"}
    ]
    assert adapter.deletes == [{"chat_id": "-1001", "message_id": "progress-1"}]


@pytest.mark.asyncio
async def test_final_drain_delivers_immediate_create_then_clear(monkeypatch, tmp_path):
    adapter, _ = await _run_with_agent(
        monkeypatch,
        tmp_path,
        ImmediateTodoClearingAgent,
        session_id="sess-todo-immediate-clear",
        config_data={"display": {"tool_progress": "off", "todo_progress": True}},
    )

    # The sender may coalesce the pair before the first send, or send then
    # delete. Either way no checklist bubble may remain visible.
    assert len(adapter.sent) == len(adapter.deletes)
    if adapter.sent:
        assert adapter.deletes == [{"chat_id": "-1001", "message_id": "progress-1"}]


@pytest.mark.asyncio
async def test_shutdown_waits_for_inflight_todo_deletion(monkeypatch, tmp_path):
    BlockingDeleteProgressAdapter.latest = None
    run_task = asyncio.create_task(
        _run_with_agent(
            monkeypatch,
            tmp_path,
            TodoClearingAgent,
            session_id="sess-todo-blocked-delete",
            config_data={"display": {"tool_progress": "off", "todo_progress": True}},
            adapter_cls=BlockingDeleteProgressAdapter,
        )
    )

    while BlockingDeleteProgressAdapter.latest is None:
        await asyncio.sleep(0)
    adapter = BlockingDeleteProgressAdapter.latest
    await asyncio.wait_for(adapter.delete_started.wait(), timeout=2)
    await asyncio.sleep(0.5)
    assert not run_task.done(), "shutdown cancelled an in-flight checklist deletion"

    adapter.allow_delete.set()
    completed_adapter, _ = await asyncio.wait_for(run_task, timeout=2)
    assert completed_adapter is adapter
    assert adapter.deletes == [{"chat_id": "-1001", "message_id": "progress-1"}]


@pytest.mark.asyncio
async def test_shutdown_bounds_hung_todo_deletion(monkeypatch, tmp_path):
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(
        gateway_run,
        "_TODO_DELETE_SHUTDOWN_TIMEOUT_SECS",
        0.05,
        raising=False,
    )
    HangingDeleteProgressAdapter.latest = None
    run_task = asyncio.create_task(
        _run_with_agent(
            monkeypatch,
            tmp_path,
            TodoClearingAgent,
            session_id="sess-todo-hung-delete",
            config_data={"display": {"tool_progress": "off", "todo_progress": True}},
            adapter_cls=HangingDeleteProgressAdapter,
        )
    )

    while HangingDeleteProgressAdapter.latest is None:
        await asyncio.sleep(0)
    adapter = HangingDeleteProgressAdapter.latest
    await asyncio.wait_for(adapter.delete_started.wait(), timeout=2)
    completed_adapter = None
    try:
        completed_adapter, _ = await asyncio.wait_for(asyncio.shield(run_task), timeout=0.5)
    except asyncio.TimeoutError:
        adapter.allow_delete.set()
        await asyncio.wait_for(run_task, timeout=2)
        pytest.fail("shutdown waited indefinitely for checklist deletion")

    assert completed_adapter is adapter
    assert adapter.deletes == []
    assert adapter.delete_cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [FailedDeleteProgressAdapter, RaisingDeleteProgressAdapter])
async def test_failed_todo_delete_keeps_message_editable(monkeypatch, tmp_path, adapter_cls):
    adapter, _ = await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoClearThenRestoreAgent,
        session_id=f"sess-todo-delete-failure-{adapter_cls.__name__}",
        config_data={"display": {"tool_progress": "off", "todo_progress": True}},
        adapter_cls=adapter_cls,
    )

    assert len(adapter.sent) == 1
    assert len(adapter.edits) == 1
    assert "Restored task" in adapter.edits[0]["content"]


@pytest.mark.asyncio
async def test_empty_todo_edits_to_delegated_only_content(monkeypatch, tmp_path):
    adapter, _ = await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoClearingWithDelegationAgent,
        session_id="sess-todo-clear-with-delegation",
        config_data={
            "display": {
                "tool_progress": "off",
                "todo_progress": True,
                "delegated_tasks": "goal",
            }
        },
    )

    assert adapter.deletes == []
    assert adapter.edits
    final = adapter.edits[-1]["content"]
    assert final == "🤖 Delegated tasks\n↳ Review the implementation"
    assert "Working on" not in final


@pytest.mark.asyncio
async def test_todo_checklist_does_not_suppress_enabled_delegate_tool_progress(monkeypatch, tmp_path):
    adapter, _ = await _run_with_agent(
        monkeypatch,
        tmp_path,
        TodoChecklistAgent,
        session_id="sess-todo-with-tool-progress",
        config_data={
            "display": {
                "tool_progress": "all",
                "todo_progress": True,
                "interim_assistant_messages": False,
            }
        },
    )

    contents = [call["content"] for call in adapter.sent + adapter.edits]
    assert any("delegating 2 tasks" in content for content in contents)


@pytest.mark.asyncio
async def test_run_agent_relays_thinking_when_tool_progress_off(monkeypatch, tmp_path):
    """_thinking scratch text relays as a bubble when thinking_progress is on,
    even with tool_progress off.

    Regression: agent.tool_progress_callback used to be gated on
    tool_progress_enabled alone, so enabling only thinking_progress left the
    callback None and _thinking never relayed — despite the progress queue
    being created for it (needs_progress_queue = tool OR thinking).
    """
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        ThinkingAgent,
        session_id="sess-thinking-on",
        config_data={"display": {"thinking_progress": True, "tool_progress": "off"}},
    )

    assert result["final_response"] == "done"
    blob = "\n".join(
        [c["content"] for c in adapter.sent] + [c["content"] for c in adapter.edits]
    )
    assert "weighing the options here" in blob


@pytest.mark.asyncio
async def test_run_agent_suppresses_thinking_when_thinking_off(monkeypatch, tmp_path):
    """With thinking_progress off and tool_progress off, _thinking is suppressed
    (no callback wired → no relay)."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")
    adapter, result = await _run_with_agent(
        monkeypatch,
        tmp_path,
        ThinkingAgent,
        session_id="sess-thinking-off",
        config_data={"display": {"thinking_progress": False, "tool_progress": "off"}},
    )

    assert result["final_response"] == "done"
    blob = "\n".join(
        [c["content"] for c in adapter.sent] + [c["content"] for c in adapter.edits]
    )
    assert "weighing the options here" not in blob
