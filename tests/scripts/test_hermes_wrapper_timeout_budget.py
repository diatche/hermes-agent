"""Behavior tests for coordinated Hermes wrapper timeout budgets."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "hermes-wrapper-timeout-budget.py"
)
PLIST = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "nz.diatche.hermes-gateway.plist"
)


def _result(tmp_path: Path, yaml: str, field: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "config.yaml").write_text(
        yaml,
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--field", field],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _budget(tmp_path: Path, drain: int | float, field: str) -> int:
    result = _result(
        tmp_path,
        f"agent:\n  restart_drain_timeout: {drain}\n",
        field,
    )
    result.check_returncode()
    return int(result.stdout.strip())


def test_timeout_budgets_follow_gateway_drain_config(tmp_path: Path) -> None:
    assert _budget(tmp_path, 12, "wrapper-grace") == 77
    assert _budget(tmp_path, 12, "controller-wait") == 87

    assert _budget(tmp_path, 42, "wrapper-grace") == 107
    assert _budget(tmp_path, 42, "controller-wait") == 117


def test_launchd_ceiling_exceeds_maximum_supported_shutdown(tmp_path: Path) -> None:
    expected = _budget(tmp_path, 12, "launchd-exit-timeout")
    with PLIST.open("rb") as stream:
        plist = plistlib.load(stream)

    assert expected == 3700
    assert plist["ExitTimeOut"] == expected


@pytest.mark.parametrize("raw", ["bogus", "-5", ".nan", "true", "3600.1"])
def test_invalid_or_unsupported_drain_config_fails_closed(
    tmp_path: Path, raw: str
) -> None:
    result = _result(
        tmp_path,
        f"agent:\n  restart_drain_timeout: {raw}\n",
        "wrapper-grace",
    )

    assert result.returncode != 0
    assert not result.stdout.strip()


def test_fractional_and_maximum_budgets_are_ceiled(tmp_path: Path) -> None:
    assert _budget(tmp_path, 0.1, "wrapper-grace") == 66
    assert _budget(tmp_path, 0.1, "controller-wait") == 76
    assert _budget(tmp_path, 3600, "wrapper-grace") == 3665
    assert _budget(tmp_path, 3600, "controller-wait") == 3675


def test_malformed_yaml_fails_closed(tmp_path: Path) -> None:
    result = _result(tmp_path, "agent: [\n", "wrapper-grace")

    assert result.returncode != 0
    assert not result.stdout.strip()


def test_json_output_returns_one_correlated_snapshot(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "agent:\n  restart_drain_timeout: 12\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == (
        '{"controller-wait": 87, "launchd-exit-timeout": 3700, '
        '"wrapper-grace": 77}'
    )
