"""Authenticated Codex proxy contracts, adapted from PR #92750."""

import asyncio
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from hermes_cli.proxy.adapters import get_adapter
from hermes_cli.proxy.adapters.base import UpstreamCredential
from hermes_cli.proxy.server import create_app, run_server


def jwt(account):
    payload = {
        "exp": 4102444800,
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }
    return (
        "header."
        + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        + ".signature"
    )


def test_codex_security_and_profile_pool(tmp_path, monkeypatch):
    from agent.secret_scope import (
        set_multiplex_active,
        set_secret_scope,
        reset_secret_scope,
    )
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    adapter = get_adapter("openai-codex")
    with pytest.raises(RuntimeError, match="client authentication"):
        create_app(adapter)
    with pytest.raises(RuntimeError, match="loopback-only"):
        asyncio.run(
            run_server(adapter, host="0.0.0.0", port=0, client_auth_token="local")
        )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    homes = [tmp_path / "a", tmp_path / "b"]
    for home, account in zip(homes, ["a", "b"]):
        home.mkdir()
        (home / "auth.json").write_text(
            json.dumps({
                "version": 1,
                "providers": {},
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": account,
                            "label": account,
                            "auth_type": "oauth",
                            "source": "manual",
                            "priority": 0,
                            "access_token": jwt(account),
                            "refresh_token": "fixture-refresh-" + account,
                            "expires_at": "2099-01-01T00:00:00Z",
                            "base_url": "https://chatgpt.com/backend-api/codex",
                        }
                    ]
                },
            })
        )
    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    set_multiplex_active(True)
    try:
        for home, account in [(homes[0], "a"), (homes[1], "b"), (homes[0], "a")]:
            ht = set_hermes_home_override(home)
            st = set_secret_scope({})
            try:
                credential = adapter.get_credential()
                assert credential.bearer == jwt(account)
                assert (
                    adapter.get_upstream_headers(credential)["ChatGPT-Account-ID"]
                    == account
                )
            finally:
                reset_secret_scope(st)
                reset_hermes_home_override(ht)
    finally:
        set_multiplex_active(False)
    # Alternate profiles between issue and retry: cached retry state must not leak.
    set_multiplex_active(True)
    try:
        issued = {}
        for home, account in zip(homes, ["a", "b"]):
            ht = set_hermes_home_override(home)
            st = set_secret_scope({})
            try:
                issued[account] = adapter.get_credential()
            finally:
                reset_secret_scope(st)
                reset_hermes_home_override(ht)
        ht = set_hermes_home_override(homes[0])
        st = set_secret_scope({})
        try:
            from hermes_cli import auth

            refreshes = []

            def refresh(access_token, refresh_token):
                refreshes.append((access_token, refresh_token))
                return {
                    "access_token": jwt("a-refreshed"),
                    "refresh_token": "rotated-a",
                }

            monkeypatch.setattr(auth, "refresh_codex_oauth_pure", refresh)
            renewed = adapter.get_retry_credential(
                failed_credential=issued["a"], status_code=401
            )
            assert renewed is not None and renewed.bearer == jwt("a-refreshed")
            coalesced = adapter.get_retry_credential(
                failed_credential=issued["a"], status_code=401
            )
            assert coalesced == renewed
            assert refreshes == [(jwt("a"), "fixture-refresh-a")]
            persisted = json.loads((homes[0] / "auth.json").read_text())
            assert (
                persisted["credential_pool"]["openai-codex"][0]["access_token"]
                == renewed.bearer
            )
            assert (
                adapter.get_retry_credential(failed_credential=renewed, status_code=429)
                is None
            )
        finally:
            reset_secret_scope(st)
            reset_hermes_home_override(ht)
        ht = set_hermes_home_override(homes[1])
        st = set_secret_scope({})
        try:
            assert adapter.get_credential().bearer == jwt("b")
            raw = json.loads((homes[1] / "auth.json").read_text())
            raw["credential_pool"]["openai-codex"][0]["base_url"] = (
                "http://127.0.0.1:9999/steal"
            )
            (homes[1] / "auth.json").write_text(json.dumps(raw))
            with pytest.raises(RuntimeError, match="untrusted"):
                adapter.get_credential()
        finally:
            reset_secret_scope(st)
            reset_hermes_home_override(ht)
    finally:
        set_multiplex_active(False)
    from hermes_cli.proxy.cli import _read_client_auth_token, cmd_proxy_start

    if os.name == "nt":
        return  # Native ACL validation requires the Windows lane.
    token = tmp_path / "token"
    token.write_text("downstream-secret\n")
    token.chmod(0o600)
    assert _read_client_auth_token(str(token)) == "downstream-secret"
    for content, message in [
        ("", "empty"),
        ("first\nsecond", "whitespace"),
        ("x" * 4097, "limit"),
    ]:
        token.write_text(content)
        with pytest.raises(ValueError, match=message):
            _read_client_auth_token(str(token))
    token.write_text("downstream-secret")
    token.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        _read_client_auth_token(str(token))
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(ValueError, match="regular file"):
        _read_client_auth_token(str(link))
    assert (
        cmd_proxy_start(
            SimpleNamespace(
                provider="openai-codex", host="0.0.0.0", auth_token_file=None
            )
        )
        == 2
    )


