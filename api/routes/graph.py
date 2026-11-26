from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from api.dependencies import get_current_session, get_document_library_service
from api.errors import error_response
from app.document_library import DocumentLibraryService, DocumentNotFoundError
from app.graph_service import GraphBusyError, GraphService, GraphUnavailableError
from app.session import UserSession


router = APIRouter(prefix="/api/v1/graph", tags=["graph"])


class GraphCapabilitiesResponse(BaseModel):
    enabled: bool
    reason: str | None = None


class GraphNode(BaseModel):
    id: str
    label: str
    type: str
    document_ids: list[str]
    weight: float


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: str
    weight: float


class GraphSubgraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool


def _graph_service(request: Request) -> GraphService | None:
    return getattr(request.app.state.services, "graph_service", None)


def _graph_error(status: int, code: str, message: str, *, retryable: bool = False):
    response = error_response(status, code, message, retryable=retryable)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get(
    "/capabilities",
    response_model=GraphCapabilitiesResponse,
    response_model_exclude_none=True,
)
def capabilities(
    response: Response,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[GraphService | None, Depends(_graph_service)],
):
    response.headers["Cache-Control"] = "private, no-store"
    enabled, reason = service.capabilities() if service else (False, "graph_disabled")
    return GraphCapabilitiesResponse(enabled=enabled, reason=reason)


@router.get("/subgraph", response_model=GraphSubgraphResponse)
def subgraph(
    response: Response,
    session: Annotated[UserSession, Depends(get_current_session)],
    documents: Annotated[DocumentLibraryService, Depends(get_document_library_service)],
    service: Annotated[GraphService | None, Depends(_graph_service)],
    document_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    query: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = 120,
):
    try:
        if service is None:
            raise GraphUnavailableError("graph_disabled")
        result = service.subgraph(
            session, documents,
            document_id=document_id,
            query=query,
            limit=limit,
        )
    except DocumentNotFoundError:
        return _graph_error(404, "document_not_found", "文档不存在")
    except GraphBusyError:
        return _graph_error(429, "graph_busy", "知识图谱查询繁忙，请稍后重试", retryable=True)
    except GraphUnavailableError as error:
        if str(error) == "graph_disabled":
            return _graph_error(503, "graph_disabled", "知识图谱尚未启用")
        return _graph_error(503, "graph_unavailable", "知识图谱暂时不可用", retryable=True)
    response.headers["Cache-Control"] = "private, no-store"
    return result
