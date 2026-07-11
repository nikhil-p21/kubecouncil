"""Provider-independent operator identity and authorization contracts."""

from enum import StrEnum
from typing import Protocol

from pydantic import Field

from app.domain.models import KubeCouncilModel


class OperatorRole(StrEnum):
    VIEWER = "viewer"
    RESPONDER = "responder"


class OperatorIdentity(KubeCouncilModel):
    principal: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    role: OperatorRole


class IdentityProvider(Protocol):
    """Verifies one provider assertion and returns application-level authority."""

    def authenticate(self, assertion: str | None) -> OperatorIdentity: ...


__all__ = ["IdentityProvider", "OperatorIdentity", "OperatorRole"]
