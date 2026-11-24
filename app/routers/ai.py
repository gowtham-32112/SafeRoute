"""SafeRoute AI Router.

Provides:
- POST /api/ai/assistant: Commuter Civic AI assistant query answering live road stats,
  how many hazards are reported, how many resolved, category breakdowns, and hotspots.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_settings
from app.services.ai_assistant import (
    AiAssistantRequest,
    AiAssistantResponse,
    answer_commuter_query,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["AI"])


@router.post(
    "/assistant",
    response_model=AiAssistantResponse,
    status_code=status.HTTP_200_OK,
    summary="Commuter Civic AI Assistant Query",
)
async def commuter_ai_assistant_endpoint(
    request: Request,
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    app_settings: Settings = Depends(get_settings),
) -> AiAssistantResponse:
    """Answer commuter questions regarding live road issues and resolution stats."""
    try:
        raw_body = await request.json()
        payload = AiAssistantRequest.model_validate(raw_body)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid query payload. Please provide a non-empty query string.",
        ) from exc

    return await run_in_threadpool(
        answer_commuter_query,
        query=payload.query,
        access_token=ctx.access_token,
        app_settings=app_settings,
    )
