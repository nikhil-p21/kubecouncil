from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.repositories import get_run_store
from app.domain.interfaces import ManifestRenderer, RunStore
from app.domain.models import AnalysisResult, DependencyEdge, RepositorySnapshot
from app.kubernetes.kustomize import (
    KustomizeManifestRenderer,
    ManifestParseError,
    ManifestRenderError,
)

router = APIRouter(prefix="/api/runs", tags=["runs"])

_manifest_renderer = KustomizeManifestRenderer()


def get_manifest_renderer() -> ManifestRenderer:
    return _manifest_renderer


def error_detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


@router.post(
    "/{run_id}/analyse",
    response_model=AnalysisResult,
    status_code=status.HTTP_200_OK,
)
def analyse_run(
    run_id: str,
    renderer: Annotated[ManifestRenderer, Depends(get_manifest_renderer)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> AnalysisResult:
    stored_snapshot = store.get(run_id, "repository_snapshot")
    if not isinstance(stored_snapshot, RepositorySnapshot):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail("repository_snapshot_not_found", "repository is not connected"),
        )

    try:
        source = renderer.render(stored_snapshot)
        services = tuple(renderer.service_profiles(source))
    except ManifestRenderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_detail("manifest_render_failed", str(exc)),
        ) from exc
    except ManifestParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("manifest_parse_failed", str(exc)),
        ) from exc

    result = AnalysisResult(
        run_id=run_id,
        source=source,
        services=services,
        compatibility_issues=source.compatibility_issues,
        dependency_edges=tuple(
            DependencyEdge(from_service=service.name, to_service=dependency)
            for service in services
            for dependency in service.dependencies
        ),
    )
    store.put(run_id, "deployment_source", source)
    store.put(run_id, "service_profiles", services)
    store.put(run_id, "compatibility_issues", source.compatibility_issues)
    store.put(run_id, "analysis_result", result)
    return result
