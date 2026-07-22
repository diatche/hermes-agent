#!/usr/bin/env python3
"""Resolve coordinated timeout budgets for Pavel's HermesGateway wrapper.

Usage: ``python3 scripts/hermes-wrapper-timeout-budget.py --field FIELD``.
Reads the effective Hermes config and gateway watchdog policy, then emits one
integer-second budget for the signed app, controller, or static LaunchAgent.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.restart import (  # noqa: E402
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    parse_restart_drain_timeout,
)
from gateway.shutdown_watchdog import resolve_shutdown_watchdog_delay  # noqa: E402
import yaml  # noqa: E402

from hermes_cli.config import get_config_path  # noqa: E402

WRAPPER_CLEANUP_MARGIN_S = 5.0
WRAPPER_KILL_WAIT_S = 5.0
CONTROLLER_STOP_MARGIN_S = 5.0
MAXIMUM_SUPPORTED_DRAIN_TIMEOUT_S = 3600.0
LAUNCHD_EXIT_TIMEOUT_S = 3700


def timeout_budgets() -> dict[str, int]:
    """Return correlated timeout budgets derived from effective gateway config."""
    config_path = get_config_path()
    raw_config: dict[object, object] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError("config.yaml must contain a mapping")
        raw_config = loaded or {}
    agent = raw_config.get("agent", {})
    if not isinstance(agent, dict):
        raise ValueError("agent must be a mapping")
    raw = agent.get("restart_drain_timeout", DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT)
    if isinstance(raw, bool):
        raise ValueError("agent.restart_drain_timeout must be a number, not a boolean")
    try:
        strict_drain = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "agent.restart_drain_timeout must be a finite non-negative number"
        ) from exc
    if not math.isfinite(strict_drain) or strict_drain < 0:
        raise ValueError("agent.restart_drain_timeout must be a finite non-negative number")
    drain = parse_restart_drain_timeout(raw)
    if drain != strict_drain:
        raise RuntimeError("gateway drain parser changed a strictly validated value")
    if drain > MAXIMUM_SUPPORTED_DRAIN_TIMEOUT_S:
        raise ValueError(
            "agent.restart_drain_timeout exceeds the wrapper-supported maximum "
            f"of {MAXIMUM_SUPPORTED_DRAIN_TIMEOUT_S:.0f} seconds"
        )

    wrapper_grace = resolve_shutdown_watchdog_delay(drain) + WRAPPER_CLEANUP_MARGIN_S
    controller_wait = (
        wrapper_grace + WRAPPER_KILL_WAIT_S + CONTROLLER_STOP_MARGIN_S
    )
    maximum_shutdown = (
        resolve_shutdown_watchdog_delay(MAXIMUM_SUPPORTED_DRAIN_TIMEOUT_S)
        + WRAPPER_CLEANUP_MARGIN_S
        + WRAPPER_KILL_WAIT_S
    )
    if LAUNCHD_EXIT_TIMEOUT_S <= maximum_shutdown:
        raise RuntimeError("launchd ExitTimeOut does not exceed maximum wrapper shutdown")

    return {
        "wrapper-grace": math.ceil(wrapper_grace),
        "controller-wait": math.ceil(controller_wait),
        "launchd-exit-timeout": LAUNCHD_EXIT_TIMEOUT_S,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument(
        "--field",
        choices=("wrapper-grace", "controller-wait", "launchd-exit-timeout"),
    )
    output.add_argument("--json", action="store_true")
    args = parser.parse_args()
    budgets = timeout_budgets()
    print(json.dumps(budgets, sort_keys=True) if args.json else budgets[args.field])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
