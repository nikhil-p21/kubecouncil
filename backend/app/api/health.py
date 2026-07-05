from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "kubecouncil-backend"}


@router.get("/ready")
def ready() -> dict[str, object]:
    return {
        "status": "ok",
        "checks": {
            "api": "ok",
        },
    }
