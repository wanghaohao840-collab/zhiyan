from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse

from api.dependencies import (
    get_csrf_validated_session,
    get_current_session,
    get_qa_service,
    get_session_token,
)
from api.errors import error_response
from api.schemas.qa import (
    AskRequest,
    CreateConversationRequest,
    QaActiveJobResponse,
    QaCapabilitiesResponse,
    QaConversationPageResponse,
    QaConversationResponse,
    QaDeletionResponse,
    QaJobResponse,
    QaMessagePageResponse,
    QaMessageResponse,
    RetryRequest,
    SummaryRequest,
    active_job_response,
    conversation_page_response,
    conversation_response,
    deletion_response,
    job_response,
    message_page_response,
    message_response,
)
from app.qa_models import QaValidationError
from app.qa_service import (
    QaBusyError,
    QaEngineUnavailableError,
    QaNotFoundError,
    QaRetryNotAllowedError,
    QaService,
)
from app.session import UserSession


router = APIRouter(prefix="/api/v1/qa", tags=["qa"])


def _disabled(request: Request) -> JSONResponse | None:
    if request.app.state.api_config.qa_route_enabled:
        return None
    return error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "QA_ROUTE_DISABLED",
        "智能问答功能当前未启用",
        retryable=False,
    )


def _not_found() -> JSONResponse:
    return error_response(
        status.HTTP_404_NOT_FOUND,
        "QA_NOT_FOUND",
        "问答资源不存在",
    )


def _domain_error(error: Exception) -> JSONResponse:
    if isinstance(error, QaNotFoundError):
        return _not_found()
    if isinstance(error, QaBusyError):
        return error_response(
            status.HTTP_409_CONFLICT,
            error.code,
            "当前对话正在处理另一项请求",
            retryable=True,
        )
    if isinstance(error, QaRetryNotAllowedError):
        return error_response(
            status.HTTP_409_CONFLICT,
            error.code,
            "该回答当前不可重试",
        )
    if isinstance(error, QaEngineUnavailableError):
        return error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error.code,
            "问答服务暂时不可用，请稍后重试",
            retryable=error.retryable,
            trace_id=error.trace_id,
        )
    if isinstance(error, QaValidationError):
        status_code = (
            status.HTTP_409_CONFLICT
            if error.code in {"QA_DOCUMENT_DELETING"}
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return error_response(
            status_code,
            error.code,
            "问答请求无效",
        )
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "QA_OPERATION_FAILED",
        "问答操作失败，请稍后重试",
        retryable=True,
    )


@router.get("/capabilities", response_model=QaCapabilitiesResponse)
def capabilities(
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
) -> QaCapabilitiesResponse:
    return QaCapabilitiesResponse(
        enabled=request.app.state.api_config.qa_route_enabled
    )