def test_codex_authenticated_raw_stream_and_identity(monkeypatch):
    adapter = get_adapter("openai-codex")
    seen = []
    lookups = []
    body = b'{ "model":"gpt-6-luna", "text":{"format":{"type":"json_schema","name":"result","strict":true,"schema":{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}}}, "stream":true }'
    frames = b'event: response.completed\ndata: {"type":"response.completed","lastOne":true}\n\n'

    async def scenario():
        release = asyncio.Event()

        async def upstream(request):
            seen.append((request.raw_path, await request.read(), dict(request.headers)))
            if request.query.get("retry") and request.headers[
                "Authorization"
            ] == "Bearer " + jwt("trusted"):
                return web.Response(status=401)
            if request.query.get("redirect"):
                return web.Response(status=302, headers={"Location": "/stolen"})
            response = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"}
            )
            await response.prepare(request)
            await response.write(frames[:17])
            await release.wait()
            await response.write(frames[17:])
            await response.write_eof()
            return response

        up = web.Application()
        up.router.add_route("*", "/{tail:.*}", upstream)
        async with TestServer(up) as upstream_server:

            def credential():
                lookups.append(True)
                return UpstreamCredential(
                    jwt("trusted"), str(upstream_server.make_url("")).rstrip("/")
                )

            monkeypatch.setattr(adapter, "get_credential", credential)
            monkeypatch.setattr(
                adapter, "is_authenticated", lambda: lookups.append(True) or True
            )
            async with TestClient(
                TestServer(create_app(adapter, client_auth_token="local"))
            ) as client:
                for path in ["/health", "/v1/responses", "/v1/models"]:
                    for headers in [
                        {},
                        {"Authorization": "Bearer wrong"},
                        {"Authorization": "Basic local"},
                    ]:
                        response = await client.request(
                            "GET" if path == "/health" else "POST",
                            path,
                            headers=headers,
                        )
                        assert response.status == 401
                assert not lookups and not seen
                response = await client.post(
                    URL("/v1/responses?x=%252f&x=a%2Bb", encoded=True),
                    data=body,
                    headers={
                        "Authorization": "Bearer local",
                        "user-agent": "spoof",
                        "ORIGINATOR": "spoof",
                        "chatgpt-account-id": "spoof",
                        "x-openai-internal-codex-residency": "spoof",
                    },
                )
                assert response.status == 200
                first = await asyncio.wait_for(
                    response.content.readexactly(17), timeout=2
                )
                release.set()
                assert first + await response.read() == frames
                path, received, headers = seen[0]
                assert path == "/responses?x=%252f&x=a%2Bb"
                assert received == body
                assert headers["Authorization"] == "Bearer " + jwt("trusted")
                assert headers["ChatGPT-Account-ID"] == "trusted"
                assert headers["originator"] == "hermes-agent"
                assert headers["User-Agent"].startswith("HermesAgent/")
                assert not any(
                    k.lower() == "x-openai-internal-codex-residency" for k in headers
                )
                denied = await client.post(
                    "/v1/chat/completions", headers={"Authorization": "Bearer local"}
                )
                assert denied.status == 404

                def retry(*, failed_credential, status_code):
                    assert (
                        failed_credential.bearer == jwt("trusted")
                        and status_code == 401
                    )
                    return UpstreamCredential(
                        jwt("refreshed"), str(upstream_server.make_url("")).rstrip("/")
                    )

                monkeypatch.setattr(adapter, "get_retry_credential", retry)
                retried = await client.post(
                    "/v1/responses?retry=1",
                    data=body,
                    headers={"Authorization": "Bearer local"},
                )
                assert retried.status == 200 and await retried.read() == frames
                assert [item[1] for item in seen[-2:]] == [body, body]
                assert seen[-1][2]["ChatGPT-Account-ID"] == "refreshed"
                redirected = await client.post(
                    "/v1/responses?redirect=1",
                    data=body,
                    headers={"Authorization": "Bearer local"},
                    allow_redirects=False,
                )
                assert redirected.status == 302
                await redirected.read()
                assert not any(item[0] == "/stolen" for item in seen)

    asyncio.run(scenario())
