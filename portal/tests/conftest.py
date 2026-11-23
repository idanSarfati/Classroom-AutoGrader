"""Shared fixtures for the portal tests.

Tests build their own app with a temporary SQLite file and a fixed test
secret, so they never touch ``portal/data/portal.db`` or the real signing key.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portal.auth import ratelimit  # noqa: E402
from portal.config import PortalSettings  # noqa: E402
from portal.main import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def reset_login_throttle():
    ratelimit.reset_for_tests()
    yield
    ratelimit.reset_for_tests()


@pytest.fixture
def portal_settings(tmp_path: Path) -> PortalSettings:
    return PortalSettings(
        database_url=f"sqlite:///{(tmp_path / 'portal.db').as_posix()}",
        secret_key="test-secret-key-not-for-production",
        app_name="פורטל בדיקה",
        timezone="Asia/Jerusalem",
        environment="test",
        https_only=False,
    )


@pytest.fixture
def app(portal_settings: PortalSettings):
    return create_app(portal_settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def new_client(app):
    """An independent browser session, for testing teacher + student at once."""
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()
