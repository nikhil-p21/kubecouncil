import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

REQUEST_ID_HEADER = "X-Request-ID"
_request_id: ContextVar[str] = ContextVar("kubecouncil_request_id", default="-")


def current_request_id() -> str:
    return _request_id.get()


class JsonLogFormatter(logging.Formatter):
    """Formats application logs for Cloud Logging ingestion without request bodies."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": getattr(record, "request_id", current_request_id()),
        }
        if hasattr(record, "http_method"):
            payload["http_method"] = record.http_method
        if hasattr(record, "http_path"):
            payload["http_path"] = record.http_path
        if hasattr(record, "http_status"):
            payload["http_status"] = record.http_status
        if hasattr(record, "duration_ms"):
            payload["duration_ms"] = record.duration_ms
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


class RequestIdMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._logger = logging.getLogger("kubecouncil.requests")

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = _clean_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = _request_id.set(request_id)
        started_at = time.perf_counter()
        status_code = 500
        response: Response | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            self._logger.exception(
                "request failed",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": status_code,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                },
            )
            raise
        finally:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            if response is not None:
                response.headers[REQUEST_ID_HEADER] = request_id
            self._logger.info(
                "request completed",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": status_code,
                    "duration_ms": duration_ms,
                },
            )
            _request_id.reset(token)


def _clean_request_id(value: str | None) -> str:
    if value is None:
        return uuid4().hex
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 128 or any(ord(character) < 32 for character in cleaned):
        return uuid4().hex
    return cleaned
