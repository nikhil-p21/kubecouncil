"""Explicit test identity; deployed application defaults remain fail-closed."""

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("KUBECOUNCIL_RUNTIME_MODE", "test")

from app.domain.identity import OperatorIdentity, OperatorRole
from app.main import app
from app.services.identity import LocalIdentityProvider


@pytest.fixture(autouse=True)
def authenticated_responder() -> Iterator[None]:
    previous = app.state.identity_provider
    app.state.identity_provider = LocalIdentityProvider(
        runtime_mode="development",
        identity=OperatorIdentity(
            principal="test-responder@example.com",
            subject="test:responder",
            role=OperatorRole.RESPONDER,
        ),
    )
    try:
        yield
    finally:
        app.state.identity_provider = previous
