"""Keep inherited proxy settings from breaking the grading client.

httpx 2.x turns every ``NO_PROXY`` entry into a proxy-pattern URL and decides
the shape from the token: a *bare* IPv6 address becomes ``all://[::1]`` (valid),
but a *bracketed* one is treated as a domain and becomes ``all://*[::1]``,
which the client rejects with ``InvalidURL: Invalid port``. Some environments
export the bracketed form, so the grading engine's OpenAI client fails at
construction and every grade dies before any network call.

Canonicalising to the bare (curl-documented) form keeps the bypass rule
identical while giving httpx a token it understands. Nothing is added or
removed semantically, and a well-formed list is left untouched.
"""
from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import MutableMapping

_ENV_KEYS = ("NO_PROXY", "no_proxy")
_BRACKETED_IPV6 = re.compile(
    r"^\[(?P<addr>[0-9A-Fa-f:.]+)\](?::\d+)?(?:/(?P<network>\d+))?$"
)


def _normalise_token(token: str) -> str:
    match = _BRACKETED_IPV6.match(token)
    if match is None:
        return token
    try:
        ipaddress.IPv6Address(match.group("addr"))
    except ValueError:
        return token
    network = match.group("network")
    return match.group("addr") + (f"/{network}" if network else "")


def canonical_no_proxy(value: str) -> str:
    """Return a bypass list that httpx can parse, without changing its rules."""
    tokens: list[str] = []
    for raw in (value or "").split(","):
        token = _normalise_token(raw.strip())
        if token and token not in tokens:
            tokens.append(token)
    return ",".join(tokens)


def sanitize_no_proxy_env(
    environ: MutableMapping[str, str] | None = None,
) -> bool:
    """Normalise ``NO_PROXY``/``no_proxy`` in place; True when changed."""
    source = os.environ if environ is None else environ
    changed = False
    for key in _ENV_KEYS:
        raw = source.get(key)
        if raw is None:
            continue
        fixed = canonical_no_proxy(raw)
        if fixed != raw:
            source[key] = fixed
            changed = True
    return changed


__all__ = ["canonical_no_proxy", "sanitize_no_proxy_env"]