from pathlib import Path
from tempfile import gettempdir
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status

from app.domain.fakes import InMemoryRunStore
from app.domain.interfaces import RepositoryProvider, RunStore
from app.domain.models import RepositoryConnection, RepositorySnapshot
from app.repositories.github import (
    GitHubRepositoryProvider,
    InvalidRepositoryUrlError,
    MissingDeploymentPathError,
    RepositoryCloneError,
)

router = APIRouter(prefix="/api/repositories", tags=["repositories"])

_run_store = InMemoryRunStore()
_repository_provider = GitHubRepositoryProvider(Path(gettempdir()) / "kubecouncil-workspaces")


def get_run_store() -> RunStore:
    return _run_store


def get_repository_provider() -> RepositoryProvider:
    return _repository_provider


def error_detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


@router.post(
    "/connect",
    response_model=RepositorySnapshot,
    status_code=status.HTTP_201_CREATED,
)
def connect_repository(
    connection: RepositoryConnection,
    provider: Annotated[RepositoryProvider, Depends(get_repository_provider)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> RepositorySnapshot:
    run_id = f"run-{uuid4().hex}"
    try:
        snapshot = provider.connect(connection, run_id)
    except InvalidRepositoryUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail("invalid_repository_url", str(exc)),
        ) from exc
    except MissingDeploymentPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("missing_deployment_path", str(exc)),
        ) from exc
    except RepositoryCloneError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_detail("repository_clone_failed", str(exc)),
        ) from exc

    store.put(run_id, "repository_snapshot", snapshot)
    return snapshot
