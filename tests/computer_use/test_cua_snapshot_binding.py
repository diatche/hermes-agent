"""Regression coverage for snapshot-bound actions across driver schema versions.

Run with scripts/run_tests.sh tests/computer_use/test_cua_snapshot_binding.py.
The fake transport enforces the driver boundary; no desktop input is delivered.
"""
from unittest.mock import Mock

import pytest

from tools.computer_use.cua_backend import CuaDriverBackend


@pytest.mark.parametrize('action', ['click', 'scroll', 'set_value'])
@pytest.mark.parametrize('support', ['schema', 'legacy_capability', 'unsupported'])
def test_element_actions_preserve_snapshot_identity(action, support):
    backend = CuaDriverBackend()
    session = backend._session
    session._capabilities = {action: {'accessibility.element_tokens'} if support == 'legacy_capability' else set()}
    session._tool_schemas = {action: {'properties': {'element_token': {'type': 'string'}}}} if support == 'schema' else {}
    backend._active_pid = 111
    backend._active_window_id = 222
    backend._snapshot_tokens = {5: 's00000001:5'}

    def dispatch(name, args):
        if support == 'unsupported':
            assert 'element_token' not in args  # additionalProperties:false
        else:
            assert args.get('element_token') == 's00000001:5', 'snapshot_id_required'
        return {'data': 'ok', 'structuredContent': None, 'isError': False}

    session.call_tool = Mock(side_effect=dispatch)
    if action == 'scroll':
        result = backend.scroll(element=5, direction='down')
    elif action == 'set_value':
        result = backend.set_value('test', element=5)
    else:
        result = backend.click(element=5)
    assert result.ok, result.message


def test_coordinate_click_does_not_attach_cached_element_token():
    backend = CuaDriverBackend()
    backend._active_pid = 111
    backend._active_window_id = 222
    backend._snapshot_tokens = {5: 's00000001:5'}
    backend._session._tool_schemas = {'click': {'properties': {'element_token': {}}}}

    def dispatch(name, args):
        assert 'element_token' not in args
        return {'data': 'ok', 'structuredContent': None, 'isError': False}

    backend._session.call_tool = dispatch
    assert backend.click(x=10, y=20).ok
