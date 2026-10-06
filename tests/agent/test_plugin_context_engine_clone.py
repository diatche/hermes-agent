"""A plugin-registered context engine is one shared instance; agent init hands each agent its own
copy through ``clone_for_agent()`` (default deepcopy), so engines with uncopyable state (locks,
SQLite connections — hermes-lcm) stay selectable and a child's model never leaks into the parent
(#99640, #42449)."""

import threading
from unittest.mock import MagicMock, patch

from agent.agent_init import _select_context_engine
from agent.context_engine import ContextEngine
from run_agent import AIAgent


class _Engine(ContextEngine):
    engine_name = "lcm"

    @property
    def name(self):
        return self.engine_name

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, current_tokens=None):
        return messages


class _LockedEngine(_Engine):
    """Holds a lock (deepcopy raises) and hands out per-agent clones like hermes-lcm does."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self.clones = 0

    def clone_for_agent(self):
        self.clones += 1
        return _LockedEngine()


def _select(engine):
    with (patch("plugins.context_engine.load_context_engine", return_value=None),
          patch("hermes_cli.plugins.get_plugin_context_engine", return_value=engine)):
        return _select_context_engine({"context": {"engine": engine.name}})


def test_engine_with_uncopyable_state_is_selected_via_clone_for_agent():
    singleton = _LockedEngine()
    selected = _select(singleton)
    assert isinstance(selected, _LockedEngine) and selected is not singleton
    assert singleton.clones == 1


def test_default_clone_isolates_parent_from_child_update_model():
    singleton = _Engine()
    singleton.update_model(model="big", context_length=1_000_000)
    child = _select(singleton)
    child.update_model(model="small", context_length=204_800)
    assert child is not singleton
    assert (singleton.context_length, child.context_length) == (1_000_000, 204_800)


def test_agent_closes_context_engine_clone_once():
    agent = object.__new__(AIAgent)
    engine = MagicMock()
    setattr(agent, "context_compressor", engine)

    agent._close_context_engine()
    agent._close_context_engine()

    engine.close.assert_called_once_with()


def test_release_clients_does_not_close_borrowed_context_engine():
    owner = object.__new__(AIAgent)
    borrower = object.__new__(AIAgent)
    engine = MagicMock()
    owner.context_compressor = engine
    borrower.context_compressor = engine
    borrower._active_children = []
    borrower._active_children_lock = threading.Lock()
    borrower.client = None
    borrower._codex_session = None

    borrower.release_clients()

    engine.close.assert_not_called()
    engine.prepare_messages.return_value = ["still-open"]
    assert owner.context_compressor.prepare_messages([]) == ["still-open"]
