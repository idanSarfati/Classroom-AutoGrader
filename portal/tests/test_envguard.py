"""The NO_PROXY normaliser protects the grading client from inherited config."""
from __future__ import annotations

from portal.grading.envguard import canonical_no_proxy, sanitize_no_proxy_env


def test_bracketed_ipv6_entries_are_unbracketed_and_deduplicated() -> None:
    raw = "localhost,.localhost,127.0.0.1,127.0.0.0/8,[::1],::1"

    assert canonical_no_proxy(raw) == (
        "localhost,.localhost,127.0.0.1,127.0.0.0/8,::1"
    )


def test_bracketed_ipv6_cidr_keeps_its_prefix_length() -> None:
    assert canonical_no_proxy("[fd00::]/8") == "fd00::/8"


def test_bare_ipv6_is_already_canonical() -> None:
    assert canonical_no_proxy("::1") == "::1"


def test_hostnames_and_port_suffixed_hosts_are_untouched() -> None:
    raw = "example.com,.internal,10.0.0.5:8080,*.local"

    assert canonical_no_proxy(raw) == raw


def test_sanitize_rewrites_both_spellings_in_place() -> None:
    environ = {"NO_PROXY": "[::1]", "no_proxy": "[::1],::1", "OTHER": "x"}

    assert sanitize_no_proxy_env(environ) is True
    assert environ == {"NO_PROXY": "::1", "no_proxy": "::1", "OTHER": "x"}


def test_well_formed_or_absent_values_report_no_change() -> None:
    environ = {"NO_PROXY": "::1,localhost"}

    assert sanitize_no_proxy_env(environ) is False
    assert sanitize_no_proxy_env({}) is False