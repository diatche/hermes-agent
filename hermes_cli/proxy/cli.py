"""CLI handlers for the ``hermes proxy`` subcommand."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path
import sys
from typing import Any

from hermes_cli.proxy.adapters import ADAPTERS, get_adapter
from hermes_cli.proxy.server import (
    AIOHTTP_AVAILABLE, DEFAULT_HOST, DEFAULT_PORT, is_loopback_host, run_server
)

logger = logging.getLogger(__name__)


_MAX_CLIENT_AUTH_TOKEN_CHARS = 4096


def _validate_windows_owner_only_acl(
    *,
    owner_sid: str,
    allowed_sids: set[str],
    allowed_aces: list[tuple[int, str]],
) -> None:
    """Reject Windows file ACLs that grant authority beyond owner and SYSTEM."""
    if owner_sid not in allowed_sids:
        raise ValueError("Proxy auth token file has the wrong owner on Windows.")
    for mask, sid in allowed_aces:
        if mask and sid not in allowed_sids:
            raise ValueError("Proxy auth token file has a permissive DACL on Windows.")


def _verify_windows_owner_only_descriptor(descriptor: int) -> None:
    """Verify the opened Windows token file's owner and DACL via pywin32."""
    if sys.platform != "win32":
        raise RuntimeError("Windows token-file ACL verification requires Windows.")

    import msvcrt

    import win32api
    import win32con
    import win32security

    process_token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    current_sid = win32security.GetTokenInformation(
        process_token, win32security.TokenUser
    )[0]
    system_sid = win32security.ConvertStringSidToSid("S-1-5-18")
    allowed_sids = {
        win32security.ConvertSidToStringSid(current_sid),
        win32security.ConvertSidToStringSid(system_sid),
    }

    handle = msvcrt.get_osfhandle(descriptor)
    info = (
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
    )
    security = win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT, info)
    owner = security.GetSecurityDescriptorOwner()
    owner_sid = win32security.ConvertSidToStringSid(owner)
    dacl = security.GetSecurityDescriptorDacl()
    if dacl is None:
        raise ValueError("Proxy auth token file has a null DACL on Windows.")

    allow_types = {
        win32security.ACCESS_ALLOWED_ACE_TYPE,
        win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
        getattr(win32security, "ACCESS_ALLOWED_CALLBACK_ACE_TYPE", 9),
        getattr(win32security, "ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE", 11),
    }
    allowed_aces: list[tuple[int, str]] = []
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        if ace[0][0] in allow_types:
            allowed_aces.append((ace[1], win32security.ConvertSidToStringSid(ace[-1])))
    _validate_windows_owner_only_acl(
        owner_sid=owner_sid,
        allowed_sids=allowed_sids,
        allowed_aces=allowed_aces,
    )


