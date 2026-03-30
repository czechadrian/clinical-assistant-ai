"""
Set required environment variables BEFORE main.py is imported.

main.py calls Settings.from_env() at module level, which reads SUPABASE_URL
and SUPABASE_SERVICE_ROLE_KEY via os.environ[]. Without these the import fails.
Setting them here (before any test file imports main) is the minimal fix.
"""

import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
# Empty string → _embedder stays None in lifespan → _retrieve_context returns []
# Tests that need a real embedder mock it directly via @patch("main._embedder").
os.environ.setdefault("OPENAI_API_KEY", "")

import pytest
from starlette.testclient import TestClient

from main import Auth, app, get_auth


def _fake_auth() -> Auth:
    return Auth(user_id="user-test-123", jwt="fake-jwt")


@pytest.fixture(scope="module")
def client():
    """TestClient with lifespan + auth dependency overridden."""
    app.dependency_overrides[get_auth] = _fake_auth
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
