"""Signed IAP identity verification and explicit local-development identity."""

import importlib
from typing import Protocol, cast

from app.domain.identity import IdentityProvider, OperatorIdentity, OperatorRole

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_PUBLIC_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"


class IdentityError(RuntimeError):
    """Raised when an operator identity cannot be verified safely."""


class TokenVerifier(Protocol):
    def verify(self, assertion: str, audience: str) -> dict[str, object]: ...


class _GoogleIdTokenModule(Protocol):
    def verify_token(
        self,
        token: str,
        request: object,
        audience: str,
        *,
        certs_url: str,
    ) -> dict[str, object]: ...


class _GoogleRequestModule(Protocol):
    class Request:
        def __init__(self) -> None: ...


class GoogleIAPTokenVerifier:
    """Small Google Auth adapter that verifies signature, expiry, and audience."""

    def verify(self, assertion: str, audience: str) -> dict[str, object]:
        try:
            id_token = cast(
                _GoogleIdTokenModule, importlib.import_module("google.oauth2.id_token")
            )
            transport = cast(
                _GoogleRequestModule,
                importlib.import_module("google.auth.transport.requests"),
            )
            return id_token.verify_token(
                assertion,
                transport.Request(),
                audience,
                certs_url=IAP_PUBLIC_KEYS_URL,
            )
        except Exception as error:
            raise IdentityError("IAP assertion signature or claims are invalid") from error


class IAPIdentityProvider(IdentityProvider):
    def __init__(
        self,
        *,
        audience: str,
        responder_principals: frozenset[str],
        token_verifier: TokenVerifier | None = None,
    ) -> None:
        if not audience:
            raise ValueError("IAP audience is required in deployed mode")
        self._audience = audience
        self._responders = frozenset(_normalize_principal(item) for item in responder_principals)
        self._token_verifier = token_verifier or GoogleIAPTokenVerifier()

    def authenticate(self, assertion: str | None) -> OperatorIdentity:
        if not assertion:
            raise IdentityError("signed IAP assertion is required")
        claims = self._token_verifier.verify(assertion, self._audience)
        if claims.get("iss") != IAP_ISSUER:
            raise IdentityError("IAP assertion issuer is invalid")
        subject = claims.get("sub")
        email = claims.get("email")
        if not isinstance(subject, str) or not subject:
            raise IdentityError("IAP assertion subject is missing")
        if not isinstance(email, str) or not email:
            raise IdentityError("IAP assertion email is missing")
        principal = _normalize_principal(email)
        role = (
            OperatorRole.RESPONDER
            if principal in self._responders
            else OperatorRole.VIEWER
        )
        return OperatorIdentity(principal=principal, subject=subject, role=role)


class LocalIdentityProvider(IdentityProvider):
    """Fake identity that refuses construction outside explicit development mode."""

    def __init__(self, *, runtime_mode: str, identity: OperatorIdentity) -> None:
        if runtime_mode != "development":
            raise ValueError("local identity is available only in explicit development mode")
        self._identity = identity

    def authenticate(self, assertion: str | None) -> OperatorIdentity:
        return self._identity


class UnavailableIdentityProvider(IdentityProvider):
    """Fail-closed deployed identity used when required configuration is absent."""

    def authenticate(self, assertion: str | None) -> OperatorIdentity:
        raise IdentityError("deployed IAP identity is not configured")


def _normalize_principal(principal: str) -> str:
    prefix = "accounts.google.com:"
    normalized = principal.strip().lower()
    return normalized[len(prefix) :] if normalized.startswith(prefix) else normalized


__all__ = [
    "GoogleIAPTokenVerifier",
    "IAPIdentityProvider",
    "IdentityError",
    "LocalIdentityProvider",
    "TokenVerifier",
    "UnavailableIdentityProvider",
]
