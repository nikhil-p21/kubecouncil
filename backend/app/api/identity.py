"""Verified operator identity dependencies and current-principal endpoint."""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.domain.identity import IdentityProvider, OperatorIdentity, OperatorRole
from app.services.identity import IdentityError

router = APIRouter(prefix="/api/identity", tags=["identity"])


def get_identity_provider(request: Request) -> IdentityProvider:
    provider = getattr(request.app.state, "identity_provider", None)
    if not callable(getattr(provider, "authenticate", None)):
        raise RuntimeError("identity provider is unavailable")
    return cast(IdentityProvider, provider)


def get_current_identity(
    provider: Annotated[IdentityProvider, Depends(get_identity_provider)],
    assertion: Annotated[
        str | None, Header(alias="X-Goog-IAP-JWT-Assertion")
    ] = None,
) -> OperatorIdentity:
    try:
        return provider.authenticate(assertion)
    except IdentityError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "identity_unverified", "message": str(error)},
        ) from error


def require_responder(
    identity: Annotated[OperatorIdentity, Depends(get_current_identity)],
) -> OperatorIdentity:
    if identity.role is not OperatorRole.RESPONDER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "responder_required",
                "message": "Responder role is required for this command",
            },
        )
    return identity


@router.get("/me", response_model=OperatorIdentity)
def current_identity(
    identity: Annotated[OperatorIdentity, Depends(get_current_identity)],
) -> OperatorIdentity:
    return identity


__all__ = ["get_current_identity", "get_identity_provider", "require_responder", "router"]
