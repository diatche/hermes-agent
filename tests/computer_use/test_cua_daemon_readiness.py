"""Behavioral coverage for bounded embedded cua-driver readiness probes.

Run with scripts/run_tests.sh tests/computer_use/test_cua_daemon_readiness.py.
The fake status command models a contended host without launching a daemon.
"""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from tools.computer_use import cua_backend
from tools.computer_use.cua_backend_daemon import _EmbeddedCuaDaemon


def test_socket_readiness_allows_slow_status_probe_within_startup_deadline():
    daemon = _EmbeddedCuaDaemon("/usr/bin/cua-driver", "unrestricted")
    daemon._command = "/usr/bin/cua-driver"
    observed = {}

    def slow_status_probe(command, *, timeout, **kwargs):
        observed.update(command=command, timeout=timeout, kwargs=kwargs)
        if timeout < 5.0:
            raise subprocess.TimeoutExpired(command, timeout)
        return SimpleNamespace(returncode=0)

    with patch.object(cua_backend, "_run_quiet", side_effect=slow_status_probe):
        assert daemon._socket_ready({"PATH": "/usr/bin"}) is True

    assert observed["command"] == [
        "/usr/bin/cua-driver",
        "status",
        "--socket",
        daemon.socket_path,
    ]
    assert observed["timeout"] == 5.0