def _read_client_auth_token(path: str) -> str:
    """Read a bounded token from an owner-only regular file."""
    token_path = Path(path).expanduser()
    metadata = token_path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(
            "Proxy auth token path must be a regular file (not a symlink)."
        )
    if os.name != "nt":
        if metadata.st_mode & 0o077:
            raise ValueError("Proxy auth token file must be owner-only (mode 0600).")
        get_euid = getattr(os, "geteuid", None)
        if get_euid is not None and metadata.st_uid != get_euid():
            raise ValueError("Proxy auth token file must be owned by the current user.")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(token_path, flags)
    except OSError as exc:
        raise ValueError(
            "Proxy auth token path must be a readable regular file."
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise ValueError("Proxy auth token file changed while it was opened.")
        if os.name == "nt":
            _verify_windows_owner_only_descriptor(descriptor)
        else:
            if opened.st_mode & 0o077:
                raise ValueError(
                    "Proxy auth token file must be owner-only (mode 0600)."
                )
            get_euid = getattr(os, "geteuid", None)
            if get_euid is not None and opened.st_uid != get_euid():
                raise ValueError(
                    "Proxy auth token file must be owned by the current user."
                )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            raw_token = handle.read(_MAX_CLIENT_AUTH_TOKEN_CHARS + 1)
            if len(raw_token) > _MAX_CLIENT_AUTH_TOKEN_CHARS:
                raise ValueError("Proxy auth token exceeds the 4096-character limit.")
            token = raw_token.strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if not token:
        raise ValueError("Proxy auth token file is empty.")
    if len(token) > _MAX_CLIENT_AUTH_TOKEN_CHARS:
        raise ValueError("Proxy auth token exceeds the 4096-character limit.")
    if any(character.isspace() for character in token):
        raise ValueError("Proxy auth token must be a single value without whitespace.")
    return token



def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def cmd_proxy_start(args: Any) -> int:
    """Run the proxy server in the foreground."""
    if not AIOHTTP_AVAILABLE:
        _err("hermes proxy requires aiohttp. Run `hermes setup` to install it.")
        return 1
    provider = getattr(args, "provider", None) or "nous"
    try:
        adapter = get_adapter(provider)
    except ValueError as exc:
        _err(f"Error: {exc}")
        return 2
    if not adapter.is_authenticated():
        auth_hint = getattr(adapter, "auth_hint", f"hermes auth add {adapter.name}")
        _err(f"Not logged into {adapter.display_name}. Run `{auth_hint}` first.")
        return 2
    host = getattr(args, "host", None) or DEFAULT_HOST
    port = getattr(args, "port", None) or DEFAULT_PORT
    if adapter.loopback_only and not is_loopback_host(host):
        print(
            f"Error: {adapter.display_name} proxy is loopback-only; "
            f"refusing bind host {host!r}.",
            file=sys.stderr,
        )
        return 2

    token_file = getattr(args, "auth_token_file", None)
    if adapter.requires_client_auth and not token_file:
        print(
            f"Error: {adapter.display_name} requires client authentication; "
            "provide an owner-only regular file with --auth-token-file.",
            file=sys.stderr,
        )
        return 2
    client_auth_token = None
    if token_file:
        try:
            client_auth_token = _read_client_auth_token(str(token_file))
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    client_auth_message = (
        "  Client auth:    required (bearer from owner-only token file)\n"
        if client_auth_token
        else "  Client auth:    any bearer accepted\n"
    )

    _err(
        f"Starting Hermes proxy for {adapter.display_name}\n"
        f"  Listening on:  http://{host}:{port}/v1\n"
        f"  Forwarding to: (resolved per-request from your subscription)\n"
        f"{client_auth_message}"
        f"\n"
        f"Press Ctrl+C to stop."
    )
    try:
        asyncio.run(run_server(adapter, host=host, port=port, client_auth_token=client_auth_token))
    except KeyboardInterrupt:
        _err("\nproxy: stopped")
    except OSError as exc:
        _err(f"proxy: failed to bind {host}:{port}: {exc}")
        return 1
    return 0


def cmd_proxy_status(args: Any) -> int:
    """Print the status of each configured upstream adapter."""
    print("Hermes proxy upstream adapters\n")
    for name in sorted(ADAPTERS):
        adapter = get_adapter(name)
        if not adapter.is_authenticated():
            print(f"  [{name:8s}] {adapter.display_name} — not logged in")
            continue
        try:
            cred = adapter.get_credential()
        except Exception as exc:
            print(f"  [{name:8s}] {adapter.display_name} — credentials need attention ({exc})")
            continue
        expires = f" (bearer expires {cred.expires_at})" if cred.expires_at else ""
        print(f"  [{name:8s}] {adapter.display_name} — ready{expires}")
    print("\nStart the proxy with: hermes proxy start [--provider <name>]")
    return 0


def cmd_proxy_list_providers(args: Any) -> int:
    """List available proxy upstream providers."""
    print("Available proxy upstream providers:")
    for name in sorted(ADAPTERS):
        adapter = get_adapter(name)
        print(f"  {name}  — {adapter.display_name}")
    return 0


_SUBCOMMANDS = {
    "start": cmd_proxy_start,
    "status": cmd_proxy_status,
    "providers": cmd_proxy_list_providers,
    "list": cmd_proxy_list_providers,
}


def cmd_proxy(args: Any) -> int:
    """Dispatch ``hermes proxy <subcommand>``; no/unknown subcommand prints the short help."""
    handler = _SUBCOMMANDS.get(getattr(args, "proxy_command", None))
    if handler is not None:
        return handler(args)
    _err(
        "hermes proxy — local OpenAI-compatible proxy that attaches your\n"
        "OAuth-authenticated provider credentials to outbound requests.\n"
        "\n"
        "Subcommands:\n"
        "  hermes proxy start [--provider openai-codex|nous|xai] [--host 127.0.0.1] [--port 8645]\n"
        "      Run the proxy in the foreground (--auth-token-file PATH required for Codex).\n"
        "  hermes proxy status\n"
        "      Show which upstream adapters are ready.\n"
        "  hermes proxy providers\n"
        "      List available upstream providers.\n"
    )
    return 0


__all__ = ["cmd_proxy", "cmd_proxy_start", "cmd_proxy_status", "cmd_proxy_list_providers"]
