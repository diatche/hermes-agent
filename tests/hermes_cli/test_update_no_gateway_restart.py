"""Tests for externally supervised update process control."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from hermes_cli.subcommands.update import build_update_parser


def test_update_parser_accepts_no_gateway_restart() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    def sentinel(_args: argparse.Namespace) -> None:
        return None

    build_update_parser(subparsers, cmd_update=sentinel)

    args = parser.parse_args(["update", "--no-gateway-restart"])

    assert args.no_gateway_restart is True
    assert args.func is sentinel


def test_no_gateway_restart_rejects_windows_before_update_side_effects(monkeypatch) -> None:
    import hermes_cli.update_cmd as update_cmd

    side_effect_reached = False

    def mark_side_effect(_args) -> None:
        nonlocal side_effect_reached
        side_effect_reached = True

    monkeypatch.setattr(update_cmd, "_is_windows", lambda: True)
    monkeypatch.setattr(update_cmd, "_run_pre_update_backup", mark_side_effect)

    with pytest.raises(SystemExit) as raised:
        update_cmd._cmd_update_impl(
            SimpleNamespace(no_gateway_restart=True), gateway_mode=False
        )

    assert raised.value.code == 2
    assert side_effect_reached is False