@router.post(
    "/conversations",
    response_model=QaConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    body: CreateConversationRequest,
    request: Request,
    _session: Annotated[UserSession, Depends(get_csrf_validated_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        return conversation_response(
            service.create_conversation(
                get_session_token(request), body.document_ids
            )
        )
    except Exception as error:
        return _domain_error(error)


@router.get("/conversations", response_model=QaConversationPageResponse)
def list_conversations(
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if disabled := _disabled(request):
        return disabled
    try:
        return conversation_page_response(
            service.list_conversations(
                get_session_token(request), cursor=cursor, limit=limit
            )
        )
    except Exception as error:
        return _domain_error(error)


@router.get(
    "/conversations/{conversation_id}",
    response_model=QaConversationResponse,
)
def get_conversation(
    conversation_id: UUID,
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        return conversation_response(
            service.get_conversation(
                get_session_token(request), str(conversation_id)
            )
        )
    except Exception as error:
        return _domain_error(error)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=QaMessagePageResponse,
)
def list_messages(
    conversation_id: UUID,
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    if disabled := _disabled(request):
        return disabled
    try:
        return message_page_response(
            service.list_messages(
                get_session_token(request),
                str(conversation_id),
                cursor=cursor,
                limit=limit,
            )
        )
    except Exception as error:
        return _domain_error(error)


@router.delete(
    "/conversations/{conversation_id}",
    response_model=QaDeletionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_conversation(
    conversation_id: UUID,
    request: Request,
    _session: Annotated[UserSession, Depends(get_csrf_validated_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        return deletion_response(
            service.delete_conversation(
                get_session_token(request), str(conversation_id)
            )
        )
    except Exception as error:
        return _domain_error(error)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=QaMessageResponse,
)
def ask(
    conversation_id: UUID,
    body: AskRequest,
    response: Response,
    request: Request,
    _session: Annotated[UserSession, Depends(get_csrf_validated_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        message = service.ask(
            get_session_token(request),
            str(conversation_id),
            body.question,
            body.mode,
            str(body.client_request_id),
        )
        if message.status == "pending":
            response.status_code = status.HTTP_202_ACCEPTED
        return message_response(message)
    except Exception as error:
        return _domain_error(error)


@router.get("/messages/{message_id}", response_model=QaMessageResponse)
def get_message(
    message_id: UUID,
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        return message_response(
            service.get_message(get_session_token(request), str(message_id))
        )
    except Exception as error:
        return _domain_error(error)


@router.post(
    "/messages/{message_id}/retry",
    response_model=QaMessageResponse,
)
def retry_message(
    message_id: UUID,
    body: RetryRequest,
    response: Response,
    request: Request,
    _session: Annotated[UserSession, Depends(get_csrf_validated_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        message = service.retry(
            get_session_token(request),
            str(message_id),
            str(body.client_request_id),
        )
        if message.status == "pending":
            response.status_code = status.HTTP_202_ACCEPTED
        return message_response(message)
    except Exception as error:
        return _domain_error(error)


@router.post(
    "/messages/{message_id}/summary-retry",
    response_model=QaJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_summary(
    message_id: UUID,
    body: RetryRequest,
    request: Request,
    _session: Annotated[UserSession, Depends(get_csrf_validated_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        return job_response(
            service.retry_summary(
                get_session_token(request), str(message_id), str(body.client_request_id)
            )
        )
    except Exception as error:
        return _domain_error(error)


@router.get(
    "/conversations/{conversation_id}/summary-jobs/active",
    response_model=QaActiveJobResponse,
)
def get_active_summary_job(
    conversation_id: UUID,
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        return active_job_response(
            service.get_active_job(
                get_session_token(request), str(conversation_id)
            )
        )
    except Exception as error:
        return _domain_error(error)


@router.post(
    "/conversations/{conversation_id}/summary-jobs",
    response_model=QaJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_summary(
    conversation_id: UUID,
    body: SummaryRequest,
    request: Request,
    _session: Annotated[UserSession, Depends(get_csrf_validated_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        return job_response(
            service.start_summary(
                get_session_token(request),
                str(conversation_id),
                body.instruction or "",
                str(body.client_request_id),
            )
        )
    except Exception as error:
        return _domain_error(error)


@router.get("/jobs/{job_id}", response_model=QaJobResponse)
def get_job(
    job_id: UUID,
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        job = service.get_job(get_session_token(request), str(job_id))
        return job_response(job) if job is not None else _not_found()
    except Exception as error:
        return _domain_error(error)


@router.post("/jobs/{job_id}/cancel", response_model=QaJobResponse)
def cancel_job(
    job_id: UUID,
    request: Request,
    _session: Annotated[UserSession, Depends(get_csrf_validated_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        job = service.cancel_job(get_session_token(request), str(job_id))
        return job_response(job) if job is not None else _not_found()
    except Exception as error:
        return _domain_error(error)


@router.get("/deletions/{deletion_id}", response_model=QaDeletionResponse)
def get_deletion(
    deletion_id: UUID,
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[QaService, Depends(get_qa_service)],
):
    if disabled := _disabled(request):
        return disabled
    try:
        deletion = service.deletion_service.get_deletion(
            get_session_token(request), str(deletion_id)
        )
        return deletion_response(deletion) if deletion is not None else _not_found()
    except Exception as error:
        return _domain_error(error)
