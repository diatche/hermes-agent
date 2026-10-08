"""Signed HTTP script acknowledgements: only successful intentional silence is ignored.

Run with pytest; uses an isolated profile, real subprocesses and an ephemeral HTTP
listener, never live CRM processors, configs, outboxes or gateway services.
"""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


@pytest.fixture
def script_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "processor.py"
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "script_timeout_seconds": 1,
        "routes": {"crm": {"secret": "fixture-secret", "script": script.name,
                            "events": ["message.upserted"], "prompt": "Message: {text}"}},
    }))
    adapter.handle_message = AsyncMock()
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    body = json.dumps({"event_type": "message.upserted", "text": "Hello from Alice"}).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json", "X-Request-ID": "stable-event-1",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": hmac.new(
            b"fixture-secret", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest(),
    }
    return script, adapter, app, {"data": body, "headers": headers}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    "print('[SILENT]')", "", "print('{\"__hermes_ignore__\": true}')",
    "print('{\"[SILENT]\": true}')",
])
async def test_successful_silent_processor_is_acknowledged_without_agent(source, script_route):
    script, adapter, app, post = script_route
    script.write_text(source, encoding="utf-8")
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/webhooks/crm", **post)
        assert response.status == 200
        assert await response.json() == {"status": "ignored", "reason": "script", "route": "crm"}
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "nonzero", "nonzero-silent", "timeout", "missing", "launch", "malformed-output",
    "malformed-result", "missing-transformed-payload", "processor-exception", "redaction",
])
async def test_processor_failure_is_not_acknowledged_and_same_event_can_retry(
        failure, script_route, monkeypatch, caplog):
    script, adapter, app, post = script_route
    sources = {
        "nonzero": "import sys\nprint('private-processor-secret', file=sys.stderr)\nsys.exit(7)",
        "nonzero-silent": "import sys\nprint('[SILENT]')\nsys.exit(7)",
        "timeout": "import time\ntime.sleep(60)",
        "malformed-output": "print('[]')",
    }
    if failure != "missing":
        script.write_text(sources.get(failure, "print('[SILENT]')"), encoding="utf-8")

    async with TestClient(TestServer(app)) as client:
        with monkeypatch.context() as patch:
            if failure == "launch":
                patch.setattr("gateway.platforms.webhook_filters.sys.executable", str(script.parent / "missing-python"))
            elif failure == "malformed-result":
                patch.setattr(adapter._route_processor, "run_route_script", lambda *args: (None, None))
            elif failure == "missing-transformed-payload":
                patch.setattr(adapter._route_processor, "run_route_script", lambda *args: (True, None))
            elif failure == "processor-exception":
                def broken(*args):
                    raise RuntimeError("private-processor-secret")
                patch.setattr(adapter._route_processor, "run_route_script", broken)
            elif failure == "redaction":
                def broken(*args):
                    raise RuntimeError("private-processor-secret")
                patch.setattr("agent.redact.redact_sensitive_text", broken)
            response = await client.post("/webhooks/crm", **post)
            assert response.status == 503
            result = await response.json()
            assert result == {"status": "error", "reason": "script_failed", "route": "crm"}
            # Beeper/Teams require 2xx; HA requires an accepted/success/ignored
            # body after urlopen succeeds. Neither contract acknowledges this response.
            assert not 200 <= response.status < 300
            assert result["status"] not in {"accepted", "success", "ignored"}
            assert "private-processor-secret" not in await response.text()
            adapter.handle_message.assert_not_called()
        script.write_text("print('[SILENT]')", encoding="utf-8")
        retried = await client.post("/webhooks/crm", **post)
        assert retried.status == 200
        assert (await retried.json())["status"] == "ignored"
    adapter.handle_message.assert_not_called()
    assert "private-processor-secret" not in caplog.text
