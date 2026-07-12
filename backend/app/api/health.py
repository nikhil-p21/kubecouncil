from fastapi import APIRouter, Request, Response, status

from app.runtime.readiness import ReadinessRegistry

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "kubecouncil-backend"}


@router.get("/ready")
def ready(request: Request, response: Response) -> dict[str, object]:
    registry = getattr(request.app.state, "readiness", None)
    if not isinstance(registry, ReadinessRegistry):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not_ready",
            "checks": {"api": "failed"},
            "details": {"api": "Readiness is uninitialized."},
            "approval_enabled": False,
        }
    checks = registry.snapshot()
    if not registry.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if registry.ready else "not_ready",
        "checks": {name: "ok" if check.ready else "failed" for name, check in checks.items()},
        "details": {name: check.detail for name, check in checks.items()},
        "approval_enabled": registry.approval_enabled,
    }
